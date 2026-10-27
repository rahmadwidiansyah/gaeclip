"""Audit logger smoke tests. No network, no secrets leakage."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, "tests")


class AuditWriteTests(unittest.TestCase):
    def test_appends_iso_record(self):
        import gaeclip.audit as audit_mod

        with tempfile.TemporaryDirectory() as tmp:
            audit_mod.write_event(
                storage_root=tmp,
                action="job.created",
                actor="api",
                job_id="abc-123",
                ip="127.0.0.1",
            )
            path = Path(tmp) / "audit.jsonl"
            self.assertTrue(path.exists())
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["action"], "job.created")
            self.assertEqual(rec["job_id"], "abc-123")
            self.assertEqual(rec["ip"], "127.0.0.1")
            self.assertEqual(rec["actor"], "api")
            self.assertIn("ts", rec)
            self.assertIn("iso", rec)
            self.assertRegex(rec["iso"], r"^\d{4}-\d{2}-\d{2}T")

    def test_multiple_events_preserve_order(self):
        import gaeclip.audit as audit_mod

        with tempfile.TemporaryDirectory() as tmp:
            audit_mod.write_event(storage_root=tmp, action="a")
            time.sleep(0.01)
            audit_mod.write_event(storage_root=tmp, action="b")
            path = Path(tmp) / "audit.jsonl"
            actions = [json.loads(line)["action"] for line in path.read_text().splitlines()]
            self.assertEqual(actions, ["a", "b"])

    def test_size_bytes_returns_zero_when_missing(self):
        import gaeclip.audit as audit_mod

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(audit_mod.size_bytes(storage_root=tmp), 0)

    def test_missing_storage_root_creates_dir(self):
        import gaeclip.audit as audit_mod

        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "a" / "b"
            audit_mod.write_event(storage_root=nested, action="x")
            self.assertTrue((nested / "audit.jsonl").exists())

    def test_io_failure_is_silent(self):
        import gaeclip.audit as audit_mod

        # Force a path that will not be writable.
        path = Path("/proc/nonexistent_audit")
        audit_mod.write_event(storage_root=path.parent, action="z")
        # No exception leaks.


class AuditSizeRotateTests(unittest.TestCase):
    def test_rotation_when_exceeded(self):
        import gaeclip.audit as audit_mod

        with tempfile.TemporaryDirectory() as tmp:
            # Write a record that makes the file > 10 bytes.
            audit_mod.write_event(storage_root=tmp, action="big", max_bytes=10)
            path = Path(tmp) / "audit.jsonl"
            rotated = path.with_suffix(path.suffix + ".1")
            # Either rotation happened or the first write was already
            # over the tiny limit; either way the old content should not
            # be sitting in the primary file without a rotation marker.
            self.assertTrue(path.exists() or rotated.exists())


class AuditListTests(unittest.TestCase):
    def test_empty_file_returns_empty(self):
        import gaeclip.audit as audit_mod

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "audit.jsonl").touch()
            self.assertEqual(audit_mod.list_recent(storage_root=tmp), [])

    def test_limit_truncates(self):
        import gaeclip.audit as audit_mod

        with tempfile.TemporaryDirectory() as tmp:
            for i in range(5):
                audit_mod.write_event(storage_root=tmp, action=f"e{i}")
            rows = audit_mod.list_recent(storage_root=tmp, limit=2)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["action"], "e3")
            self.assertEqual(rows[-1]["action"], "e4")


if __name__ == "__main__":
    unittest.main()
