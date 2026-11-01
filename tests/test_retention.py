"""Storage retention enforcement tests."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, "tests")


class RetentionRunTests(unittest.TestCase):
    def test_removes_old_artifacts(self):
        from gaeclip.retention import run

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "jobs" / "j1" / "clips"
            job.mkdir(parents=True)
            old = job / "clip1.mp4"
            old.write_bytes(b"mp4")
            # Touch so mtime is 25 hours ago.
            ts = time.time() - 25 * 3600
            os.utime(old, (ts, ts))
            new = job / "clip2.mp4"
            new.write_bytes(b"mp4")
            result = run(
                storage_root=tmp,
                artifact_retention_hours=24,
                intermediate_retention_hours=0,
            )
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            self.assertEqual(result["artifacts"], 1)
            self.assertEqual(result["errors"], [])

    def test_keeps_new_artifacts(self):
        from gaeclip.retention import run

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "jobs" / "j1" / "clips"
            job.mkdir(parents=True)
            recent = job / "clip.mp4"
            recent.write_bytes(b"mp4")
            result = run(
                storage_root=tmp,
                artifact_retention_hours=24,
                intermediate_retention_hours=0,
            )
            self.assertTrue(recent.exists())
            self.assertEqual(result["artifacts"], 0)

    def test_removes_intermediate_temp(self):
        from gaeclip.retention import run

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp) / "tmp"
            tmp_dir.mkdir()
            old_json = tmp_dir / "transcript.json"
            old_json.write_text("{}")
            ts = time.time() - 7 * 3600
            os.utime(old_json, (ts, ts))
            result = run(
                storage_root=tmp,
                artifact_retention_hours=0,
                intermediate_retention_hours=6,
            )
            self.assertFalse(old_json.exists())
            self.assertEqual(result["intermediate"], 1)

    def test_dry_run_does_not_remove(self):
        from gaeclip.retention import run

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "jobs" / "j1" / "clips"
            job.mkdir(parents=True)
            clip = job / "x.mp4"
            clip.write_bytes(b"data")
            ts = time.time() - 100 * 3600
            os.utime(clip, (ts, ts))
            result = run(
                storage_root=tmp,
                artifact_retention_hours=1,
                intermediate_retention_hours=0,
                dry_run=True,
            )
            self.assertTrue(clip.exists())
            self.assertEqual(result["dry_run"], True)
            self.assertEqual(result["artifacts"], 0)


if __name__ == "__main__":
    unittest.main()
