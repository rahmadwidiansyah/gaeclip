import os
import unittest
from unittest.mock import patch

from gaeclip.app_settings import (
    AppSettings,
    DEFS,
    INFRASTRUCTURE_KEYS,
    SETTINGS,
    decrypt_secret,
    encrypt_secret,
)
from gaeclip.errors import ValidationError


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}

    def hget(self, name, key):
        value = self.hashes.get(name, {}).get(key)
        return value.encode() if isinstance(value, str) else value

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
        return {k.encode(): v.encode() for k, v in self.hashes.get(name, {}).items()}

    def pipeline(self):
        client = self

        class Pipe:
            def __init__(self):
                self.ops = []

            def hset(self, name, key, value=None, mapping=None):
                self.ops.append((name, key, value, mapping))
                return self

            def execute(self):
                return [client.hset(n, k, v, mapping=m) for n, k, v, m in self.ops]

        return Pipe()


def clean_env():
    return patch.dict(os.environ, {}, clear=False)


class SchemaTests(unittest.TestCase):
    def test_all_spec_keys_present(self):
        expected = {
            "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_TIMEOUT_SECONDS",
            "AI_MODEL_MODE", "AI_PRIMARY_MODEL", "AI_FALLBACK_MODELS",
            "AI_FREE_TIER_ONLY",
            "GEMINI_MAX_ATTEMPTS", "GEMINI_RETRY_MAX_ATTEMPTS",
            "GEMINI_RETRY_BASE_DELAY_MS", "GEMINI_RETRY_MAX_DELAY_MS",
            "GEMINI_RETRY_JITTER_RATIO", "GEMINI_REQUEST_TIMEOUT_MS",
            "GEMINI_CIRCUIT_FAILURE_THRESHOLD", "GEMINI_CIRCUIT_COOLDOWN_MS",
            "GEMINI_AUTH_COOLDOWN_MS", "GEMINI_MAX_CONCURRENT_PER_PROVIDER",
            "GEMINI_ROUTING_STRATEGY", "GEMINI_MODEL_DISCOVERY_TTL",
            "RENDER_PROFILE",
            "WHISPER_MODEL", "TRANSCRIPT_SOURCE", "LOG_LEVEL", "WORKER_CONCURRENCY",
            "TALKNET_ENABLED", "TALKNET_DEVICE", "TALKNET_RESOURCE_MODE",
            "MAX_SOURCE_DURATION_SECONDS", "MAX_CLIPS",
            "MIN_CLIP_DURATION_SECONDS", "MAX_CLIP_DURATION_SECONDS",
            "CLIP_BUFFER_SECONDS", "MAX_TRANSCRIPT_CHARS",
            "MAX_ARTIFACT_BYTES", "MAX_JOB_SECONDS",
            "MAX_LLM_OUTPUT_TOKENS", "MAX_LLM_INPUT_CHARS",
            "MAX_CONCURRENT_JOBS", "ARTIFACT_RETENTION_HOURS",
            "INTERMEDIATE_RETENTION_HOURS",
            "SOCIAL_YOUTUBE_CLIENT_ID", "SOCIAL_YOUTUBE_CLIENT_SECRET",
            "SOCIAL_TIKTOK_CLIENT_KEY", "SOCIAL_TIKTOK_CLIENT_SECRET",
            "SOCIAL_META_APP_ID", "SOCIAL_META_APP_SECRET",
            "SOCIAL_INSTAGRAM_APP_ID", "SOCIAL_INSTAGRAM_APP_SECRET",
            "SOCIAL_PUBLIC_MEDIA_BASE_URL",
            "SOCIAL_OAUTH_HOST", "SOCIAL_OAUTH_PORT",
        }
        self.assertEqual(set(DEFS), expected)

    def test_social_oauth_secrets_encrypted_type(self):
        for key in ("SOCIAL_YOUTUBE_CLIENT_SECRET", "SOCIAL_TIKTOK_CLIENT_SECRET",
                    "SOCIAL_META_APP_SECRET", "SOCIAL_INSTAGRAM_APP_SECRET"):
            self.assertEqual(DEFS[key].type, "secret")
            self.assertEqual(DEFS[key].category, "Social Publishing")

    def test_spec_defaults(self):
        self.assertEqual(DEFS["GEMINI_MODEL"].default, "gemini-3.8-flash")
        self.assertEqual(DEFS["GEMINI_TIMEOUT_SECONDS"].default, "300")
        self.assertEqual(DEFS["WHISPER_MODEL"].default, "small")
        self.assertEqual(DEFS["WHISPER_MODEL"].category, "Transcript & LLM")
        self.assertEqual(DEFS["TRANSCRIPT_SOURCE"].default, "youtube_first")
        self.assertEqual(DEFS["LOG_LEVEL"].default, "INFO")
        self.assertEqual(DEFS["MAX_CLIPS"].default, "3")
        self.assertEqual(DEFS["MIN_CLIP_DURATION_SECONDS"].default, "30")
        self.assertEqual(DEFS["MAX_CLIP_DURATION_SECONDS"].default, "60")
        self.assertEqual(DEFS["RENDER_PROFILE"].default, "quality")
        self.assertEqual(DEFS["MAX_TRANSCRIPT_CHARS"].default, "1000000")

    def test_infra_keys_not_editable(self):
        for key in ("REDIS_URL", "STORAGE_ROOT", "API_HOST", "API_PORT", "PUBLIC_MODE", "API_AUTH_TOKEN"):
            self.assertIn(key, INFRASTRUCTURE_KEYS)
            self.assertNotIn(key, DEFS)


class ResolutionTests(unittest.TestCase):
    def test_default_without_db_or_env(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(AppSettings(FakeRedis()).get("MAX_CLIPS"), 3)

    def test_empty_env_falls_back_to_default(self):
        with patch.dict(os.environ, {"MAX_CLIPS": ""}):
            self.assertEqual(AppSettings(FakeRedis()).get("MAX_CLIPS"), 3)

    def test_env_fallback(self):
        with patch.dict(os.environ, {"MAX_CLIPS": "7"}):
            self.assertEqual(AppSettings(FakeRedis()).get("MAX_CLIPS"), 7)

    def test_db_beats_env(self):
        redis = FakeRedis()
        service = AppSettings(redis)
        service.set("MAX_CLIPS", 9)
        with patch.dict(os.environ, {"MAX_CLIPS": "7"}):
            self.assertEqual(service.get("MAX_CLIPS"), 9)

    def test_invalid_db_falls_back_to_default(self):
        redis = FakeRedis()
        redis.hset("gaeclip:settings", "MAX_CLIPS", "not-a-number")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(AppSettings(redis).get("MAX_CLIPS"), 3)


class MigrationTests(unittest.TestCase):
    def test_migrate_seeds_from_env(self):
        redis = FakeRedis()
        with patch.dict(os.environ, {"MAX_CLIPS": "6", "WHISPER_MODEL": "base"}):
            seeded = AppSettings(redis).migrate_from_env()
        self.assertIn("MAX_CLIPS", seeded)
        self.assertIn("WHISPER_MODEL", seeded)
        self.assertEqual(AppSettings(redis).get("MAX_CLIPS"), 6)

    def test_migrate_never_overwrites_user_values(self):
        redis = FakeRedis()
        AppSettings(redis).set("MAX_CLIPS", 5)
        with patch.dict(os.environ, {"MAX_CLIPS": "3"}):
            seeded = AppSettings(redis).migrate_from_env()
        self.assertNotIn("MAX_CLIPS", seeded)
        self.assertEqual(AppSettings(redis).get("MAX_CLIPS"), 5)

    def test_migrate_skips_invalid_env(self):
        redis = FakeRedis()
        with patch.dict(os.environ, {"MAX_CLIPS": "banana"}):
            seeded = AppSettings(redis).migrate_from_env()
        self.assertNotIn("MAX_CLIPS", seeded)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.service = AppSettings(FakeRedis())

    def test_rejects_unknown_and_infra_keys(self):
        with self.assertRaises(ValidationError):
            self.service.set("NOPE", 1)
        with self.assertRaises(ValidationError):
            self.service.set("REDIS_URL", "redis://x")
        with self.assertRaises(ValidationError):
            self.service.set_many({"MAX_CLIPS": 2, "API_HOST": "x"})

    def test_rejects_out_of_range(self):
        with self.assertRaises(ValidationError):
            self.service.set("MAX_CLIPS", 0)
        with self.assertRaises(ValidationError):
            self.service.set("GEMINI_TIMEOUT_SECONDS", -5)
        with self.assertRaises(ValidationError):
            self.service.set("WHISPER_MODEL", "huge")
        with self.assertRaises(ValidationError):
            self.service.set("LOG_LEVEL", "TRACE")

    def test_cross_field_min_max(self):
        with self.assertRaises(ValidationError):
            self.service.set_many({
                "MIN_CLIP_DURATION_SECONDS": 90,
                "MAX_CLIP_DURATION_SECONDS": 30,
            })
        with self.assertRaises(ValidationError):
            self.service.set("MIN_CLIP_DURATION_SECONDS", 9999)

    def test_set_many_is_atomic(self):
        with self.assertRaises(ValidationError):
            self.service.set_many({"MAX_CLIPS": 4, "WORKER_CONCURRENCY": 0})
        # MAX_CLIPS must not have been stored
        self.assertIsNone(self.service.stored_raw("MAX_CLIPS"))

    def test_reset_reveals_default(self):
        self.service.set("MAX_CLIPS", 8)
        self.assertEqual(self.service.get("MAX_CLIPS"), 8)
        self.service.reset("MAX_CLIPS")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.service.get("MAX_CLIPS"), 3)


class SecretTests(unittest.TestCase):
    def test_encrypted_at_rest_and_masked(self):
        redis = FakeRedis()
        service = AppSettings(redis)
        service.set("GEMINI_API_KEY", "sk-test-secret-value")
        stored = redis.hashes["gaeclip:settings"]["GEMINI_API_KEY"]
        self.assertTrue(stored.startswith("enc:v1:"))
        self.assertNotIn("sk-test-secret-value", stored)
        self.assertEqual(service.get("GEMINI_API_KEY"), "sk-test-secret-value")
        entry = next(item for item in service.as_dict() if item["key"] == "GEMINI_API_KEY")
        self.assertIsNone(entry["value"])
        self.assertTrue(entry["configured"])

    def test_unconfigured_secret_reports_false(self):
        service = AppSettings(FakeRedis())
        with patch.dict(os.environ, {}, clear=True):
            entry = next(item for item in service.as_dict() if item["key"] == "GEMINI_API_KEY")
            self.assertFalse(entry["configured"])

    def test_fernet_roundtrip(self):
        token = encrypt_secret("hello")
        self.assertEqual(decrypt_secret(token), "hello")

    def test_undecryptable_secret_falls_back_gracefully(self):
        # Simulates key rotation/loss: DB holds a token from another data key.
        import gaeclip.app_settings as mod
        redis = FakeRedis()
        redis.hashes["gaeclip:settings"] = {"GEMINI_API_KEY": "enc:v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}
        service = AppSettings(redis)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(service.get("GEMINI_API_KEY"), "")  # default, no crash
        # as_dict must also survive (settings page stays usable).
        entry = next(item for item in service.as_dict() if item["key"] == "GEMINI_API_KEY")
        self.assertTrue(entry["configured"])  # a stored value exists
        self.assertIsNone(entry["value"])


class ResolveTests(unittest.TestCase):
    def test_settings_resolve_uses_db(self):
        from gaeclip.config import Settings
        redis = FakeRedis()
        AppSettings(redis).set("MAX_CLIPS", 7)
        AppSettings(redis).set("GEMINI_MODEL", "gemini-x")
        with patch.dict(os.environ, {"MAX_CLIPS": "3"}):
            settings = Settings.resolve(redis)
        self.assertEqual(settings.max_clips, 7)
        self.assertEqual(settings.gemini_model, "gemini-x")

    def test_settings_resolve_without_client_uses_env(self):
        from gaeclip.config import Settings
        with patch.dict(os.environ, {"MAX_CLIPS": "4"}):
            settings = Settings.resolve(None)
        self.assertEqual(settings.max_clips, 4)


if __name__ == "__main__":
    unittest.main()
