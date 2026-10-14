"""Embed the reviewed updater for delivery by the already-running private setup URL."""
import base64
import pathlib
import re

root = pathlib.Path(__file__).parent
payload = base64.b64encode((root / "configure_storage.py").read_bytes()).decode("ascii")
target = root / "ubuntu_bootstrap.py"
source = target.read_text(encoding="utf-8")
source, count = re.subn(r'^STORAGE_SETUP_SOURCE_B64 = "[A-Za-z0-9+/=]*"$',
                       'STORAGE_SETUP_SOURCE_B64 = "' + payload + '"', source, flags=re.MULTILINE)
assert count == 1
target.write_text(source, encoding="utf-8")
print("Storage updater packaged for the private Ubuntu setup endpoint.")
