"""Backend tests for the Gemini model system (AT).

All Google API traffic is mocked (urllib stub); no quota is spent.
Secrets are FakeRedis-backed; the API key must never appear in logs.
"""

import io
import json
import logging
import time
import unittest
import urllib.error
from unittest.mock import patch

from gaeclip import ai_models
from gaeclip.ai_models import (
    AIChainExhausted,
    AIPrefs,
    DiscoveryError,
    RegistrySnapshot,
    automatic_chain,
    backoff_delays,
    cached_snapshot,
    classify_category,
    describe_selection,
    discover_raw_models,
    free_tier_status,
    generation_tuple,
    get_registry,
    invalidate_registry,
    normalize_chain,
    normalize_model,
    parse_fallbacks,
    prefs_from_service,
    rank_models,
    refresh_registry,
    release_channel,
    resolve_chain,
    run_ai_chain,
    validate_prefs,
)
from gaeclip.app_settings import AppSettings
from gaeclip.errors import ValidationError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def model_entry(name, methods=("generateContent",), **extra):
    payload = {
        "name": f"models/{name}",
        "displayName": name.replace("-", " ").title(),
        "version": "",
        "description": f"{name}ᴬ",
        "inputTokenLimit": 1000000,
        "outputTokenLimit": 8192,
        "supportedGenerationMethods": list(methods),
    }
    payload.update(extra)
    return payload


SCENARIO_MODELS = [
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


def install_urlopen(pages=None, error=None, calls=None):
    """pages: list of response payloads (one per page fetch)."""
    state = {"n": 0}
    if calls is not None:
        calls["n"] = 0

    def fake_urlopen(request, timeout=None):
        state["n"] += 1
        if calls is not None:
            calls["n"] += 1
            calls.setdefault("keys", []).append(
                dict(getattr(request, "header_items", lambda: [])())
                if hasattr(request, "header_items") else {}
            )
        if error is not None:
            raise error
        payload = pages[min(state["n"] - 1, len(pages) - 1)]
        return FakeHTTPResponse(payload)

    patcher = patch("urllib.request.urlopen", fake_urlopen)
    patcher.start()
    return patcher, state


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}

    def _enc(self, value):
        return value.encode() if isinstance(value, str) else value

    @staticmethod
    def _key(key):
        return key.decode() if isinstance(key, bytes) else key

    def hget(self, name, key):
        value = self.hashes.get(self._key(name), {}).get(self._key(key))
        return self._enc(value) if value is not None else None

    def hset(self, name, key, value=None, mapping=None):
        table = self.hashes.setdefault(self._key(name), {})
        table[self._key(key)] = value if isinstance(value, str) else value.decode()
        return 1

    def hdel(self, name, *keys):
        table = self.hashes.get(self._key(name), {})
        normed = [self._key(k) for k in keys]
        removed = sum(1 for k in normed if k in table)
        for k in normed:
            table.pop(k, None)
        return removed

    def hgetall(self, name):
        return {k.encode(): self._enc(v) for k, v in self.hashes.get(self._key(name), {}).items()}

    def pipeline(self):
        redis = self

        class Pipe:
            def hset(self, name, key, value=None, mapping=None):
                redis.hset(name, key, value, mapping=mapping)
                return self

            def execute(self):
                return []

        return Pipe()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class DiscoveryTests(unittest.TestCase):
    def test_success_single_page(self):
        patcher, _ = install_urlopen([{"models": SCENARIO_MODELS}])
        try:
            out = discover_raw_models("sk-key")
            self.assertEqual(len(out), 4)
        finally:
            patcher.stop()

    def test_pagination_followed(self):
        patcher, state = install_urlopen([
            {"models": SCENARIO_MODELS[:2], "nextPageToken": "p2"},
            {"models": SCENARIO_MODELS[2:]},
        ])
        try:
            out = discover_raw_models("sk-key")
            self.assertEqual(len(out), 4)
            self.assertEqual(state["n"], 2)
        finally:
            patcher.stop()

    def test_empty(self):
        patcher, _ = install_urlopen([{"models": []}])
        try:
            self.assertEqual(discover_raw_models("sk-key"), [])
        finally:
            patcher.stop()

    def test_401_403_are_auth(self):
        for code in (401, 403):
            err = urllib.error.HTTPError("url", code, "x", {}, io.BytesIO(b"{}"))
            patcher, _ = install_urlopen(error=err)
            try:
                with self.assertRaises(DiscoveryError) as ctx:
                    discover_raw_models("sk-key")
                self.assertEqual(ctx.exception.kind, "auth")
            finally:
                patcher.stop()

    def test_429_503_are_transient(self):
        for code in (429, 503):
            err = urllib.error.HTTPError("url", code, "x", {}, io.BytesIO(b"{}"))
            patcher, _ = install_urlopen(error=err)
            try:
                with self.assertRaises(DiscoveryError) as ctx:
                    discover_raw_models("sk-key")
                self.assertEqual(ctx.exception.kind, "transient")
            finally:
                patcher.stop()

    def test_network_error_transient(self):
        patcher, _ = install_urlopen(error=urllib.error.URLError("dns"))
        try:
            with self.assertRaises(DiscoveryError) as ctx:
                discover_raw_models("sk-key")
            self.assertEqual(ctx.exception.kind, "transient")
        finally:
            patcher.stop()

    def test_key_sent_as_header_not_url(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            return FakeHTTPResponse({"models": []})

        patcher = patch("urllib.request.urlopen", fake_urlopen)
        patcher.start()
        try:
            discover_raw_models("sk-live-SECRETKEY123")
            self.assertNotIn("SECRETKEY", seen["url"])
        finally:
            patcher.stop()


# ---------------------------------------------------------------------------
# Normalization / classification / policy
# ---------------------------------------------------------------------------

class ModelInfoTests(unittest.TestCase):
    def test_normalize_stable_flash(self):
        info = normalize_model(SCENARIO_MODELS[0])
        self.assertEqual(info.id, "gemini-3.8-flash")
        self.assertEqual(info.display_name, "Gemini 3.8 Flash")
        self.assertTrue(info.available)
        self.assertTrue(info.supports_generate_content)
        self.assertEqual(info.family, "gemini")
        self.assertEqual(info.generation, "3.8")
        self.assertEqual(info.variant, "flash")
        self.assertEqual(info.release_channel, "stable")
        self.assertFalse(info.is_preview)
        self.assertEqual(info.category, "multimodal")
        self.assertEqual(info.free_tier, "free")
        self.assertTrue(info.eligible_for_gaeclip)

    def test_release_channels(self):
        self.assertEqual(release_channel("gemini-3.8-flash")[0], "stable")
        self.assertEqual(release_channel("gemini-2.0-flash-preview")[0], "preview")
        self.assertEqual(release_channel("gemini-exp-1206")[0], "experimental")
        self.assertEqual(release_channel("gemini-flash-latest")[0], "latest_alias")
        self.assertEqual(release_channel("weird-model")[0], "unknown")

    def test_latest_is_not_stable(self):
        channel, _, _, is_latest = release_channel("gemini-flash-latest")
        self.assertTrue(is_latest)
        self.assertNotEqual(channel, "stable")

    def test_unsupported_categories_filtered(self):
        self.assertEqual(classify_category("text-embedding-004", ["embedContent"]), "embedding")
        self.assertEqual(classify_category("imagen-3", []), "image_generation")
        self.assertEqual(classify_category("gemini-tts-x", []), "text_to_speech")
        self.assertEqual(classify_category("gemini-live-x", []), "live")
        for name in ("text-embedding-004", "imagen-3", "gemini-tts-x", "gemini-live-x"):
            methods = ["embedContent"] if "embed" in name else []
            info = normalize_model(model_entry(name, methods))
            self.assertFalse(info.eligible_for_gaeclip, name)

    def test_unknown_category_never_forced(self):
        self.assertEqual(classify_category("mystery-9", []), "other")

    def test_free_tier_policy(self):
        self.assertEqual(free_tier_status("flash", "stable"), "free")
        self.assertEqual(free_tier_status("flash-lite", "stable"), "free")
        self.assertEqual(free_tier_status("pro", "stable"), "paid")
        self.assertEqual(free_tier_status("flash", "preview"), "unknown")
        self.assertEqual(free_tier_status("other", "stable"), "unknown")

    def test_version_compare_not_lexical(self):
        self.assertGreater(generation_tuple("3.10"), generation_tuple("3.9"))

    def test_no_invented_metadata(self):
        info = normalize_model({"name": "models/odd-one"})
        self.assertEqual(info.generation, "")
        self.assertEqual(info.release_channel, "unknown")
        self.assertFalse(info.supports_generate_content)
        self.assertFalse(info.eligible_for_gaeclip)


# ---------------------------------------------------------------------------
# Registry / cache
# ---------------------------------------------------------------------------

class RegistryTests(unittest.TestCase):
    def test_refresh_then_cache_hit(self):
        calls = {}
        patcher, _ = install_urlopen([{"models": SCENARIO_MODELS}], calls=calls)
        redis = FakeRedis()
        try:
            snap = refresh_registry(redis, "sk-key")
            self.assertEqual(snap.source, "live")
            self.assertEqual(snap.counts()["discovered"], 4)
            self.assertEqual(calls["n"], 1)
            cached = get_registry(redis, "sk-key")
            self.assertEqual(cached.source, "cache")
            self.assertEqual(calls["n"], 1)  # no second HTTP call
        finally:
            patcher.stop()

    def test_stale_cache_used_when_google_fails(self):
        patcher, _ = install_urlopen([{"models": SCENARIO_MODELS}])
        redis = FakeRedis()
        try:
            refresh_registry(redis, "sk-key", ttl=1)
        finally:
            patcher.stop()
        time.sleep(1.05)
        err = urllib.error.HTTPError("url", 503, "x", {}, io.BytesIO(b"{}"))
        patcher, _ = install_urlopen(error=err)
        try:
            snap = get_registry(redis, "sk-key")
            self.assertEqual(snap.source, "cache-stale")
            self.assertEqual(len(snap.models), 4)  # dropdown never emptied
        finally:
            patcher.stop()

    def test_error_without_any_cache(self):
        err = urllib.error.HTTPError("url", 503, "x", {}, io.BytesIO(b"{}"))
        patcher, _ = install_urlopen(error=err)
        try:
            snap = get_registry(FakeRedis(), "sk-key")
            self.assertEqual(snap.source, "error")
            self.assertEqual(snap.error["code"], "MODEL_DISCOVERY_FAILED")
        finally:
            patcher.stop()

    def test_key_change_scopes_cache(self):
        patcher, _ = install_urlopen([{"models": SCENARIO_MODELS}])
        redis = FakeRedis()
        try:
            refresh_registry(redis, "sk-old")
            invalidate_registry(redis, "sk-old")
            self.assertIsNone(cached_snapshot(redis, "sk-old"))
        finally:
            patcher.stop()


# ---------------------------------------------------------------------------
# Preferences / validation / selection
# ---------------------------------------------------------------------------

def snapshot_of(names):
    return RegistrySnapshot(models=[normalize_model(model_entry(n)) for n in names], source="live")


class PrefsTests(unittest.TestCase):
    def test_duplicate_fallbacks_rejected_or_normalized(self):
        prefs = AIPrefs(mode="manual", primary_model="gemini-3.8-flash",
                        fallback_models=["gemini-3.8-flash", "gemini-3.7-flash"])
        primary, fallbacks, _ = validate_prefs(prefs, snapshot_of(["gemini-3.8-flash", "gemini-3.7-flash"]))
        self.assertEqual(primary, "gemini-3.8-flash")
        self.assertEqual(fallbacks, ["gemini-3.7-flash"])

    def test_normalize_chain_empty_primary(self):
        with self.assertRaises(ValidationError):
            normalize_chain("", [])

    def test_unavailable_model_kept_with_warning(self):
        prefs = AIPrefs(mode="manual", primary_model="gemini-9.9-flash", fallback_models=[])
        primary, _, warnings = validate_prefs(prefs, snapshot_of(["gemini-3.8-flash"]))
        self.assertEqual(primary, "gemini-9.9-flash")  # kept, never silently deleted
        self.assertTrue(any("9.9" in w for w in warnings))

    def test_ineligible_model_rejected(self):
        prefs = AIPrefs(mode="manual", primary_model="text-embedding-004", fallback_models=[])
        snap = RegistrySnapshot(models=[normalize_model(model_entry("text-embedding-004", ["embedContent"]))], source="live")
        with self.assertRaises(ValidationError):
            validate_prefs(prefs, snap)

    def test_paid_rejected_when_free_only(self):
        prefs = AIPrefs(mode="manual", primary_model="gemini-3.8-pro", fallback_models=[], free_tier_only=True)
        with self.assertRaises(ValidationError):
            validate_prefs(prefs, snapshot_of(["gemini-3.8-pro"]))
        prefs.free_tier_only = False
        primary, _, _ = validate_prefs(prefs, snapshot_of(["gemini-3.8-pro"]))
        self.assertEqual(primary, "gemini-3.8-pro")

    def test_automatic_prefers_stable_flash(self):
        snap = snapshot_of(["gemini-3.8-pro", "gemini-2.0-flash-preview", "gemini-3.7-flash"])
        chain = automatic_chain(snap.models, True)
        self.assertEqual(chain[0], "gemini-3.7-flash")

    def test_automatic_future_proof(self):
        snap = snapshot_of(["gemini-3.8-flash", "gemini-4.0-flash"])
        self.assertEqual(automatic_chain(snap.models, True)[0], "gemini-4.0-flash")

    def test_ranking_not_lexical(self):
        snap = snapshot_of(["gemini-3.9-flash", "gemini-3.10-flash"])
        self.assertEqual(rank_models(snap.models, True)[0].id, "gemini-3.10-flash")

    def test_parse_fallbacks(self):
        self.assertEqual(parse_fallbacks('["a", "b"]'), ["a", "b"])
        self.assertEqual(parse_fallbacks([]), [])
        self.assertEqual(parse_fallbacks(""), [])

    def test_legacy_primary_from_gemini_model(self):
        redis = FakeRedis()
        service = AppSettings(redis)
        prefs = prefs_from_service(service)
        self.assertEqual(prefs.mode, "automatic")
        # GEMINI_MODEL default acts as legacy primary when AI_PRIMARY empty.
        self.assertEqual(prefs.primary_model, "gemini-3.8-flash")

    def test_describe_selection_unavailable(self):
        prefs = AIPrefs(mode="manual", primary_model="gemini-9.9-flash")
        desc = describe_selection(prefs, snapshot_of(["gemini-3.8-flash"]))
        self.assertEqual(desc["models"][0]["available"], None)


class SettingsPersistenceTests(unittest.TestCase):
    def test_save_dedupes_fallbacks(self):
        redis = FakeRedis()
        service = AppSettings(redis)
        service.set_many({
            "GEMINI_API_KEY": "sk-test-key-1234567890",
            "AI_MODEL_MODE": "manual",
            "AI_PRIMARY_MODEL": "gemini-3.8-flash",
            "AI_FALLBACK_MODELS": '["gemini-3.8-flash", "gemini-3.7-flash"]',
        })
        stored = json.loads(service.get("AI_FALLBACK_MODELS"))
        self.assertEqual(stored, ["gemini-3.7-flash"])

    def test_key_change_invalidates_registry(self):
        redis = FakeRedis()
        patcher, _ = install_urlopen([{"models": SCENARIO_MODELS}])
        try:
            refresh_registry(redis, "sk-old")
        finally:
            patcher.stop()
        service = AppSettings(redis)
        service.set("GEMINI_API_KEY", "sk-new-key-1234567890")
        self.assertIsNone(cached_snapshot(redis, "sk-old"))

    def test_save_without_registry_still_atomic(self):
        redis = FakeRedis()
        service = AppSettings(redis)
        out = service.set_many({
            "AI_MODEL_MODE": "manual",
            "AI_PRIMARY_MODEL": "gemini-9.9-flash",
            "AI_FALLBACK_MODELS": "[]",
        })
        self.assertEqual(out["AI_PRIMARY_MODEL"], "gemini-9.9-flash")


# ---------------------------------------------------------------------------
# Execution chain
# ---------------------------------------------------------------------------

def http_error(code):
    return urllib.error.HTTPError("url", code, "x", {}, io.BytesIO(b"boom"))


class ChainTests(unittest.TestCase):
    def test_backoff_shape(self):
        delays = backoff_delays(3, base=1.0)
        self.assertEqual(len(delays), 3)
        self.assertLess(delays[0], delays[1])
        self.assertLess(delays[1], delays[2])

    def test_retry_then_fallback_success(self):
        calls = []

        def request(model, attempt):
            calls.append((model, attempt))
            if model == "gemini-3.8-flash":
                raise http_error(503)
            return object()

        slept = []
        _, meta = run_ai_chain(
            ["gemini-3.8-flash", "gemini-3.7-flash"], request,
            max_retries=2, base_delay=0.01, sleep_fn=slept.append,
        )
        self.assertEqual(meta["model_used"], "gemini-3.7-flash")
        self.assertEqual(meta["selection_method"], "gemini_fallback")
        self.assertEqual(meta["fallback_index"], 1)
        self.assertEqual(len(slept), 2)  # bounded retries before fallback
        self.assertEqual(meta["attempts_total"], len(calls))

    def test_primary_success_meta(self):
        _, meta = run_ai_chain(["m1"], lambda m, a: object(), sleep_fn=lambda s: None)
        self.assertEqual(meta["selection_method"], "gemini_primary")
        self.assertEqual(meta["fallback_index"], 0)

    def test_skip_known_unavailable(self):
        seen = []
        _, meta = run_ai_chain(
            ["gone", "m2"], lambda m, a: seen.append(m) or object(),
            skip_unavailable={"gone"}, sleep_fn=lambda s: None,
        )
        self.assertEqual(seen, ["m2"])
        self.assertEqual(meta["fallback_index"], 1)

    def test_auth_aborts_chain(self):
        def request(model, attempt):
            raise http_error(401)

        with self.assertRaises(urllib.error.HTTPError):
            run_ai_chain(["m1", "m2"], request, sleep_fn=lambda s: None)

    def test_exhaustion_raises_with_failures(self):
        with self.assertRaises(AIChainExhausted) as ctx:
            run_ai_chain(["m1"], lambda m, a: (_ for _ in ()).throw(http_error(500)),
                         max_retries=1, base_delay=0.01, sleep_fn=lambda s: None)
        self.assertTrue(ctx.exception.failures)
        self.assertIsNotNone(ctx.exception.last_error)


class CuratorChainTests(unittest.TestCase):
    def _lines(self):
        from gaeclip.schemas import TranscriptLine

        specs = [
            (0.0, 10.0, "pembuka yang cukup panjang sekali"),
            (10.0, 20.0, "isi tengah yang juga cukup panjang"),
            (20.0, 35.0, "penutup akhir yang lengkap dan jelas"),
        ]
        return [
            TranscriptLine(
                line_id=f"l{i + 1:06d}", start=start, end=end, text=text,
                language="id", source="youtube_auto",
            )
            for i, (start, end, text) in enumerate(specs)
        ]

    def _ok_response(self):
        import types

        payload = {"candidates": [{
            "candidate_id": "c000001", "start_line_id": "l000001", "end_line_id": "l000003",
            "start": 0.0, "end": 35.0, "title": "t", "reason": "r", "score": 0.9,
        }]}
        return types.SimpleNamespace(text=json.dumps(payload), usage_metadata=None)

    def test_fallback_model_used_after_primary_503(self):
        from gaeclip.curation import GeminiCurator

        calls = []

        def fake_generate(api_key, model, **kwargs):
            calls.append(model)
            if model == "gemini-3.8-flash":
                raise http_error(503)
            return self._ok_response()

        # curate() does `from .gemini_service import generate_content` locally,
        # so patch the attribute on the (importable, SDK-free) module.
        with patch("gaeclip.gemini_service.generate_content", fake_generate, create=True):
            curator = GeminiCurator("sk-k", model="gemini-3.8-flash",
                                    fallbacks=["gemini-3.7-flash"], max_retries=0)
            result = curator.curate(self._lines())
        self.assertEqual(calls, ["gemini-3.8-flash", "gemini-3.7-flash"])
        self.assertEqual(result.model_used, "gemini-3.7-flash")
        self.assertEqual(result.selection_method, "gemini_fallback")
        self.assertEqual(result.fallback_index, 1)

    def test_chain_dedupes_primary(self):
        from gaeclip.curation import GeminiCurator

        curator = GeminiCurator("sk-k", model="m1", fallbacks=["m1", "m2"])
        self.assertEqual(curator.chain, ["m1", "m2"])

    def test_all_models_fail_then_heuristic_last_resort(self):
        from gaeclip.curation import GeminiCurator, curate_with_fallback

        def always_503(api_key, model, **kwargs):
            raise http_error(503)

        with patch("gaeclip.gemini_service.generate_content", always_503, create=True):
            curator = GeminiCurator(
                "sk-k", model="gemini-3.8-flash",
                fallbacks=["gemini-3.7-flash", "gemini-3.6-flash"], max_retries=0,
            )
            result = curate_with_fallback(
                self._lines(), curator,
                min_duration=5, max_duration=40, max_clips=2,
            )
        # Chain exhausted (503s, no retry storm) -> heuristic, never a raise.
        self.assertFalse(result.ai_used)
        self.assertTrue(result.fallback_used)
        self.assertIn(result.curation_method, ("heuristic", "deterministic"))
        self.assertTrue(result.candidates)
        self.assertIn("503", result.fallback_reason or "")


class KeyHygieneTests(unittest.TestCase):
    def test_key_never_logged(self):
        records: list[logging.LogRecord] = []

        class Sink(logging.Handler):
            def emit(self, record):
                records.append(record)

        root = logging.getLogger("gaeclip.ai_models")
        sink = Sink()
        root.addHandler(sink)
        err = urllib.error.HTTPError("url", 503, "x", {}, io.BytesIO(b"{}"))
        patcher, _ = install_urlopen(error=err)
        try:
            try:
                refresh_registry(FakeRedis(), "sk-super-SECRET-123")
            except DiscoveryError:
                pass
        finally:
            patcher.stop()
            root.removeHandler(sink)
        blob = " ".join(r.getMessage() for r in records)
        self.assertNotIn("SECRET", blob)


if __name__ == "__main__":
    unittest.main()
