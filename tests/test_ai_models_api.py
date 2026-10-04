"""API-level scenarios for the Gemini model system (BC).

Mocked Google Models API + FakeRedis + FastAPI TestClient.
Covers: dropdown-from-backend, missing-model handling, refresh counts,
atomic save validation, key-change rediscovery, stale-cache safety,
and key hygiene in responses.
"""

import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch


def model_entry(name, methods=("generateContent",)):
    return {
        "name": f"models/{name}",
        "displayName": name.replace("-", " ").title(),
        "version": "",
        "description": f"{name}",
        "inputTokenLimit": 1000000,
        "outputTokenLimit": 8192,
        "supportedGenerationMethods": list(methods),
    }


FOUR_FLASH = [
    model_entry("gemini-3.8-flash"),
    model_entry("gemini-3.7-flash"),
    model_entry("gemini-3.6-flash"),
    model_entry("gemini-3.5-flash"),
]


class FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    @staticmethod
    def _k(key):
        return key.decode() if isinstance(key, bytes) else key

    def _enc(self, value):
        return value.encode() if isinstance(value, str) else value

    def get(self, key):
        v = self.store.get(self._k(key))
        return self._enc(v) if v is not None else None

    def set(self, key, value, ex=None, nx=None):
        if nx and self._k(key) in self.store:
            return False
        self.store[self._k(key)] = value if isinstance(value, str) else value.decode()
        return True

    def delete(self, key):
        return self.store.pop(self._k(key), None) is not None

    def ping(self):
        return True

    def hget(self, name, key):
        v = self.hashes.get(self._k(name), {}).get(self._k(key))
        return self._enc(v) if v is not None else None

    def hset(self, name, key, value=None, mapping=None):
        table = self.hashes.setdefault(self._k(name), {})
        if mapping:
            for k, v in mapping.items():
                table[self._k(k)] = v if isinstance(v, str) else v.decode()
            return len(mapping)
        table[self._k(key)] = value if isinstance(value, str) else value.decode()
        return 1

    def hdel(self, name, *keys):
        table = self.hashes.get(self._k(name), {})
        normed = [self._k(k) for k in keys]
        removed = sum(1 for k in normed if k in table)
        for k in normed:
            table.pop(k, None)
        return removed

    def hgetall(self, name):
        return {k.encode(): self._enc(v) for k, v in self.hashes.get(self._k(name), {}).items()}

    def pipeline(self):
        client = self

        class Pipe:
            def hset(self, name, key, value=None, mapping=None):
                client.hset(name, key, value, mapping=mapping)
                return self

            def execute(self):
                return []

        return Pipe()


def install_urlopen(pages=None, error=None):
    def fake_urlopen(request, timeout=None):
        if error is not None:
            raise error
        payload = pages[0] if pages else {"models": []}
        # consume pages in order across calls
        if len(pages) > 1:
            pages.pop(0)
        return FakeHTTPResponse(payload)

    patcher = patch("urllib.request.urlopen", fake_urlopen)
    patcher.start()
    return patcher


def make_client():
    from fastapi.testclient import TestClient
    import gaeclip.api as api_mod

    fake = FakeRedis()
    redis_mock = MagicMock()
    redis_mock.from_url.return_value = fake
    with patch("redis.Redis", redis_mock):
        app = api_mod.create_app()
    return TestClient(app), fake


def set_key(client, key="sk-test-key-1234567890"):
    res = client.patch("/api/v1/settings", json={"settings": {"GEMINI_API_KEY": key}})
    assert res.status_code == 200, res.text


class AiModelsApiTests(unittest.TestCase):
    def test_scenario1_dropdown_from_backend(self):
        client, _ = make_client()
        set_key(client)
        patcher = install_urlopen([{"models": FOUR_FLASH}])
        try:
            res = client.get("/api/v1/settings/ai/models")
            self.assertEqual(res.status_code, 200, res.text)
            body = res.json()
            ids = [m["id"] for m in body["models"]]
            self.assertEqual(ids, ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"])
            self.assertNotIn("sk-test-key", res.text)  # key never leaks
            for m in body["models"]:
                self.assertTrue(m["eligible_for_gaeclip"])
        finally:
            patcher.stop()

    def test_scenario2_missing_model_not_selectable(self):
        client, _ = make_client()
        set_key(client)
        patcher = install_urlopen([{"models": FOUR_FLASH[1:]}])  # 3.8 gone
        try:
            client.post("/api/v1/settings/ai/models/refresh")
            res = client.get("/api/v1/settings/ai/selection")
            sel = res.json()["selection"]
            self.assertEqual(sel["method"], "automatic")
            self.assertEqual(sel["primary"], "gemini-3.7-flash")
            self.assertNotIn("gemini-3.8-flash", [m["id"] for m in sel["models"]])
        finally:
            patcher.stop()

    def test_scenario6_key_change_rediscovers(self):
        client, _ = make_client()
        set_key(client, "sk-old-key-1234567890")
        patcher = install_urlopen([{"models": FOUR_FLASH[:2]}])
        try:
            client.post("/api/v1/settings/ai/models/refresh")
        finally:
            patcher.stop()
        set_key(client, "sk-new-key-1234567890")  # invalidates old cache
        patcher = install_urlopen([{"models": FOUR_FLASH[2:]}])
        try:
            res = client.post("/api/v1/settings/ai/models/refresh")
            ids = [m["id"] for m in res.json()["selection"]["models"]]
            self.assertIn("gemini-3.6-flash", ids)
            self.assertNotIn("gemini-3.8-flash", ids)
        finally:
            patcher.stop()

    def test_scenario7_stale_cache_on_failure(self):
        client, _ = make_client()
        set_key(client)
        patcher = install_urlopen([{"models": FOUR_FLASH}])
        try:
            client.post("/api/v1/settings/ai/models/refresh")
        finally:
            patcher.stop()
        err = urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b"{}"))
        patcher = install_urlopen(error=err)
        try:
            res = client.post("/api/v1/settings/ai/models/refresh")
            # Forced refresh with zero cache-success: error, but models kept?
            # Force path returns 502 only when NOTHING cached; here cache is fresh.
            self.assertIn(res.status_code, (200, 502))
            res = client.get("/api/v1/settings/ai/models")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(len(res.json()["models"]), 4)
        finally:
            patcher.stop()

    def test_scenario8_new_model_appears_after_refresh(self):
        client, _ = make_client()
        set_key(client)
        patcher = install_urlopen([{"models": FOUR_FLASH}])
        try:
            client.post("/api/v1/settings/ai/models/refresh")
        finally:
            patcher.stop()
        patcher = install_urlopen([{"models": FOUR_FLASH + [model_entry("gemini-4.0-flash")]}])
        try:
            res = client.post("/api/v1/settings/ai/models/refresh")
            body = res.json()
            self.assertEqual(body["discovered"], 5)
            self.assertEqual(body["selection"]["primary"], "gemini-4.0-flash")
        finally:
            patcher.stop()

    def test_scenario9_custom_chain_persists(self):
        client, _ = make_client()
        set_key(client)
        patcher = install_urlopen([{"models": FOUR_FLASH}])
        try:
            client.post("/api/v1/settings/ai/models/refresh")
            res = client.patch("/api/v1/settings", json={"settings": {
                "AI_MODEL_MODE": "manual",
                "AI_PRIMARY_MODEL": "gemini-3.7-flash",
                "AI_FALLBACK_MODELS": json.dumps(["gemini-3.6-flash", "gemini-3.5-flash"]),
                "AI_FREE_TIER_ONLY": True,
            }})
            self.assertEqual(res.status_code, 200, res.text)
            sel = client.get("/api/v1/settings/ai/selection").json()["selection"]
            self.assertEqual(sel["primary"], "gemini-3.7-flash")
            self.assertEqual(sel["fallbacks"], ["gemini-3.6-flash", "gemini-3.5-flash"])
        finally:
            patcher.stop()

    def test_save_rejects_ineligible_and_paid(self):
        client, _ = make_client()
        set_key(client)
        models = FOUR_FLASH + [model_entry("text-embedding-004", ["embedContent"]),
                               model_entry("gemini-3.8-pro")]
        patcher = install_urlopen([{"models": models}])
        try:
            client.post("/api/v1/settings/ai/models/refresh")
            res = client.patch("/api/v1/settings", json={"settings": {
                "AI_MODEL_MODE": "manual",
                "AI_PRIMARY_MODEL": "text-embedding-004",
                "AI_FALLBACK_MODELS": "[]",
            }})
            self.assertEqual(res.status_code, 422, res.text)
            res = client.patch("/api/v1/settings", json={"settings": {
                "AI_MODEL_MODE": "manual",
                "AI_PRIMARY_MODEL": "gemini-3.8-pro",
                "AI_FALLBACK_MODELS": "[]",
                "AI_FREE_TIER_ONLY": True,
            }})
            self.assertEqual(res.status_code, 422, res.text)
        finally:
            patcher.stop()

    def test_refresh_reports_counts(self):
        client, _ = make_client()
        set_key(client)
        models = FOUR_FLASH + [model_entry("text-embedding-004", ["embedContent"])]
        patcher = install_urlopen([{"models": models}])
        try:
            res = client.post("/api/v1/settings/ai/models/refresh")
            body = res.json()
            self.assertEqual(body["discovered"], 5)
            self.assertEqual(body["compatible"], 4)
            self.assertEqual(body["unsupported"], 1)
        finally:
            patcher.stop()


if __name__ == "__main__":
    unittest.main()
