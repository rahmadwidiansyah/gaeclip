"""CORS + Private Network Access tests (tunneled dev, e.g. ngrok HTTPS).

When the UI is opened through a public tunnel while the API stays on
loopback, Chromium treats fetches as public->private requests:
- the Origin must be in FRONTEND_ORIGINS (CORS), and
- the server must answer `Access-Control-Allow-Private-Network: true`
  when the request carries `Access-Control-Request-Private-Network`.

No network, no credentials. FakeRedis-backed app via TestClient.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, "tests")

from test_video_metadata import FakeRedis

NGROK_ORIGIN = "https://2b4d-test.ngrok-free.app"


def _app(extra_origins=""):
    from fastapi.testclient import TestClient

    import gaeclip.api as api_mod

    origins = "http://127.0.0.1:3000,http://localhost:3000" + extra_origins
    fake = FakeRedis()
    redis_mock = MagicMock()
    redis_mock.from_url.return_value = fake
    with patch.dict(os.environ, {"FRONTEND_ORIGINS": origins}), \
         patch("redis.Redis", redis_mock):
        app = api_mod.create_app()
    return TestClient(app)


class PrivateNetworkAccessTests(unittest.TestCase):
    def test_preflight_from_tunnel_gets_pna_header(self):
        client = _app(f",{NGROK_ORIGIN}")
        res = client.options(
            "/api/v1/jobs?limit=100",
            headers={"Origin": NGROK_ORIGIN,
                     "Access-Control-Request-Method": "GET",
                     "Access-Control-Request-Private-Network": "true"})
        self.assertEqual(res.headers.get(
            "access-control-allow-private-network"), "true")
        # CORS itself still allows the origin.
        self.assertEqual(res.headers.get(
            "access-control-allow-origin"), NGROK_ORIGIN)

    def test_actual_get_from_tunnel_gets_pna_header(self):
        client = _app(f",{NGROK_ORIGIN}")
        res = client.get(
            "/api/v1/jobs?limit=100",
            headers={"Origin": NGROK_ORIGIN,
                     "Access-Control-Request-Private-Network": "true"})
        self.assertEqual(res.headers.get(
            "access-control-allow-private-network"), "true")

    def test_no_pna_marker_means_no_extra_header(self):
        # Localhost flow without the PNA marker: behavior unchanged.
        client = _app(f",{NGROK_ORIGIN}")
        res = client.get(
            "/api/v1/jobs?limit=100",
            headers={"Origin": "http://localhost:3000"})
        self.assertIsNone(res.headers.get(
            "access-control-allow-private-network"))

    def test_non_allowlisted_origin_gets_nothing(self):
        # PNA marker from a foreign origin: no opt-in, no CORS leak.
        client = _app()
        res = client.get(
            "/api/v1/jobs?limit=100",
            headers={"Origin": "https://evil.example",
                     "Access-Control-Request-Private-Network": "true"})
        self.assertIsNone(res.headers.get(
            "access-control-allow-private-network"))
        self.assertNotEqual(res.headers.get(
            "access-control-allow-origin"), "https://evil.example")

    def test_tunnel_origin_without_env_still_blocked(self):
        # Documents the A1 operational rule: the ngrok origin MUST be in
        # FRONTEND_ORIGINS, otherwise CORS blocks it (no ACAO echo).
        client = _app()
        res = client.options(
            "/api/v1/jobs?limit=100",
            headers={"Origin": NGROK_ORIGIN,
                     "Access-Control-Request-Method": "GET",
                     "Access-Control-Request-Private-Network": "true"})
        self.assertNotEqual(res.headers.get(
            "access-control-allow-origin"), NGROK_ORIGIN)
        self.assertIsNone(res.headers.get(
            "access-control-allow-private-network"))


if __name__ == "__main__":
    unittest.main()
