"""Storage transfer integrity, non-overwrite behavior, mount checks, and private binding."""
import base64
import io
import json
import pathlib
import re
import sys
import tempfile
from unittest.mock import Mock, patch
import configure_storage as storage

root = pathlib.Path(__file__).parent
valid = dict(target=str(storage.DRIVE), source="/dev/sda1", fstype="ext4", uuid=storage.UUID,
             options="rw,nosuid,nodev,relatime,errors=remount-ro")
with patch("configure_storage.capture", return_value=json.dumps(dict(filesystems=[valid]))):
    assert storage.mounted_drive()["source"] == "/dev/sda1"
for changes in (dict(uuid="wrong"), dict(fstype="ntfs"), dict(target="/"), dict(options="ro,nosuid")):
    with patch("configure_storage.capture", return_value=json.dumps(dict(filesystems=[{**valid, **changes}]))):
        try:
            storage.mounted_drive()
        except ValueError:
            pass
        else:
            raise AssertionError("A missing, different or read-only drive was accepted")
with tempfile.TemporaryDirectory(prefix="qa-storage-", dir=root) as folder:
    directory = pathlib.Path(folder)
    original, saved = directory / "original.bin", directory / "saved.bin"
    data = bytes(range(256)) * 32768 + b"end"
    original.write_bytes(data)
    checksum = storage.copy_verified(original, saved, len(data))
    assert saved.read_bytes() == data and original.read_bytes() == data
    assert storage.copy_verified(original, saved, len(data)) == checksum
    saved.write_bytes(b"keep this different destination")
    try:
        storage.copy_verified(original, saved, len(data))
    except ValueError:
        pass
    else:
        raise AssertionError("A different existing destination was overwritten")
    assert saved.read_bytes() == b"keep this different destination"
    assert original.read_bytes() == data
    saved.unlink()
    with patch("configure_storage.shutil.copyfileobj", side_effect=OSError("disk write failed")):
        try:
            storage.copy_verified(original, saved, len(data))
        except OSError:
            pass
        else:
            raise AssertionError("A failed copy was accepted")
    assert original.read_bytes() == data and not saved.exists()
    assert not list(directory.glob("*.moving-*"))
    config = directory / "drop-in" / "storage.conf"
    unit = storage.dropin_text("drive.mount")
    storage.write_config(config, unit.encode())
    assert config.read_text() == unit
    assert "ConditionPathIsMountPoint=" + str(storage.DRIVE) in unit
    assert "BindsTo=drive.mount" in unit and "RequiresMountsFor=" in unit
    assert "BindPaths=" + str(storage.STORAGE) + ":" + str(storage.OLD) in unit
    assert "UMask=0007" in unit
    assert storage.UUID in storage.mount_text() and "Type=ext4" in storage.mount_text()
bootstrap = (root / "ubuntu_bootstrap.py").read_text(encoding="utf-8")
match = re.search(r'^STORAGE_SETUP_SOURCE_B64 = "([A-Za-z0-9+/=]+)"$', bootstrap, re.MULTILINE)
assert match and base64.b64decode(match[1]) == (root / "configure_storage.py").read_bytes()
compile(base64.b64decode(match[1]), "configure_storage.py", "exec")
bootstrap = bootstrap.replace("__BUNDLE_URL__", repr("http://unused.invalid/bundle.tar"))
bootstrap = bootstrap.replace("__COMPLETE_URL__", repr("http://unused.invalid/complete"))
work = pathlib.Path(tempfile.mkdtemp(prefix="qa-storage-bootstrap-", dir=root)).resolve()
assert work.parent == root.resolve()
def elevated_update(arguments, **options):
    assert arguments[:3] == ["sudo", sys.executable, "-B"] and options["check"]
    assert pathlib.Path(arguments[3]).read_bytes() == (root / "configure_storage.py").read_bytes()
try:
    with patch("sys.platform", "linux"), patch("sys.argv", ["-c", "--configure-storage"]), \
         patch("shutil.which", return_value="/usr/bin/tool"), \
         patch("tempfile.mkdtemp", return_value=str(work)), \
         patch("subprocess.run", side_effect=elevated_update) as elevated, \
         patch("urllib.request.build_opener") as network:
        try:
            exec(compile(bootstrap, "ubuntu_bootstrap.py", "exec"), {})
        except SystemExit as result:
            assert result.code == 0
        else:
            raise AssertionError("Storage mode continued into the original migration")
        elevated.assert_called_once()
        network.assert_not_called()
    assert not work.exists()
finally:
    if work.exists():
        import shutil
        assert work.parent == root.resolve()
        shutil.rmtree(work)
print("PASS: verified binary copies and retries; originals survive copy/disk errors; conflicting files are preserved; drive UUID/filesystem/read-only checks; persistent mount and service binding configuration; packaged updater matches source.")
