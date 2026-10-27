"""Tests for storage backend protocol."""

import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "tests")


class LocalStorageBackendTests(unittest.TestCase):
    def test_upload_download_delete(self):
        from gaeclip.storage_backend import LocalStorageBackend

        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(Path(tmp))
            data = b"hello world"
            backend.upload("foo/bar.txt", io.BytesIO(data), len(data))
            self.assertTrue(backend.exists("foo/bar.txt"))
            stream, size = backend.download("foo/bar.txt")
            self.assertEqual(size, len(data))
            self.assertEqual(stream.read(), data)
            stream.close()
            backend.delete("foo/bar.txt")
            self.assertFalse(backend.exists("foo/bar.txt"))

    def test_list_prefix(self):
        from gaeclip.storage_backend import LocalStorageBackend

        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(Path(tmp))
            backend.upload("a/x.txt", io.BytesIO(b"x"), 1)
            backend.upload("a/y.txt", io.BytesIO(b"y"), 1)
            backend.upload("b/z.txt", io.BytesIO(b"z"), 1)
            keys = sorted(backend.list_prefix("a"))
            # Normalize to forward slashes for cross-platform comparison.
            keys = [k.replace("\\", "/") for k in keys]
            self.assertEqual(keys, ["a/x.txt", "a/y.txt"])
            all_keys = sorted(backend.list_prefix(""))
            all_keys = [k.replace("\\", "/") for k in all_keys]
            self.assertEqual(len(all_keys), 3)


if __name__ == "__main__":
    unittest.main()
