"""Install the local messenger as an Ubuntu service; no pip or Internet needed."""
import argparse
import ipaddress
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from urllib.request import ProxyHandler, build_opener
from lan_platform import detect_host
from ubuntu_migration import PROGRAM_FILES

APP = pathlib.Path("/opt/lan-messenger")
DATA = pathlib.Path("/var/lib/lan-messenger")
UNIT = pathlib.Path("/etc/systemd/system/lan-messenger.service")
OWNER = "lan-messenger"


def service_text(network):
    return """# Managed by the LAN Messenger Ubuntu installer.
[Unit]
Description=LAN Messenger
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=lan-messenger
Group=lan-messenger
WorkingDirectory=/opt/lan-messenger
ExecStart=/usr/bin/python3 -u /opt/lan-messenger/lan_messenger.py --network %s --data-dir /var/lib/lan-messenger
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/lan-messenger

[Install]
WantedBy=multi-user.target
""" % network


def validate_data(source):
    database = source / "history.sqlite3"
    if not database.is_file():
        return
    with closing(sqlite3.connect(database)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("The transferred chat database failed validation.")
        for key, size in db.execute("SELECT storage_key,size FROM attachments"):
            if not re.fullmatch(r"[a-f0-9]{48}\.bin", key) or (source / "uploads" / key).stat().st_size != size:
                raise ValueError("The transfer has a missing or incomplete attachment.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parent
    configuration = json.loads((root / "migration.json").read_text()) if (root / "migration.json").exists() else {}
    network = str(ipaddress.ip_network(args.network or configuration.get("network", "192.168.128.0/24"), strict=False))
    if args.dry_run:
        print(service_text(network))
        return
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        raise SystemExit("Run on Ubuntu with: sudo python3 install_ubuntu.py")
    if not pathlib.Path("/run/systemd/system").is_dir():
        raise SystemExit("Ubuntu must be running systemd to install the service.")
    for name in PROGRAM_FILES:
        if not (root / name).is_file():
            raise SystemExit("The program transfer is incomplete: " + name)
    validate_data(root / "messenger-data")
    if (DATA / "history.sqlite3").exists():
        raise SystemExit("A messenger database already exists in /var/lib/lan-messenger. It was left unchanged; contact us before replacing it.")
    if UNIT.exists() and "Managed by the LAN Messenger Ubuntu installer." not in UNIT.read_text():
        raise SystemExit("A different service already uses lan-messenger.service; it was left unchanged.")
    host = detect_host(network)
    import pwd
    import grp
    try:
        account = pwd.getpwnam(OWNER)
        if account.pw_uid == 0 or account.pw_shell not in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false"):
            raise SystemExit("The service account name is already used by a login account; no files were replaced.")
    except KeyError:
        subprocess.run(["useradd", "--system", "--user-group", "--home-dir", str(DATA),
                        "--shell", "/usr/sbin/nologin", OWNER], check=True)
        account = pwd.getpwnam(OWNER)
    group = grp.getgrnam(OWNER).gr_gid
    APP.mkdir(parents=True, exist_ok=True)
    for name in PROGRAM_FILES:
        shutil.copyfile(root / name, APP / name)
        os.chmod(APP / name, 0o755 if name.endswith(".sh") else 0o644)
    DATA.mkdir(parents=True, exist_ok=True)
    os.chown(DATA, account.pw_uid, group)
    os.chmod(DATA, 0o700)
    source = root / "messenger-data"
    if source.exists():
        # Move on the same filesystem where possible, avoiding another full attachment copy.
        for name in ("history.sqlite3", "uploads"):
            if (source / name).exists():
                if (DATA / name).exists():
                    raise SystemExit("The storage directory already contains " + name + "; it was left unchanged.")
                shutil.move(str(source / name), str(DATA / name))
    for directory, subdirectories, files in os.walk(DATA):
        os.chown(directory, account.pw_uid, group)
        os.chmod(directory, 0o700)
        for name in files:
            path = pathlib.Path(directory) / name
            os.chown(path, account.pw_uid, group)
            os.chmod(path, 0o600)
    UNIT.write_text(service_text(network), encoding="utf-8")
    if shutil.which("ufw"):
        status = subprocess.run(["ufw", "status"], capture_output=True, text=True,
                                env={**os.environ, "LC_ALL": "C"}, check=True)
        if "Status: active" in status.stdout:
            subprocess.run(["ufw", "allow", "proto", "tcp", "from", network,
                            "to", "any", "port", "8765", "comment", "LAN Messenger"], check=True)
    print("Starting the messenger and enabling it at boot…", flush=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "lan-messenger.service"], check=True)
    opener = build_opener(ProxyHandler({}))
    for attempt in range(40):
        try:
            with opener.open("http://" + host + ":8765/health", timeout=1) as response:
                assert json.load(response)["app"] == "lan-messenger"
            break
        except (OSError, AssertionError):
            time.sleep(1)
    else:
        raise SystemExit("The service did not respond. Run: sudo journalctl -u lan-messenger -n 30 --no-pager")
    print("Running at http://" + host + ":8765/", flush=True)
    print("Chat history and uploaded files are stored in /var/lib/lan-messenger.", flush=True)


if __name__ == "__main__":
    main()
