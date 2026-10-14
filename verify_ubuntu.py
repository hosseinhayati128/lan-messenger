"""Portable network selection and complete handover with synthetic history."""
import hashlib
import io
import ipaddress
import json
import pathlib
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from unittest.mock import patch
from lan_messenger import Store
from lan_platform import detect_host, detect_network, network_neighbors
from ubuntu_migration import Migration, PROGRAM_FILES
from install_ubuntu import service_text

root = pathlib.Path(__file__).parent
addresses = [
    dict(ifname="tun0", addr_info=[dict(local="10.8.0.2", prefixlen=24, scope="global")]),
    dict(ifname="eno1", addr_info=[dict(local="192.168.128.52", prefixlen=24, scope="global")]),
]
with patch("lan_platform.sys.platform", "linux"), patch("lan_platform.linux_addresses", return_value=addresses):
    assert detect_host() == "192.168.128.52"
    assert detect_host("192.168.128.0/24") == "192.168.128.52"
    assert str(detect_network("192.168.128.52")) == "192.168.128.0/24"
with patch("lan_platform.sys.platform", "linux"), patch("lan_platform.run", return_value="192.168.128.230 dev eno1 lladdr aa:bb:cc:dd:ee:ff STALE\n192.168.128.96 dev eno1 FAILED\n10.8.0.1 dev tun0 REACHABLE") as command:
    assert network_neighbors("192.168.128.52", ipaddress.ip_network("192.168.128.0/24")) == ["192.168.128.230"]
    assert command.call_args.args[0] == ["ip", "-4", "neigh", "show"]
unit = service_text("192.168.128.0/24")
assert "WantedBy=multi-user.target" in unit and "User=lan-messenger" in unit
assert "Restart=on-failure" in unit and "--data-dir /var/lib/lan-messenger" in unit
assert "Windows" not in (root / "messenger.html").read_text(encoding="utf-8").split("<script>")[0].split("<aside id=\"migration-notice\"")[0]

with tempfile.TemporaryDirectory(prefix="qa-ubuntu-", dir=root) as folder:
    source = Store(pathlib.Path(folder) / "source" / "history.sqlite3")
    windows, token = source.identity("", "192.168.128.230", "Windows")
    ubuntu, _ = source.identity("", "192.168.128.52", "Linux")
    source.send(windows, ubuntu, "Saved before changing hosts")
    payload = bytes(range(256)) * 4096
    file_id = source.attach(ubuntu, windows, "sample.bin", len(payload), io.BytesIO(payload))
    migration = Migration(source, root, "192.168.128.52", "192.168.128.0/24", "http://192.168.128.230:8765")
    assert migration.state(False, "192.168.128.96").get("command") is None
    assert migration.state(False, "192.168.128.52")["command"]
    assert not migration.info()["paused"]
    assert all((root / name).exists() for name in PROGRAM_FILES)
    with migration.snapshot() as (database, attachments):
        assert source.moving_to == "http://192.168.128.52:8765/"
        assert not database.with_name(database.name + "-wal").exists()
        try:
            source.send(windows, ubuntu, "This would be lost after the snapshot")
        except ValueError:
            pass
        else:
            raise AssertionError("The source accepted a new message after cutover")
        archive_bytes = io.BytesIO()
        migration.write_bundle(archive_bytes, database, attachments)
    assert not list(pathlib.Path(source.path).parent.glob("migration-*"))
    archive_bytes.seek(0)
    unpacked = pathlib.Path(folder) / "destination"
    with tarfile.open(fileobj=archive_bytes, mode="r:") as archive:
        for member in archive:
            destination = unpacked.joinpath(*pathlib.PurePosixPath(member.name).parts)
            assert member.isfile() and ".." not in pathlib.PurePosixPath(member.name).parts
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as received, destination.open("wb") as output:
                output.write(received.read())
    application = unpacked / "lan-messenger"
    transferred = Store(application / "messenger-data" / "history.sqlite3")
    assert transferred.identity(token, "192.168.128.230", "Windows")[0] == windows
    assert transferred.identity("", "192.168.128.52", "Linux")[0] == ubuntu
    history = transferred.history(windows, ubuntu)["messages"]
    assert len(history) == 2 and history[0]["body"] == "Saved before changing hosts"
    assert hashlib.sha256(transferred.attachment(windows, file_id)["path"].read_bytes()).digest() == hashlib.sha256(payload).digest()
    config = json.loads((application / "migration.json").read_text())
    assert config["host"] == "192.168.128.52"
    with closing(sqlite3.connect(transferred.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 2
    migration.resume()
    source.send(windows, ubuntu, "A cancelled move resumes the source")
    assert not migration.info()["paused"]
bootstrap = (root / "ubuntu_bootstrap.py").read_text().replace("__BUNDLE_URL__", repr("http://127.0.0.1/bundle.tar")).replace("__COMPLETE_URL__", repr("http://127.0.0.1/complete"))
compile(bootstrap, "bootstrap", "exec")
print("PASS: Ubuntu adapter and neighbor discovery; boot service configuration; streamed handover with all messages, binary files and computer identities; paused source and cancel recovery.")
