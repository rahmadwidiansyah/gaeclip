"""Provider pool API tests: CRUD, validation, masking, health, persistence."""

import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, "tests")

from test_video_metadata import FakeRedis, make_client


def add_provider(client, **overrides):
    body = {"name": "API 1", "api_key": "test-key-abcdefgh", "enabled": True,
            "priority": 1,
            "models": [{"id": "gemini-a", "enabled": True, "priority": 1}]}
    body.update(overrides)
    return client.post("/api/v1/settings/ai/providers", json=body)


class ProviderApiTests(unittest.TestCase):
    def setUp(self):
        self.client, self.fake = make_client()

    def test_list_empty_initially(self):
        res = self.client.get("/api/v1/settings/ai/providers")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["providers"], [])

    def test_add_and_masking(self):
        res = add_provider(self.client)
        self.assertEqual(res.status_code, 201)
        payload = res.json()
        self.assertEqual(payload["id"], "gemini_api_1")
        self.assertNotIn("api_key", payload)
        self.assertNotIn("test-key-abcdefgh", res.text)
        self.assertEqual(payload["key_suffix"], "efgh")
        self.assertTrue(payload["key_configured"])

    def test_add_validation(self):
        res = add_provider(self.client, name="  ")
        self.assertEqual(res.status_code, 422)
        res = add_provider(self.client, api_key="  ")
        self.assertEqual(res.status_code, 422)

    def test_edit_enable_models_priority(self):
        add_provider(self.client)
        res = self.client.patch("/api/v1/settings/ai/providers/gemini_api_1", json={
            "name": "Primary", "enabled": False, "priority": 3,
            "models": [{"id": "gemini-b", "enabled": True, "priority": 1},
                       {"id": "gemini-a", "enabled": False, "priority": 2}],
        })
        self.assertEqual(res.status_code, 200)
        payload = res.json()
        self.assertEqual(payload["name"], "Primary")
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["priority"], 3)
        self.assertEqual([m["id"] for m in payload["models"]], ["gemini-b", "gemini-a"])

    def test_key_preserved_when_not_resent(self):
        add_provider(self.client)
        self.client.patch("/api/v1/settings/ai/providers/gemini_api_1",
                          json={"name": "Renamed"})
        res = self.client.get("/api/v1/settings/ai/providers")
        self.assertTrue(res.json()["providers"][0]["key_configured"])

    def test_reorder(self):
        add_provider(self.client, name="A")
        add_provider(self.client, name="B")
        res = self.client.post("/api/v1/settings/ai/providers/reorder",
                               json={"ids": ["gemini_api_2", "gemini_api_1"]})
        self.assertEqual(res.status_code, 200)
        listed = self.client.get("/api/v1/settings/ai/providers").json()["providers"]
        self.assertEqual([p["id"] for p in listed], ["gemini_api_2", "gemini_api_1"])

    def test_delete_and_last_protected(self):
        add_provider(self.client)
        res = self.client.delete("/api/v1/settings/ai/providers/gemini_api_1")
        self.assertEqual(res.status_code, 422)  # last provider protected
        add_provider(self.client, name="B")
        res = self.client.delete("/api/v1/settings/ai/providers/gemini_api_1")
        self.assertEqual(res.status_code, 200)
        listed = self.client.get("/api/v1/settings/ai/providers").json()["providers"]
        self.assertEqual(len(listed), 1)

    def test_health_dashboard_no_secrets(self):
        add_provider(self.client)
        res = self.client.get("/api/v1/settings/ai/providers/health")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("metrics", body)
        self.assertNotIn("test-key-abcdefgh", res.text)
        provider = body["providers"][0]
        self.assertIn("health", provider)
        self.assertIn("success_rate", provider)

    def test_health_reset(self):
        add_provider(self.client)
        res = self.client.post("/api/v1/settings/ai/providers/health/reset",
                               json={"provider_id": "gemini_api_1"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"reset": "gemini_api_1"})

    def test_connection_mocked(self):
        from gaeclip.gemini_service import GeminiTestResult

        add_provider(self.client)
        with patch("gaeclip.gemini_service.test_connection",
                   return_value=GeminiTestResult(ok=True, model="gemini-a", latency_ms=42)):
            with patch("gaeclip.ai_models.get_registry",
                       side_effect=Exception("offline")):
                res = self.client.post("/api/v1/settings/ai/providers/gemini_api_1/test")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["latency_ms"], 42)

    def test_model_mocked(self):
        from gaeclip.gemini_service import GeminiTestResult

        add_provider(self.client)
        with patch("gaeclip.gemini_service.test_connection",
                   return_value=GeminiTestResult(ok=True, model="gemini-a", latency_ms=7)):
            res = self.client.post("/api/v1/settings/ai/providers/gemini_api_1/test-model",
                                   json={"model": "gemini-a"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["model"], "gemini-a")

    def test_discovery_per_provider_mocked(self):
        import time

        from gaeclip import ai_models

        add_provider(self.client)
        info = ai_models.normalize_model(
            {"name": "models/gemini-a", "displayName": "A",
             "supportedGenerationMethods": ["generateContent"]})
        snap = ai_models.RegistrySnapshot(
            models=[info], source="live", discovered_at=time.time(),
            expires_at=time.time() + 3600,
            key_fingerprint=ai_models.key_fingerprint("test-key-abcdefgh"))
        with patch("gaeclip.ai_models.get_registry", return_value=snap):
            res = self.client.get("/api/v1/settings/ai/providers/gemini_api_1/models")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["models"][0]["id"], "gemini-a")

    def test_refresh_marks_removed_unavailable(self):
        import time

        from gaeclip import ai_models

        add_provider(self.client, models=[
            {"id": "gemini-gone", "enabled": True, "priority": 1}])
        snap = ai_models.RegistrySnapshot(
            models=[], source="live", discovered_at=time.time(),
            expires_at=time.time() + 3600, key_fingerprint="x")
        with patch("gaeclip.ai_models.get_registry", return_value=snap):
            res = self.client.post("/api/v1/settings/ai/providers/gemini_api_1/models/refresh")
        self.assertEqual(res.status_code, 200)
        self.assertIn("gemini-gone", res.json()["removed_marked_unavailable"])
        listed = self.client.get("/api/v1/settings/ai/providers").json()["providers"]
        self.assertIn("gemini-gone", listed[0]["unavailable_models"])

    def test_edit_model_mode_fields(self):
        add_provider(self.client)
        res = self.client.patch("/api/v1/settings/ai/providers/gemini_api_1", json={
            "model_mode": "automatic", "auto_depth": 3, "auto_free_tier_only": False})
        self.assertEqual(res.status_code, 200)
        payload = res.json()
        self.assertEqual(payload["model_mode"], "automatic")
        self.assertEqual(payload["auto_depth"], 3)
        self.assertFalse(payload["auto_free_tier_only"])
        res = self.client.patch("/api/v1/settings/ai/providers/gemini_api_1",
                                json={"model_mode": "bogus"})
        self.assertEqual(res.status_code, 422)
        res = self.client.patch("/api/v1/settings/ai/providers/gemini_api_1",
                                json={"auto_depth": 99})
        self.assertEqual(res.status_code, 422)


if __name__ == "__main__":
    unittest.main()
