"""A private, streamed handover to an explicitly selected LAN computer."""
import io
import json
import pathlib
import re
import secrets
import sqlite3
import tarfile
import tempfile
import threading
from contextlib import closing, contextmanager

PROGRAM_FILES = ("lan_messenger.py", "lan_platform.py", "ubuntu_migration.py", "ubuntu_bootstrap.py",
                 "messenger.html", "install_ubuntu.py", "start-ubuntu.sh", "LAN Messenger README.md")


class Migration:
    def __init__(self, store, root, target, network, origin):
        self.store, self.root, self.target = store, root, target
        self.network, self.origin = network, origin
        self.token = secrets.token_urlsafe(32)
        self.base = origin + "/setup/" + self.token
        self.destination = "http://" + target + ":8765/"
        self.complete = False
        self.export_lock = threading.Lock()

    def info(self):
        command = ("python3 -c \"import urllib.request; "
                   "exec(urllib.request.build_opener(urllib.request.ProxyHandler({})).open('" +
                   self.base + "/install.py').read())\"")
        return dict(enabled=True, command=command, target=self.destination,
                    paused=bool(self.store.moving_to), completed=self.complete)

    def state(self, local, ip=None):
        value = dict(target=self.destination, paused=bool(self.store.moving_to),
                     completed=self.complete, local=local)
        if (local or ip == self.target) and not self.complete:
            value["command"] = self.info()["command"]
        return value

    def resume(self):
        with self.store.lock:
            self.store.moving_to = None
            self.complete = False

    @contextmanager
    def snapshot(self):
        # Only the small SQLite snapshot is duplicated; attachments stream from their original paths.
        with tempfile.TemporaryDirectory(prefix="migration-", dir=pathlib.Path(self.store.path).parent) as folder:
            database = pathlib.Path(folder) / "history.sqlite3"
            with self.store.lock:
                self.store.moving_to = self.destination
                try:
                    with self.store.connect() as source, closing(sqlite3.connect(database)) as target:
                        source.backup(target)
                except BaseException:
                    self.store.moving_to = None
                    raise
            with closing(sqlite3.connect(database)) as db:
                db.execute("PRAGMA journal_mode=DELETE")
                attachments = db.execute("SELECT storage_key,size FROM attachments").fetchall()
                # Online state belongs to the old process, not the transferred server.
                db.execute("UPDATE devices SET seen=0")
                db.commit()
            try:
                for key, size in attachments:
                    if not re.fullmatch(r"[a-f0-9]{48}\.bin", key) or (self.store.uploads / key).stat().st_size != size:
                        raise ValueError("An attachment is missing or incomplete; the transfer was not started.")
                for name in PROGRAM_FILES:
                    if not (self.root / name).is_file():
                        raise ValueError("A required program file is missing: " + name)
            except BaseException:
                self.resume()
                raise
            yield database, attachments

    def write_bundle(self, output, database, attachments):
        with tarfile.open(fileobj=output, mode="w|") as archive:
            for name in PROGRAM_FILES:
                archive.add(self.root / name, arcname="lan-messenger/" + name, recursive=False)
            archive.add(database, arcname="lan-messenger/messenger-data/history.sqlite3", recursive=False)
            for key, _ in attachments:
                archive.add(self.store.uploads / key, arcname="lan-messenger/messenger-data/uploads/" + key, recursive=False)
            config = json.dumps(dict(host=self.target, network=self.network, port=8765)).encode()
            entry = tarfile.TarInfo("lan-messenger/migration.json")
            entry.size = len(config)
            entry.mode = 0o600
            archive.addfile(entry, io.BytesIO(config))

    def handle(self, handler):
        paths = {"/setup/" + self.token + suffix: suffix for suffix in ("/install.py", "/bundle.tar", "/complete")}
        suffix = paths.get(handler.path)
        if suffix is None:
            return False
        if handler.client_address[0] != self.target:
            handler.respond(403, {"error": "Run the setup on the selected Ubuntu computer."})
            return True
        if suffix == "/install.py":
            source = (self.root / "ubuntu_bootstrap.py").read_text(encoding="utf-8")
            source = source.replace("__BUNDLE_URL__", repr(self.base + "/bundle.tar"))
            source = source.replace("__COMPLETE_URL__", repr(self.base + "/complete"))
            handler.respond(200, source, "text/plain; charset=utf-8")
        elif suffix == "/complete":
            if not self.store.moving_to:
                handler.respond(409, {"error": "No transfer is in progress."})
            else:
                self.complete = True
                handler.respond(200, {"ok": True, "url": self.destination})
        elif not self.export_lock.acquire(blocking=False):
            handler.respond(409, {"error": "A transfer is already in progress."})
        else:
            try:
                with self.snapshot() as (database, attachments):
                    handler.send_response(200)
                    handler.send_header("Content-Type", "application/x-tar")
                    handler.send_header("Content-Disposition", 'attachment; filename="lan-messenger-ubuntu.tar"')
                    handler.send_header("Cache-Control", "no-store")
                    handler.send_header("X-Content-Type-Options", "nosniff")
                    handler.send_header("Connection", "close")
                    handler.end_headers()
                    handler.close_connection = True
                    self.write_bundle(handler.wfile, database, attachments)
            except (ConnectionError, OSError, ValueError, sqlite3.Error):
                # Keep the source paused after a started transfer so retries cannot lose newer chats.
                # The local owner can explicitly cancel the move to resume normal operation.
                handler.close_connection = True
            finally:
                self.export_lock.release()
        return True
