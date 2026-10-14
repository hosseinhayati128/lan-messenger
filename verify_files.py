"""Streaming storage checks using isolated synthetic data only."""
import hashlib
import io
import pathlib
import tempfile
from unittest.mock import patch
from lan_messenger import Store

root = pathlib.Path(__file__).parent
with tempfile.TemporaryDirectory(prefix="qa-files-", dir=root) as directory:
    database = pathlib.Path(directory) / "history.sqlite3"
    store = Store(database)
    alice, cookie = store.identity("", "192.168.1.2", "Windows")
    bob, _ = store.identity("", "192.168.1.3", "Linux")
    charlie, _ = store.identity("", "192.168.1.4", "Linux")

    class StreamingReader:
        def __init__(self, size):
            self.remaining = size
            self.largest_read = 0
            self.digest = hashlib.sha256()

        def read(self, count):
            self.largest_read = max(count, self.largest_read)
            count = min(count, self.remaining)
            self.remaining -= count
            chunk = b"x" * count
            self.digest.update(chunk)
            return chunk

    # Above the formerly proposed limit, streamed without a large memory allocation.
    size = 128 * 1024 * 1024 + 17
    reader = StreamingReader(size)
    message = store.attach(alice, bob, "../../large.bin", size, reader)
    assert reader.remaining == 0 and reader.largest_read <= 65536
    item = store.attachment(bob, message)
    assert item["name"] == "large.bin" and item["size"] == size
    digest = hashlib.sha256()
    with item["path"].open("rb") as source:
        while chunk := source.read(65536):
            digest.update(chunk)
    assert digest.hexdigest() == reader.digest.hexdigest()
    assert store.attachment(charlie, message) is None
    before = set(store.uploads.iterdir())
    for broken, length in ((io.BytesIO(b"partial"), 100), (io.BytesIO(), -1)):
        try:
            store.attach(alice, bob, "interrupted.bin", length, broken)
        except ValueError:
            pass
        else:
            raise AssertionError("An incomplete upload was accepted")
        assert set(store.uploads.iterdir()) == before
    with patch("pathlib.Path.open", side_effect=OSError("disk full")):
        try:
            store.attach(alice, bob, "failure.bin", 3, io.BytesIO(b"abc"))
        except OSError:
            pass
        else:
            raise AssertionError("A failed disk write was accepted")
    assert set(store.uploads.iterdir()) == before
    store = Store(database)
    assert store.identity(cookie, "192.168.1.2", "Windows")[0] == alice
    assert store.attachment(bob, message)["path"].stat().st_size == size
    assert store.history(bob, alice)["messages"][0]["attachment"]["size"] == size
    assert store.state(bob)["devices"][0]["preview"] == "📎 large.bin"
    # Identity merging must preserve the attachment as well as its message.
    renamed, _ = store.identity("", "192.168.1.5", "Windows")
    store.identity(cookie, "192.168.1.5", "Windows")
    assert store.attachment(renamed, message)["path"].stat().st_size == size
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
print("PASS: 128 MB upload with bounded memory and no app size limit; exact bytes, private download, interrupted/disk-error cleanup, restart and merged-identity persistence.")
