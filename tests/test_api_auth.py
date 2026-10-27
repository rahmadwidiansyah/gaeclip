"""API bearer-token guard tests.

Local/desktop default (no API_AUTH_TOKEN) stays open. Public mode must
refuse to start without a token, and a configured token must gate every
non-media route while leaving liveness and header-less media reads open.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, "tests")

TOKEN = "test-token-0123456789"


def _app():
    from fastapi.testclient import TestClient

    import gaeclip.api as api_mod

    from test_video_metadata import FakeRedis

    fake = FakeRedis()
    redis_mock = MagicMock()
    redis_mock.from_url.return_value = fake
    with patch("redis.Redis", redis_mock):
        return TestClient(api_mod.create_app())


class SettingsValidationTests(unittest.TestCase):
    def test_public_mode_requires_token(self):
        from gaeclip.config import Settings
        from gaeclip.errors import ConfigurationError

        with patch.dict(
            os.environ,
            {"PUBLIC_MODE": "true", "API_HOST": "0.0.0.0"},
            clear=True,
        ):
            with self.assertRaises(ConfigurationError):
                Settings.resolve(None)

    def test_public_mode_accepts_token(self):
        from gaeclip.config import Settings

        with patch.dict(
            os.environ,
            {
                "PUBLIC_MODE": "true",
                "API_HOST": "0.0.0.0",
                "API_AUTH_TOKEN": TOKEN,
            },
            clear=True,
        ):
            resolved = Settings.resolve(None)
        self.assertEqual(resolved.api_auth_token, TOKEN)
        self.assertTrue(resolved.public_mode)


class ApiTokenMiddlewareTests(unittest.TestCase):
    def test_default_local_profile_stays_open(self):
        with patch.dict(os.environ, {"API_AUTH_TOKEN": ""}, clear=False):
            client = _app()
        self.assertEqual(client.get("/api/v1/jobs").status_code, 200)
        self.assertEqual(client.get("/health/config").status_code, 200)

    def test_token_gates_api_and_config(self):
        with patch.dict(os.environ, {"API_AUTH_TOKEN": TOKEN}, clear=False):
            client = _app()

        self.assertEqual(client.get("/api/v1/jobs").status_code, 401)
        self.assertEqual(client.get("/health/config").status_code, 401)
        self.assertIn("Bearer", client.get("/api/v1/jobs").headers.get(
            "www-authenticate", ""))

        bearer = {"Authorization": f"Bearer {TOKEN}"}
        self.assertEqual(client.get("/api/v1/jobs", headers=bearer).status_code, 200)
        self.assertEqual(client.get("/health/config", headers=bearer).status_code, 200)

        api_key = {"X-API-Key": TOKEN}
        self.assertEqual(client.get("/api/v1/jobs", headers=api_key).status_code, 200)

        wrong = {"Authorization": "Bearer wrong"}
        self.assertEqual(client.get("/api/v1/jobs", headers=wrong).status_code, 401)

    def test_liveness_and_media_reads_are_open(self):
        with patch.dict(os.environ, {"API_AUTH_TOKEN": TOKEN}, clear=False):
            client = _app()

        self.assertEqual(client.get("/health/live").status_code, 200)
        # Past the middleware, not found: proves the request was not 401.
        self.assertEqual(
            client.get("/api/v1/brand-assets/nonexistent.png").status_code, 404
        )
        self.assertEqual(
            client.get("/api/v1/jobs/missing/clips/missing").status_code, 404
        )
        self.assertEqual(
            client.get("/api/v1/social/publish/missing/media").status_code, 404
        )

    def test_write_routes_are_gated(self):
        with patch.dict(os.environ, {"API_AUTH_TOKEN": TOKEN}, clear=False):
            client = _app()

        self.assertEqual(client.post("/api/v1/jobs", json={}).status_code, 401)
        self.assertEqual(
            client.post("/api/v1/settings", json={"settings": {}}).status_code, 401
        )


if __name__ == "__main__":
    unittest.main()
