"""Regression checks for upgrading browser identities to computer identities."""
import hashlib
import pathlib
import sqlite3
import tempfile
import time
from lan_messenger import Store

root = pathlib.Path(__file__).parent
folder = pathlib.Path(tempfile.mkdtemp(prefix="qa-identity-", dir=root))
path = folder / "history.sqlite3"
store = Store(path)
tokens = {"win-a": "token-a", "win-b": "token-b", "ubuntu-a": "token-c", "ubuntu-b": "token-d"}
now = time.time()
with store.connect() as db:
    for offset, (identifier, name, ip) in enumerate((
        ("win-a", "Windows PC", "192.168.128.230"),
        ("win-b", "My laptop", "192.168.128.230"),
        ("ubuntu-a", "Mehdi", "192.168.128.96"),
        ("ubuntu-b", "Ubuntu / Linux", "192.168.128.96"),
    )):
        digest = hashlib.sha256(tokens[identifier].encode()).hexdigest()
        db.execute("INSERT INTO devices VALUES(?,?,?,?,?,?)",
                   (identifier, name, digest, ip, now + offset, now + offset))
    for sender, recipient, body in (
        ("win-b", "ubuntu-a", "First browser's message"),
        ("ubuntu-b", "win-a", "Reply from another browser"),
        ("win-b", "win-a", "A message between Windows browser entries"),
    ):
        db.execute("INSERT INTO messages(sender,recipient,body,sent) VALUES(?,?,?,?)",
                   (sender, recipient, body, now))

store = Store(path)
with store.connect() as db:
    assert db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM device_sessions").fetchone()[0] == 4
assert len(list(folder.glob("history-before-device-merge-*.sqlite3"))) == 1
win = store.identity(tokens["win-a"], "192.168.128.230", "Windows")[0]
win2 = store.identity(tokens["win-b"], "192.168.128.230", "Windows")[0]
ubuntu = store.identity(tokens["ubuntu-a"], "192.168.128.96", "Linux")[0]
ubuntu2 = store.identity(tokens["ubuntu-b"], "192.168.128.96", "Linux")[0]
assert win == win2 and ubuntu == ubuntu2 and win != ubuntu
assert len(store.history(win, ubuntu)["messages"]) == 2
assert len(store.history(win, "ubuntu-b")["messages"]) == 2
assert len(store.history(win, win)["messages"]) == 1
assert any(item.get("is_self") for item in store.state(win)["devices"])
assert store.state(win)["me"]["name"] == "My laptop"
assert store.state(ubuntu)["me"]["name"] == "Mehdi"
for _ in range(5):
    assert store.identity("", "192.168.128.230", "Another browser")[0] == win
    assert store.identity("", "192.168.128.96", "Another browser")[0] == ubuntu
with store.connect() as db:
    assert db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 2
assert store.identity(tokens["ubuntu-a"], "192.168.128.52", "Linux")[0] == ubuntu
assert store.state(ubuntu)["me"]["ip"] == "192.168.128.52"
fresh = store.identity("", "192.168.128.80", "Windows")[0]
assert fresh not in (win, ubuntu)
assert store.history(fresh, win)["messages"] == []
assert store.history(fresh, ubuntu)["messages"] == []
store = Store(path)
assert store.identity(tokens["win-b"], "192.168.128.230", "Windows")[0] == win
assert len(store.history(win, ubuntu)["messages"]) == 2
print("PASS: duplicate migration, backups, all message history, custom names, existing cookies, stale recipient IDs, same-IP rejoining, new addresses, and restart persistence.")
