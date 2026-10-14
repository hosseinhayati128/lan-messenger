"""Move this Ubuntu messenger's attachments to the explicitly selected ext4 drive."""
import argparse
import hashlib
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

UUID = "1aaae810-a2f3-4072-9b75-cb5067b069b6"
DRIVE = pathlib.Path("/run/media/h128") / UUID
STORAGE = DRIVE / "lan-messenger" / "uploads"
DATA = pathlib.Path("/var/lib/lan-messenger")
OLD = DATA / "uploads"
SYSTEMD = pathlib.Path("/etc/systemd/system")
UNIT = SYSTEMD / "lan-messenger.service"
DROPIN = SYSTEMD / "lan-messenger.service.d" / "storage.conf"
MARKER = "# Managed by LAN Messenger storage setup."


def run(arguments, **options):
    return subprocess.run(arguments, check=True, text=True, **options)


def capture(arguments):
    return run(arguments, capture_output=True).stdout.strip()


def mounted_drive():
    value = json.loads(capture(["findmnt", "--json", "--mountpoint", str(DRIVE),
                               "--output", "TARGET,SOURCE,FSTYPE,UUID,OPTIONS"]))
    entries = value.get("filesystems", [])
    if len(entries) != 1:
        raise ValueError("The selected drive must be mounted before setup.")
    entry = entries[0]
    if entry["target"] != str(DRIVE) or entry.get("uuid", "").lower() != UUID:
        raise ValueError("The mounted device does not match the selected drive UUID.")
    if entry.get("fstype") != "ext4" or "rw" not in entry.get("options", "").split(","):
        raise ValueError("The selected drive must be a writable ext4 filesystem.")
    return entry


def mount_text():
    return MARKER + "\n" + """[Unit]
Description=LAN Messenger storage drive
JobTimeoutSec=15s

[Mount]
What=/dev/disk/by-uuid/%s
Where=%s
Type=ext4
Options=rw,nosuid,nodev,relatime,errors=remount-ro
TimeoutSec=10s
DirectoryMode=0755
""" % (UUID, DRIVE)


def dropin_text(mount_unit):
    return MARKER + "\n" + """[Unit]
RequiresMountsFor=%s
BindsTo=%s
After=%s
ConditionPathIsMountPoint=%s

[Service]
BindPaths=%s:%s
ReadWritePaths=%s %s
UMask=0007
""" % (STORAGE, mount_unit, mount_unit, DRIVE, STORAGE, OLD, STORAGE, OLD)


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def copy_verified(source, destination, size):
    if source.is_symlink() or not source.is_file() or source.stat().st_size != size:
        raise ValueError("An existing attachment is missing or incomplete: " + source.name)
    checksum = digest(source)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file() or destination.stat().st_size != size or digest(destination) != checksum:
            raise ValueError("A different file already exists at the storage destination: " + destination.name)
        return checksum
    temporary = destination.with_name(destination.name + ".moving-" + str(time.time_ns()))
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if temporary.stat().st_size != size or digest(temporary) != checksum:
            raise ValueError("Attachment verification failed; the original was kept.")
        # ext4 hard-link creation refuses to overwrite an existing destination.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return checksum


def write_config(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new-" + str(time.time_ns()))
    try:
        temporary.write_bytes(content)
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def verify_service():
    pid = int(capture(["systemctl", "show", "lan-messenger.service", "--property=MainPID", "--value"]))
    if pid <= 0:
        raise ValueError("The messenger service has not started.")
    inside = pathlib.Path("/proc") / str(pid) / "root" / OLD.relative_to("/")
    if not inside.samefile(STORAGE):
        raise ValueError("The service is not using the selected drive; original files were kept.")
    # Exercise the exact service mount namespace and account, without adding chat messages.
    probe_code = ("import pathlib,secrets; p=pathlib.Path('/var/lib/lan-messenger/uploads')/"
                  "('.storage-probe-'+secrets.token_hex(12)); "
                  "p.write_bytes(b'LAN Messenger storage check'); "
                  "assert p.read_bytes()==b'LAN Messenger storage check'; p.unlink()")
    run(["nsenter", "--target", str(pid), "--mount", "--root", "--wd=/", "--", "runuser", "--user", "lan-messenger", "--",
         "/usr/bin/python3", "-B", "-c", probe_code])
    opener = build_opener(ProxyHandler({}))
    with opener.open("http://127.0.0.1:8766/health", timeout=3) as response:
        if json.load(response).get("app") != "lan-messenger":
            raise ValueError("The restarted service did not pass its health check.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print("Files: " + str(STORAGE))
        print(dropin_text("STORAGE-MOUNT.mount"))
        return
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        raise SystemExit("Run on Ubuntu with: sudo python3 -B configure_storage.py")
    for command in ("systemctl", "systemd-escape", "systemd-analyze", "findmnt", "nsenter", "runuser"):
        if not shutil.which(command):
            raise SystemExit("A required Ubuntu command is unavailable: " + command)
    import fcntl
    with pathlib.Path("/run/lock/lan-messenger-storage.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        configure()


def configure():
    mounted_drive()
    if not UNIT.is_file() or "Managed by the LAN Messenger Ubuntu installer." not in UNIT.read_text():
        raise ValueError("This update needs the existing LAN Messenger Ubuntu service.")
    mount_unit = capture(["systemd-escape", "--path", "--suffix=mount", str(DRIVE)])
    if "/" in mount_unit or not mount_unit.endswith(".mount"):
        raise ValueError("Unexpected mount unit name.")
    mount_path = SYSTEMD / mount_unit
    fragment = capture(["systemctl", "show", mount_unit, "--property=FragmentPath", "--value"])
    # Keep existing /etc/fstab-generated or permanent mount configuration intact.
    persistent = bool(fragment and (fragment.startswith(("/etc/", "/usr/", "/lib/")) or
                      fragment.startswith("/run/systemd/generator")))
    if mount_path.exists():
        persistent = True
    previous_dropin = DROPIN.read_bytes() if DROPIN.exists() else None
    expected = dropin_text(mount_unit).encode()
    if previous_dropin is not None:
        if previous_dropin == expected:
            verify_service()
            print("Storage is already configured: " + str(STORAGE))
            return
        raise ValueError("A storage override already exists; it was left unchanged.")
    if OLD.is_symlink():
        raise ValueError("The current upload folder is a symlink; it was left unchanged.")
    import pwd
    account = pwd.getpwnam("lan-messenger")
    storage_group = pwd.getpwnam("h128").pw_gid
    for path in (STORAGE.parent, STORAGE):
        if path.is_symlink():
            raise ValueError("The target storage directory must not be a symlink.")
        path.mkdir(exist_ok=True)
        os.chown(path, account.pw_uid, storage_group)
        path.chmod(0o2770)
    print("Stopping the messenger briefly to move existing uploaded files…", flush=True)
    run(["systemctl", "stop", "lan-messenger.service"])
    created_mount = False
    switched = False
    try:
        mounted_drive()
        with closing(sqlite3.connect(DATA / "history.sqlite3")) as db:
            files = db.execute("SELECT storage_key,size FROM attachments").fetchall()
            backup = DATA / ("history-before-storage-" + str(time.time_ns()) + ".sqlite3")
            with closing(sqlite3.connect(backup)) as target:
                db.backup(target)
        required = sum(size for key, size in files if not (STORAGE / key).exists())
        if shutil.disk_usage(STORAGE).free < required:
            raise ValueError("The selected drive does not have enough free space for the existing files.")
        checksums = {}
        for index, (key, size) in enumerate(files, 1):
            if not re.fullmatch(r"[a-f0-9]{48}\.bin", key):
                raise ValueError("Unexpected attachment storage name; no original was removed.")
            print("Verifying file %d/%d (%d bytes)…" % (index, len(files), size), flush=True)
            checksums[key] = copy_verified(OLD / key, STORAGE / key, size)
            os.chown(STORAGE / key, account.pw_uid, storage_group)
            (STORAGE / key).chmod(0o660)
        if not persistent:
            write_config(mount_path, mount_text().encode())
            created_mount = True
        write_config(DROPIN, expected)
        run(["systemd-analyze", "verify", str(UNIT)])
        run(["systemctl", "daemon-reload"])
        # After a start attempt, keep this mapping on failure: new files may already have reached the drive.
        switched = True
        run(["systemctl", "start", "lan-messenger.service"])
        for attempt in range(30):
            try:
                verify_service()
                break
            except (OSError, ValueError, subprocess.CalledProcessError):
                if attempt == 29:
                    raise
                time.sleep(1)
        helper = pathlib.Path("/opt/lan-messenger/configure_storage.py")
        if pathlib.Path(__file__).resolve() != helper:
            shutil.copyfile(__file__, helper)
            helper.chmod(0o644)
        write_config(DATA / "storage.json", json.dumps(dict(uploads=str(STORAGE), uuid=UUID), indent=2).encode())
        # The service now writes only to the drive. Remove only byte-verified committed originals.
        removed = 0
        for key, checksum in checksums.items():
            source = OLD / key
            if source.is_file() and not source.is_symlink() and digest(source) == checksum:
                source.unlink()
                removed += 1
        print("Storage configured successfully.", flush=True)
        print("Uploaded files: " + str(STORAGE))
        print("Existing files verified: %d; old copies removed: %d." % (len(files), removed))
        print("Chat history remains in /var/lib/lan-messenger. The service will use the drive after reboot.")
    except BaseException:
        if not switched:
            DROPIN.unlink(missing_ok=True)
            if created_mount:
                mount_path.unlink(missing_ok=True)
            run(["systemctl", "daemon-reload"])
            run(["systemctl", "start", "lan-messenger.service"])
        else:
            print("The drive mapping is installed. Original files were kept if verification did not finish. "
                  "Check: sudo journalctl -u lan-messenger -n 30 --no-pager", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit("Storage update stopped: " + str(error))
