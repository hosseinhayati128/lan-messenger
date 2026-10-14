"""LAN messenger: browser clients, private inboxes, and local SQLite history."""
import argparse
import hashlib
import hmac
import ipaddress
import json
import pathlib
import re
import secrets
import socket
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from lan_platform import detect_host, detect_network, network_neighbors


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        self.moving_to = None
        path.parent.mkdir(parents=True, exist_ok=True)
        self.uploads = path.parent / "uploads"
        self.uploads.mkdir(exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS devices(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE,
                    ip TEXT NOT NULL, created REAL NOT NULL, seen REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL REFERENCES devices(id),
                    recipient TEXT NOT NULL REFERENCES devices(id),
                    body TEXT NOT NULL, sent REAL NOT NULL, read_at REAL);
                CREATE INDEX IF NOT EXISTS messages_pair ON messages(sender,recipient,id);
                CREATE INDEX IF NOT EXISTS inbox ON messages(recipient,read_at,id);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS device_sessions(
                    token_hash TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
                    seen REAL NOT NULL, ip TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS device_aliases(
                    alias_id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id));
                CREATE TABLE IF NOT EXISTS attachments(
                    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
                    name TEXT NOT NULL, size INTEGER NOT NULL, storage_key TEXT NOT NULL UNIQUE);
            """)
        self.prepare_devices()

    def backup(self, db):
        destination = pathlib.Path(self.path).with_name(
            "history-before-device-merge-" + str(time.time_ns()) + ".sqlite3")
        target = sqlite3.connect(destination)
        try:
            db.backup(target)
        finally:
            target.close()

    def merge_devices(self, db, rows):
        canonical = min(rows, key=lambda row: (row["created"], row["id"]))
        defaults = {"Windows PC", "Ubuntu / Linux", "Ubuntu PC", "Android", "iPhone", "Mac", "Computer"}
        named = max(rows, key=lambda row: (row["name"] not in defaults, row["seen"], row["created"]))
        identifier = canonical["id"]
        for row in rows:
            old = row["id"]
            if old == identifier:
                continue
            db.execute("UPDATE messages SET sender=? WHERE sender=?", (identifier, old))
            db.execute("UPDATE messages SET recipient=? WHERE recipient=?", (identifier, old))
            db.execute("UPDATE device_sessions SET device_id=? WHERE device_id=?", (identifier, old))
            db.execute("UPDATE device_aliases SET device_id=? WHERE device_id=?", (identifier, old))
            db.execute("INSERT OR REPLACE INTO device_aliases VALUES(?,?)", (old, identifier))
            db.execute("DELETE FROM devices WHERE id=?", (old,))
        db.execute("UPDATE devices SET name=?,seen=? WHERE id=?",
                   (named["name"], max(row["seen"] for row in rows), identifier))
        return db.execute("SELECT * FROM devices WHERE id=?", (identifier,)).fetchone()

    def prepare_devices(self):
        with self.lock, self.connect() as db:
            groups = db.execute("SELECT ip FROM devices GROUP BY ip HAVING COUNT(*)>1").fetchall()
            if groups:
                self.backup(db)
            db.execute("INSERT OR IGNORE INTO device_sessions "
                       "SELECT token_hash,id,seen,ip,created FROM devices WHERE token_hash IS NOT NULL")
            for group in groups:
                rows = db.execute("SELECT * FROM devices WHERE ip=?", (group["ip"],)).fetchall()
                self.merge_devices(db, rows)

    def resolve(self, db, identifier):
        row = db.execute("SELECT device_id FROM device_aliases WHERE alias_id=?", (identifier,)).fetchone()
        return row["device_id"] if row else identifier

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def identity(self, token, ip, agent, mark_seen=True):
        digest = hashlib.sha256(token.encode()).hexdigest() if token else None
        with self.lock, self.connect() as db:
            row = db.execute("SELECT d.* FROM devices d JOIN device_sessions s ON s.device_id=d.id "
                             "WHERE s.token_hash=?", (digest,)).fetchone() if digest else None
            if not row:
                token = secrets.token_urlsafe(32)
                digest = hashlib.sha256(token.encode()).hexdigest()
                row = db.execute("SELECT * FROM devices WHERE ip=?", (ip,)).fetchone()
                if not row:
                    name = ("Android" if "Android" in agent else "iPhone" if "iPhone" in agent else
                            "Windows PC" if "Windows" in agent else "Ubuntu / Linux" if "Linux" in agent else
                            "Mac" if "Macintosh" in agent else "Computer")
                    identifier = secrets.token_hex(16)
                    db.execute("INSERT INTO devices VALUES(?,?,?,?,?,?)",
                               (identifier, name, digest, ip, time.time(), 0))
                    row = db.execute("SELECT * FROM devices WHERE id=?", (identifier,)).fetchone()
                db.execute("INSERT INTO device_sessions VALUES(?,?,?,?,?)",
                           (digest, row["id"], 0, ip, time.time()))
            elif row["ip"] != ip:
                others = db.execute("SELECT * FROM devices WHERE ip=? AND id<>?", (ip, row["id"])).fetchall()
                if others:
                    self.backup(db)
                    row = self.merge_devices(db, [row] + list(others))
                db.execute("UPDATE devices SET ip=? WHERE id=?", (ip, row["id"]))
            if mark_seen:
                db.execute("UPDATE devices SET ip=?,seen=? WHERE id=?", (ip, time.time(), row["id"]))
                db.execute("UPDATE device_sessions SET ip=?,seen=? WHERE token_hash=?",
                           (ip, time.time(), digest))
            return row["id"], token

    def migrate(self, legacy, host, peer):
        with self.lock, self.connect() as db:
            if db.execute("SELECT 1 FROM metadata WHERE key='legacy-imported'").fetchone():
                return
            now = time.time()
            ids = {}
            for side, name, ip in (("windows", "Windows PC", host), ("ubuntu", "Ubuntu PC", peer)):
                identifier = secrets.token_hex(16)
                ids[side] = identifier
                db.execute("INSERT INTO devices VALUES(?,?,?,?,?,?)", (identifier, name, None, ip, now, 0))
            for side in ("windows", "ubuntu"):
                entry = legacy.get(side, {})
                if entry.get("text"):
                    other = "ubuntu" if side == "windows" else "windows"
                    db.execute("INSERT INTO messages(sender,recipient,body,sent) VALUES(?,?,?,?)",
                               (ids[side], ids[other], entry["text"], now))
            db.execute("INSERT INTO metadata VALUES('legacy-imported','1')")

    def state(self, me):
        with self.connect() as db:
            own = db.execute("SELECT id,name,ip FROM devices WHERE id=?", (me,)).fetchone()
            contacts = []
            for row in db.execute("SELECT id,name,ip,seen FROM devices WHERE id<>?", (me,)):
                last = db.execute(
                    "SELECT body,sent,sender,a.name AS file_name FROM messages m "
                    "LEFT JOIN attachments a ON a.message_id=m.id WHERE "
                    "(sender=? AND recipient=?) OR (sender=? AND recipient=?) ORDER BY m.id DESC LIMIT 1",
                    (me, row["id"], row["id"], me)).fetchone()
                unread = db.execute("SELECT COUNT(*) FROM messages WHERE sender=? AND recipient=? AND read_at IS NULL",
                                    (row["id"], me)).fetchone()[0]
                contacts.append(dict(id=row["id"], name=row["name"], ip=row["ip"],
                    online=time.time()-row["seen"] < 15, unread=unread,
                    preview=(("📎 " + last["file_name"] if last["file_name"] is not None else last["body"][:100]) if last else ""),
                    last_sent=(last["sent"] if last else 0)))
            saved = db.execute("SELECT body,sent,a.name AS file_name FROM messages m "
                               "LEFT JOIN attachments a ON a.message_id=m.id WHERE sender=? AND recipient=? "
                               "ORDER BY m.id DESC LIMIT 1", (me, me)).fetchone()
            if saved:
                contacts.append(dict(id=me, name="Saved messages", ip=own["ip"], online=True,
                    unread=0, preview=("📎 " + saved["file_name"] if saved["file_name"] is not None else saved["body"][:100]),
                    last_sent=saved["sent"], is_self=True))
            contacts.sort(key=lambda item: (-item["last_sent"], item["name"].casefold(), item["id"]))
            aliases = {row["alias_id"]: row["device_id"] for row in db.execute("SELECT * FROM device_aliases")}
            return dict(me=dict(own), devices=contacts, unread=sum(item["unread"] for item in contacts), aliases=aliases)

    def history(self, me, peer, before=0):
        with self.connect() as db:
            peer = self.resolve(db, peer)
            if not db.execute("SELECT 1 FROM devices WHERE id=?", (peer,)).fetchone():
                raise ValueError("That computer is no longer available.")
            rows = db.execute(
                "SELECT m.id,sender,recipient,body,sent,read_at,a.name AS file_name,a.size AS file_size "
                "FROM messages m LEFT JOIN attachments a ON a.message_id=m.id WHERE "
                "((sender=? AND recipient=?) OR (sender=? AND recipient=?)) AND (?=0 OR id<?) "
                "ORDER BY m.id DESC LIMIT 101", (me, peer, peer, me, before, before)).fetchall()
            more = len(rows) > 100
            messages = []
            for row in reversed(rows[:100]):
                message = dict(row)
                name, size = message.pop("file_name"), message.pop("file_size")
                if name is not None:
                    message["attachment"] = dict(name=name, size=size)
                messages.append(message)
            return dict(messages=messages, more=more)

    def attach(self, me, peer, name, size, reader):
        self.ensure_writable()
        if not isinstance(peer, str):
            raise ValueError("Choose a computer first.")
        if not isinstance(size, int) or size < 0:
            raise ValueError("The file size is missing or invalid.")
        if not isinstance(name, str):
            raise ValueError("Choose a named file.")
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
        name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127)[:240]
        if not name.strip() or name in (".", ".."):
            raise ValueError("Choose a named file.")
        with self.connect() as db:
            peer = self.resolve(db, peer)
            if not db.execute("SELECT 1 FROM devices WHERE id=?", (peer,)).fetchone():
                raise ValueError("That computer is no longer available.")
        key = secrets.token_hex(24) + ".bin"
        temporary, destination = self.uploads / (key + ".part"), self.uploads / key
        committed = False
        try:
            with temporary.open("xb") as output:
                remaining = size
                while remaining:
                    chunk = reader.read(min(65536, remaining))
                    if not chunk:
                        raise ValueError("Upload was interrupted. Select Send to retry.")
                    output.write(chunk)
                    remaining -= len(chunk)
            temporary.replace(destination)
            with self.lock, self.connect() as db:
                self.ensure_writable()
                me, peer = self.resolve(db, me), self.resolve(db, peer)
                result = db.execute("INSERT INTO messages(sender,recipient,body,sent) VALUES(?,?,?,?)",
                                    (me, peer, "", time.time()))
                identifier = result.lastrowid
                db.execute("INSERT INTO attachments VALUES(?,?,?,?)", (identifier, name, size, key))
            committed = True
            return identifier
        finally:
            temporary.unlink(missing_ok=True)
            if not committed:
                destination.unlink(missing_ok=True)

    def attachment(self, me, identifier):
        with self.connect() as db:
            me = self.resolve(db, me)
            row = db.execute("SELECT a.* FROM attachments a JOIN messages m ON m.id=a.message_id "
                             "WHERE m.id=? AND (m.sender=? OR m.recipient=?)", (identifier, me, me)).fetchone()
            if not row:
                return None
            return dict(name=row["name"], size=row["size"], path=self.uploads / row["storage_key"])

    def send(self, me, peer, text):
        if not isinstance(peer, str):
            raise ValueError("Choose another computer.")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Type or paste a message first.")
        if len(text.encode("utf-8")) > 200000:
            raise ValueError("This message is too long. Send less than 200 KB at a time.")
        with self.lock, self.connect() as db:
            self.ensure_writable()
            peer = self.resolve(db, peer)
            if not db.execute("SELECT 1 FROM devices WHERE id=?", (peer,)).fetchone():
                raise ValueError("Choose another computer.")
            result = db.execute("INSERT INTO messages(sender,recipient,body,sent) VALUES(?,?,?,?)",
                                (me, peer, text, time.time()))
            return result.lastrowid

    def read(self, me, peer, through):
        with self.lock, self.connect() as db:
            peer = self.resolve(db, peer)
            db.execute("UPDATE messages SET read_at=? WHERE sender=? AND recipient=? AND id<=? AND read_at IS NULL",
                       (time.time(), peer, me, through))

    def rename(self, me, name):
        if not isinstance(name, str):
            raise ValueError("Enter a name for this computer.")
        name = " ".join(name.split())[:48]
        if not name:
            raise ValueError("Enter a name for this computer.")
        with self.lock, self.connect() as db:
            self.ensure_writable()
            db.execute("UPDATE devices SET name=? WHERE id=?", (name, me))

    def ensure_writable(self):
        if self.moving_to:
            raise ValueError("This server is moving to Ubuntu. Continue at " + self.moving_to)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host")
    parser.add_argument("--network")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--local-port", type=int, default=8766)
    parser.add_argument("--data-dir", type=pathlib.Path, default=pathlib.Path(__file__).parent / "messenger-data")
    parser.add_argument("--replace-transfer", action="store_true")
    parser.add_argument("--legacy-peer", default="192.168.128.52")
    parser.add_argument("--lifetime", type=int, default=0)
    parser.add_argument("--migration-target", help="Prepare a private transfer to this Ubuntu LAN address")
    args = parser.parse_args()
    if not args.host:
        args.host = detect_host(args.network)
    network = ipaddress.ip_network(args.network, strict=False) if args.network else detect_network(args.host)
    site = pathlib.Path(__file__).with_name("messenger.html").read_bytes()
    store = Store(args.data_dir / "history.sqlite3")
    stop = threading.Event()
    secret = secrets.token_bytes(32)
    origin = "http://" + args.host + ":" + str(args.port)
    local_origin = "http://127.0.0.1:" + str(args.local_port)
    admin_token = secrets.token_urlsafe(32)
    neighbors = {"time": 0, "items": []}
    neighbor_lock = threading.Lock()
    migration = None
    if args.migration_target:
        from ubuntu_migration import Migration
        target = ipaddress.ip_address(args.migration_target)
        if target not in network or target == ipaddress.ip_address(args.host):
            parser.error("The Ubuntu migration target must be another computer on this LAN.")
        migration = Migration(store, pathlib.Path(__file__).parent, str(target), str(network), origin)

    if args.replace_transfer:
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(local_origin + "/state", timeout=4) as response:
                old = json.load(response)
            store.migrate(old, args.host, args.legacy_peer)
            with opener.open(local_origin + "/", timeout=4) as response:
                old_page = response.read().decode("utf-8")
            token = re.search("name='token' value='([^']+)'", old_page).group(1)
            opener.open(Request(local_origin + "/stop",
                                data=urlencode({"token": token}).encode()), timeout=4).read()
        except URLError as error:
            if not isinstance(error.reason, ConnectionRefusedError):
                raise

    def discover():
        with neighbor_lock:
            if time.time() - neighbors["time"] < 30:
                return neighbors["items"]
            try:
                neighbors.update(time=time.time(), items=network_neighbors(args.host, network))
            except (OSError, ValueError, subprocess.SubprocessError):
                neighbors.update(time=time.time(), items=[])
            return neighbors["items"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def allowed(self):
            try:
                address = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            local = self.server.server_address[1] == args.local_port
            expected = local_origin if local else origin
            return ((address.is_loopback if local else address in network)
                    and self.headers.get("Host") == expected.removeprefix("http://"))

        def respond(self, code, value, content_type="application/json; charset=utf-8", cookie=None, location=None):
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False).encode("utf-8")
            elif isinstance(value, str):
                value = value.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(value)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
                             "form-action 'self'; frame-ancestors 'none'")
            if cookie:
                self.send_header("Set-Cookie", "lan_identity=" + cookie + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(value)

        def identity(self, mark_seen=True):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                pass
            token = cookie.get("lan_identity")
            token = token.value if token else ""
            if len(token) > 128:
                token = ""
            ip = self.client_address[0]
            if ip == "127.0.0.1":
                ip = args.host
            me, current = store.identity(token, ip, self.headers.get("User-Agent", ""), mark_seen)
            return me, current, current if current != token else None

        def csrf(self, me):
            return hmac.new(secret, me.encode(), hashlib.sha256).hexdigest()

        def download(self, me, identifier, cookie):
            item = store.attachment(me, identifier)
            if not item:
                self.respond(404, {"error": "This file is not in your conversations."}, cookie=cookie)
                return
            try:
                source = item["path"].open("rb")
            except FileNotFoundError:
                self.respond(404, {"error": "The saved file is missing from the host computer."}, cookie=cookie)
                return
            with source:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(item["size"]))
                self.send_header("Content-Disposition", "attachment; filename=\"download\"; filename*=UTF-8''" + quote(item["name"], safe=""))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "same-origin")
                if cookie:
                    self.send_header("Set-Cookie", "lan_identity=" + cookie + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")
                self.end_headers()
                try:
                    while chunk := source.read(65536):
                        self.wfile.write(chunk)
                except (ConnectionError, OSError):
                    pass

        def do_GET(self):
            if not self.allowed():
                self.respond(403, {"error": "This messenger is available on the local network only."})
                return
            local = self.server.server_address[1] == args.local_port
            if local and self.path == "/api/admin":
                self.respond(200, {"token": admin_token})
                return
            if local and self.path == "/api/admin/migration":
                self.respond(200, migration.info() if migration else {"enabled": False})
                return
            if migration and migration.handle(self):
                return
            path = urlsplit(self.path)
            if path.path == "/health":
                self.respond(200, {"status": "ok", "app": "lan-messenger"})
                return
            if path.path == "/":
                self.respond(200, site, "text/html; charset=utf-8")
                return
            if path.path not in ("/api/state", "/api/history", "/api/discovery", "/api/file"):
                self.respond(404, {"error": "Not found."})
                return
            me, _, cookie = self.identity(mark_seen=path.path == "/api/state")
            try:
                if path.path == "/api/state":
                    data = store.state(me)
                    data.update(csrf=self.csrf(me), url=origin + "/")
                    if migration:
                        data["migration"] = migration.state(local, self.client_address[0])
                elif path.path == "/api/history":
                    query = parse_qs(path.query)
                    data = store.history(me, query.get("peer", [""])[0],
                                         int(query.get("before", ["0"])[0]))
                elif path.path == "/api/file":
                    identifier = int(parse_qs(path.query).get("id", ["0"])[0])
                    self.download(me, identifier, cookie)
                    return
                else:
                    data = {"addresses": discover(), "url": origin + "/"}
                self.respond(200, data, cookie=cookie)
            except ValueError as error:
                self.respond(400, {"error": str(error)}, cookie=cookie)

        def do_POST(self):
            local = self.server.server_address[1] == args.local_port
            expected_origin = local_origin if local else origin
            request_origin = self.headers.get("Origin")
            if not self.allowed() or (request_origin and request_origin != expected_origin):
                self.respond(403, {"error": "Open the main messenger page again and retry."})
                return
            if self.path == "/api/admin/stop":
                if local and hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), admin_token):
                    self.respond(200, {"ok": True})
                    stop.set()
                else:
                    self.respond(403, {"error": "Local control only."})
                return
            if self.path == "/api/admin/migration/resume":
                if local and migration and hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), admin_token):
                    migration.resume()
                    self.respond(200, {"ok": True})
                else:
                    self.respond(403, {"error": "Local control only."})
                return
            me, _, cookie = self.identity()
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.csrf(me)):
                self.respond(403, {"error": "Refresh the messenger page and retry."}, cookie=cookie)
                return
            path = urlsplit(self.path)
            if path.path == "/api/upload":
                try:
                    if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type") != "application/octet-stream":
                        raise ValueError("Invalid file upload.")
                    length = int(self.headers.get("Content-Length", "-1"))
                    query = parse_qs(path.query)
                    self.connection.settimeout(60)
                    identifier = store.attach(me, query.get("peer", [""])[0], query.get("name", [""])[0], length, self.rfile)
                    self.respond(200, {"ok": True, "id": identifier}, cookie=cookie)
                except ValueError as error:
                    try:
                        self.respond(400, {"error": str(error)}, cookie=cookie)
                    except OSError:
                        pass
                except (OSError, sqlite3.Error):
                    try:
                        self.respond(500, {"error": "Upload could not be saved. Check free disk space and retry."}, cookie=cookie)
                    except OSError:
                        pass
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1048576 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("Invalid request.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Invalid request.")
                if self.path == "/api/send":
                    identifier = store.send(me, data.get("peer"), data.get("text"))
                    result = {"ok": True, "id": identifier}
                elif self.path == "/api/read":
                    store.read(me, data.get("peer"), int(data.get("through", 0)))
                    result = {"ok": True}
                elif self.path == "/api/name":
                    store.rename(me, data.get("name"))
                    result = {"ok": True}
                else:
                    self.respond(404, {"error": "Not found."}, cookie=cookie)
                    return
                self.respond(200, result, cookie=cookie)
            except (ValueError, TypeError, UnicodeDecodeError) as error:
                self.respond(400, {"error": str(error)}, cookie=cookie)

    servers = []
    for attempt in range(24):
        try:
            servers.append(ThreadingHTTPServer((args.host, args.port), Handler))
            servers.append(ThreadingHTTPServer(("127.0.0.1", args.local_port), Handler))
            break
        except OSError:
            for server in servers:
                server.server_close()
            servers.clear()
            if not args.replace_transfer or attempt == 23:
                raise
            time.sleep(0.25)
    for server in servers:
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(json.dumps({"url": origin + "/", "local_url": local_origin + "/",
                      "network": str(network), "history": store.path, "lifetime": args.lifetime}), flush=True)
    try:
        stop.wait(args.lifetime if args.lifetime > 0 else None)
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
