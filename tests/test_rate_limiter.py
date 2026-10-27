"""Tests for the rate limiter."""

import sys
import unittest

sys.path.insert(0, "tests")


class FakeRequest:
    def __init__(self, headers: dict | None = None, ip: str = "127.0.0.1"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": ip})()


class RateLimiterTests(unittest.TestCase):
    def test_allows_under_limit(self):
        from gaeclip.rate_limiter import RateLimiter, reset

        reset()
        limiter = RateLimiter(default_limit=3, window_seconds=60.0)
        req = FakeRequest(ip="1.2.3.4")
        self.assertTrue(limiter.allow(req))
        self.assertTrue(limiter.allow(req))
        self.assertTrue(limiter.allow(req))
        self.assertFalse(limiter.allow(req))

    def test_key_uses_client_id_when_present(self):
        from gaeclip.rate_limiter import RateLimiter, reset

        reset()
        # Use the module-level singleton so the test interacts with the
        # same state that the middleware will use.
        limiter = RateLimiter(default_limit=1, window_seconds=60.0)
        req_a = FakeRequest(headers={"X-Client-Id": "abc"}, ip="1.1.1.1")
        req_b = FakeRequest(headers={"X-Client-Id": "def"}, ip="2.2.2.2")
        self.assertTrue(limiter.allow(req_a))
        # Same client id should be blocked.
        self.assertFalse(limiter.allow(req_a))
        # Different client id should be allowed.
        self.assertTrue(limiter.allow(req_b))

    def test_remaining_reflects_usage(self):
        from gaeclip.rate_limiter import RateLimiter, reset

        reset()
        limiter = RateLimiter(default_limit=5, window_seconds=60.0)
        req = FakeRequest(ip="9.9.9.9")
        for _ in range(3):
            limiter.allow(req)
        self.assertEqual(limiter.remaining(req), 2)


if __name__ == "__main__":
    unittest.main()
