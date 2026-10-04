import json
import sys
import types
import unittest
from unittest.mock import patch


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def _enc(self, value):
        return value.encode() if isinstance(value, str) else value

    def get(self, key):
        value = self.store.get(key)
        return self._enc(value) if value is not None else None

    def set(self, key, value, ex=None, nx=None):
        if nx and key in self.store:
            return False
        self.store[key] = value if isinstance(value, str) else value.decode()
        return True

    def delete(self, key):
        return self.store.pop(key, None) is not None

    def ping(self):
        return True

    def scan_iter(self, match="", count=100):
        import fnmatch
        return [k for k in self.store if fnmatch.fnmatch(k, match)]

    def hget(self, name, key):
        value = self.hashes.get(name, {}).get(key)
        return self._enc(value) if value is not None else None

    def hset(self, name, key, value=None, mapping=None):
        table = self.hashes.setdefault(name, {})
        if mapping:
            for k, v in mapping.items():
                table[k] = v if isinstance(v, str) else v.decode()
            return len(mapping)
        table[key] = value if isinstance(value, str) else value.decode()
        return 1

    def hdel(self, name, *keys):
        table = self.hashes.get(name, {})
        removed = sum(1 for key in keys if key in table)
        for key in keys:
            table.pop(key, None)
        return removed

    def hgetall(self, name):
        return {k.encode(): self._enc(v) for k, v in self.hashes.get(name, {}).items()}

    def pipeline(self):
        client = self

        class Pipe:
            def __init__(self):
                self.ops = []

            def set(self, key, value, ex=None, nx=None):
                self.ops.append(("set", key, value, ex, nx))
                return self

            def hset(self, name, key, value=None, mapping=None):
                self.ops.append(("hset", name, key, value, mapping))
                return self

            def execute(self):
                out = []
                for op in self.ops:
                    if op[0] == "set":
                        _, k, v, e, n = op
                        out.append(client.set(k, v, ex=e, nx=n))
                    else:
                        _, name, key, value, mapping = op
                        out.append(client.hset(name, key, value, mapping=mapping))
                return out

        return Pipe()


def make_client():
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient
    import gaeclip.api as api_mod

    fake = FakeRedis()
    redis_mock = MagicMock()
    redis_mock.from_url.return_value = fake
    with patch("redis.Redis", redis_mock):
        app = api_mod.create_app()
    client = TestClient(app)
    return client, fake


class SettingsApiTests(unittest.TestCase):
    def test_list_settings_masks_secret(self):
        client, _ = make_client()
        res = client.get("/api/v1/settings")
        self.assertEqual(res.status_code, 200)
        settings = {item["key"]: item for item in res.json()["settings"]}
        self.assertIn("MAX_CLIPS", settings)
        self.assertIn("GEMINI_API_KEY", settings)
        secret = settings["GEMINI_API_KEY"]
        self.assertIsNone(secret["value"])
        self.assertIn("configured", secret)
        # metadata present for UI rendering
        self.assertIn("label", settings["MAX_CLIPS"])
        self.assertIn("category", settings["MAX_CLIPS"])
        self.assertIn("unit", settings["MAX_CLIPS"])

    def test_update_and_reset_roundtrip(self):
        client, _ = make_client()
        res = client.patch("/api/v1/settings", json={"settings": {"MAX_CLIPS": 7}})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("MAX_CLIPS", res.json()["updated"])
        settings = {item["key"]: item for item in res.json()["settings"]}
        self.assertEqual(settings["MAX_CLIPS"]["value"], 7)
        self.assertTrue(settings["MAX_CLIPS"]["customized"])
        res = client.delete("/api/v1/settings/MAX_CLIPS")
        self.assertEqual(res.status_code, 200)
        settings = {item["key"]: item for item in res.json()["settings"]}
        self.assertFalse(settings["MAX_CLIPS"]["customized"])

    def test_rejects_unknown_and_infra_keys(self):
        client, _ = make_client()
        res = client.patch("/api/v1/settings", json={"settings": {"NOPE": 1}})
        self.assertEqual(res.status_code, 422)
        res = client.patch("/api/v1/settings", json={"settings": {"REDIS_URL": "redis://evil"}})
        self.assertEqual(res.status_code, 422)
        res = client.patch("/api/v1/settings/REDIS_URL", json={"value": "x"})
        self.assertEqual(res.status_code, 422)
        res = client.delete("/api/v1/settings/API_HOST")
        self.assertEqual(res.status_code, 422)

    def test_validation_error_shape(self):
        client, _ = make_client()
        res = client.patch("/api/v1/settings", json={"settings": {"MAX_CLIPS": 0}})
        self.assertEqual(res.status_code, 422)
        self.assertIn("message", res.json()["detail"])
        # atomic: nothing stored
        res = client.patch(
            "/api/v1/settings",
            json={"settings": {"MAX_CLIPS": 4, "WORKER_CONCURRENCY": 0}},
        )
        self.assertEqual(res.status_code, 422)
        settings = {item["key"]: item for item in client.get("/api/v1/settings").json()["settings"]}
        self.assertNotEqual(settings["MAX_CLIPS"]["value"], 4)

    def test_cross_field_min_max_rejected(self):
        client, _ = make_client()
        res = client.patch(
            "/api/v1/settings",
            json={"settings": {"MIN_CLIP_DURATION_SECONDS": 90, "MAX_CLIP_DURATION_SECONDS": 30}},
        )
        self.assertEqual(res.status_code, 422)

    def test_secret_roundtrip_never_leaks(self):
        client, fake = make_client()
        res = client.patch("/api/v1/settings", json={"settings": {"GEMINI_API_KEY": "sk-live-123"}})
        self.assertEqual(res.status_code, 200)
        stored = fake.hashes["gaeclip:settings"]["GEMINI_API_KEY"]
        self.assertNotIn("sk-live-123", stored)
        body = res.text
        self.assertNotIn("sk-live-123", body)
        listing = client.get("/api/v1/settings").json()["settings"]
        secret = next(item for item in listing if item["key"] == "GEMINI_API_KEY")
        self.assertTrue(secret["configured"])
        self.assertIsNone(secret["value"])

    def test_gemini_test_requires_key(self):
        client, _ = make_client()
        res = client.post("/api/v1/settings/test/gemini", json={})
        self.assertEqual(res.status_code, 422)

    def test_gemini_test_success_with_stub(self):
        client, _ = make_client()

        class FakeModels:
            def generate_content(self, **kwargs):
                return types.SimpleNamespace(text="ok")

        class FakeClient:
            def __init__(self, api_key=None, **kwargs):
                self.api_key = api_key
                self.models = FakeModels()

        stub = types.ModuleType("google")
        genai = types.ModuleType("google.genai")
        genai.Client = FakeClient
        stub.genai = genai
        with patch.dict(sys.modules, {"google": stub, "google.genai": genai}):
            res = client.post("/api/v1/settings/test/gemini", json={"api_key": "sk-test"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["ok"])
        self.assertNotIn("sk-test", res.text)

    def test_save_survives_service_restart(self):
        """PATCH -> brand-new service instance (simulated backend restart)
        -> GET must still return the saved value (Redis persistence)."""
        from gaeclip.app_settings import AppSettings

        client, fake = make_client()
        res = client.patch("/api/v1/settings", json={"settings": {"GEMINI_MODEL": "gemini-persist-1"}})
        self.assertEqual(res.status_code, 200)
        # Simulate a full backend restart: fresh service over same storage.
        fresh = AppSettings(fake)
        self.assertEqual(fresh.get("GEMINI_MODEL"), "gemini-persist-1")
        listed = client.get("/api/v1/settings").json()["settings"]
        entry = next(item for item in listed if item["key"] == "GEMINI_MODEL")
        self.assertEqual(entry["value"], "gemini-persist-1")
        self.assertTrue(entry["customized"])

    def test_repeated_saves_no_duplicates_last_wins(self):
        client, fake = make_client()
        for model in ("gemini-a", "gemini-b", "gemini-c"):
            res = client.patch("/api/v1/settings", json={"settings": {"GEMINI_MODEL": model}})
            self.assertEqual(res.status_code, 200)
        stored = fake.hashes["gaeclip:settings"]
        self.assertEqual(
            [k for k in stored if "GEMINI_MODEL" in k],
            ["GEMINI_MODEL"],
        )
        entry = next(
            item for item in client.get("/api/v1/settings").json()["settings"]
            if item["key"] == "GEMINI_MODEL"
        )
        self.assertEqual(entry["value"], "gemini-c")

    def test_cors_preflight_allows_patch_and_delete(self):
        # Regression: browsers block PATCH/DELETE without an explicit
        # preflight allow-list, surfacing as opaque "Failed to fetch".
        client, _ = make_client()
        for method in ("PATCH", "DELETE", "PUT"):
            res = client.options(
                "/api/v1/settings",
                headers={
                    "Origin": "http://127.0.0.1:3000",
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": "content-type",
                },
            )
            allowed = res.headers.get("access-control-allow-methods", "")
            self.assertIn(method, allowed, f"preflight must allow {method}: {allowed!r}")

    def test_health_config_has_system_block(self):
        client, _ = make_client()
        res = client.get("/health/config")
        self.assertEqual(res.status_code, 200)
        system = res.json()["system"]
        for field in ("redis_connected", "storage_root", "frontend_origins", "public_mode", "api_host", "api_port"):
            self.assertIn(field, system)
        # no secrets in health output
        self.assertNotIn("GEMINI_API_KEY", json.dumps(res.json()))


if __name__ == "__main__":
    unittest.main()
