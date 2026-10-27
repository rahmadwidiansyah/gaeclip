"""Tests for metrics module."""

import sys
import unittest

sys.path.insert(0, "tests")


class MetricsTests(unittest.TestCase):
    def test_increment_and_render(self):
        from gaeclip.metrics import inc, render, reset

        reset()
        inc("jobs_created")
        inc("jobs_created")
        inc("clips_generated")
        out = render()
        self.assertIn("gaeclip_jobs_created 2", out)
        self.assertIn("gaeclip_clips_generated 1", out)

    def test_gauge(self):
        from gaeclip.metrics import set_gauge, render, reset

        reset()
        set_gauge("storage_bytes", 12345)
        out = render()
        self.assertIn("gaeclip_storage_bytes 12345", out)


if __name__ == "__main__":
    unittest.main()
