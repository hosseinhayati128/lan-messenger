"""Regression: sudo-created caches must not turn installation success into failure."""
import contextlib
import io
import json
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import Mock, patch

root = pathlib.Path(__file__).resolve().parent
source = (root / "ubuntu_bootstrap.py").read_text(encoding="utf-8")
source = source.replace("__BUNDLE_URL__", repr("http://example.invalid/bundle.tar"))
source = source.replace("__COMPLETE_URL__", repr("http://example.invalid/complete"))
archive = io.BytesIO()
with tarfile.open(fileobj=archive, mode="w") as package:
    member = tarfile.TarInfo("lan-messenger/install_ubuntu.py")
    content = b"# synthetic installer\n"
    member.size = len(content)
    package.addfile(member, io.BytesIO(content))

for installer_fails in (False, True):
    workspace = pathlib.Path(tempfile.mkdtemp(prefix="qa-bootstrap-", dir=root)).resolve()
    assert workspace.parent == root
    output, errors = io.StringIO(), io.StringIO()
    opener = Mock()
    opener.open.side_effect = lambda url, **unused: io.BytesIO(
        archive.getvalue() if url.endswith("bundle.tar") else json.dumps({"url": "http://192.168.128.52:8765/"}).encode())
    installation_error = subprocess.CalledProcessError(1, ["sudo", "python3", "-B", "install_ubuntu.py"])
    try:
        with patch("sys.platform", "linux"), patch("shutil.which", return_value="/usr/bin/tool"), \
             patch("tempfile.mkdtemp", return_value=str(workspace)), \
             patch("urllib.request.build_opener", return_value=opener), \
             patch("subprocess.run", side_effect=installation_error if installer_fails else None) as install, \
             patch("shutil.rmtree", side_effect=PermissionError("root-owned __pycache__")), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                exec(compile(source, "ubuntu_bootstrap.py", "exec"), {})
            except subprocess.CalledProcessError as error:
                assert installer_fails and error is installation_error
            else:
                assert not installer_fails
        arguments = install.call_args.args[0]
        assert arguments[:3] == ["sudo", sys.executable, "-B"]
        assert "result shown above is unchanged" in errors.getvalue()
        assert ("Migration complete" in output.getvalue()) == (not installer_fails)
        assert opener.open.call_count == (1 if installer_fails else 2)
    finally:
        # This is the verified, freshly created test directory only.
        assert workspace.resolve().parent == root
        shutil.rmtree(workspace)
print("PASS: elevated installer disables bytecode writes; cleanup permission errors preserve success and the original installation failure.")
