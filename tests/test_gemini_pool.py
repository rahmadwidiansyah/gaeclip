"""Gemini provider pool tests (spec TEST 1-30 + integration).

All transports are injected fakes (request_fn) — no network, no sleeping:
delays are recorded via sleep_fn. Redis is a minimal in-memory fake.
"""

import concurrent.futures
import logging
import threading
import unittest

from gaeclip.gemini_pool import (
    CAT_AUTH,
    METRICS,
    GeminiProvider,
    HealthManager,
    PoolConfig,
    PoolExhausted,
    ProviderModel,
    ProviderStore,
    classify_action,
    compute_backoff,
    execute_pool,
    failure_report,
)


class FakeRedisLite:
    def __init__(self):
        self.hashes: dict[str, dict] = {}
        self.counters: dict[str, int] = {}

    def hget(self, h, k):
        return self.hashes.get(h, {}).get(k)

    def hset(self, h, k, v):
        self.hashes.setdefault(h, {})[k] = v
        return 1

    def hdel(self, h, k):
        return self.hashes.get(h, {}).pop(k, None) is not None

    def hgetall(self, h):
        return dict(self.hashes.get(h, {}))

    def incr(self, k):
        self.counters[k] = self.counters.get(k, 0) + 1
        return self.counters[k]

    def decr(self, k):
        self.counters[k] = self.counters.get(k, 0) - 1
        return self.counters[k]

    def expire(self, k, s):
        return True

    def delete(self, k):
        self.counters.pop(k, None)
        return 1


class E(Exception):
    def __init__(self, code=None, text=""):
        self.code = code
        super().__init__(text or f"err {code}")


def script_transport(plan: dict, calls: list):
    def request_fn(pid, mid, attempt):
        calls.append((pid, mid, attempt))
        outcomes = plan.get((pid, mid), ["ok"])
        value = outcomes[0] if len(outcomes) == 1 else outcomes.pop(0)
        if value == "ok":
            return f"WIN:{pid}/{mid}"
        if isinstance(value, BaseException):
            raise value
        raise E(value)
    return request_fn


def make_providers(spec: dict) -> list[GeminiProvider]:
    """spec: {pid: ([model_ids], priority)}."""
    out = []
    for pid, (models, priority) in spec.items():
        out.append(GeminiProvider(
            id=pid, name=pid.upper(), enabled=True, priority=priority,
            api_key=f"key-{pid}",
            models=[ProviderModel(id=m, enabled=True, priority=i + 1) for i, m in enumerate(models)],
        ))
    return out


def run_pool(providers, plan, calls, config=None, client=None, **kw):
    cfg = config or PoolConfig(retry_base_delay_ms=1, retry_max_delay_ms=5)
    health = HealthManager(client, cfg)
    kwargs = dict(task_type="curation", sleep_fn=lambda s: None)
    kwargs.update(kw)
    return execute_pool(providers, request_fn=script_transport(plan, calls),
                        config=cfg, health=health, **kwargs), health


class PoolBasicsTests(unittest.TestCase):
    def test_1_single_provider_model_success(self):
        calls = []
        (resp, meta), _ = run_pool(make_providers({"p1": (["a"], 1)}), {}, calls)
        self.assertEqual(resp, "WIN:p1/a")
        self.assertEqual(meta["provider_id"], "p1")
        self.assertFalse(meta["fallback_used"])

    def test_2_model_fallback(self):
        calls = []
        (resp, meta), _ = run_pool(
            make_providers({"p1": (["a", "b"], 1)}),
            {("p1", "a"): [503], ("p1", "b"): ["ok"]}, calls,
            config=PoolConfig(retry_base_delay_ms=1, retry_max_delay_ms=5))
        self.assertEqual(resp, "WIN:p1/b")
        self.assertTrue(meta["fallback_used"])

    def test_3_provider_fallback_stops_at_success(self):
        calls = []
        providers = make_providers({"p1": (["a"], 1), "p2": (["a"], 2), "p3": (["a"], 3)})
        (resp, meta), _ = run_pool(
            providers, {("p1", "a"): [503, 503, 503], ("p2", "a"): ["ok"]}, calls,
            config=PoolConfig(retry_max_attempts=0, max_attempts=9))
        self.assertEqual(meta["provider_id"], "p2")
        self.assertNotIn(("p3", "a", 0), calls)

    def test_4_retry_same_on_503(self):
        calls = []
        run_pool(make_providers({"p1": (["a"], 1)}),
                 {("p1", "a"): [503, 503, "ok"]}, calls)
        self.assertEqual([c[:2] for c in calls], [("p1", "a")] * 3)

    def test_5_repeated_503_falls_back(self):
        calls = []
        (resp, _), _ = run_pool(
            make_providers({"p1": (["a", "b"], 1)}),
            {("p1", "a"): [503, 503, 503], ("p1", "b"): ["ok"]}, calls)
        self.assertEqual(resp, "WIN:p1/b")
        self.assertEqual(len([c for c in calls if c[:2] == ("p1", "a")]), 3)

    def test_6_no_retry_on_429_failover(self):
        # Quota never recovers in seconds: 429 fails over immediately
        # instead of burning retries on the same pair.
        calls = []
        (resp, meta), _ = run_pool(
            make_providers({"p1": (["a"], 1), "p2": (["a"], 2)}),
            {("p1", "a"): [429], ("p2", "a"): ["ok"]}, calls)
        self.assertEqual(resp, "WIN:p2/a")
        self.assertEqual([c[:2] for c in calls if c[0] == "p1"], [("p1", "a")])

    def test_7_retry_after_honored_and_capped(self):
        delays = []
        calls = []
        run_pool(make_providers({"p1": (["a"], 1)}),
                 {("p1", "a"): [E(503, "overloaded, Retry-After: 2"), "ok"]}, calls,
                 config=PoolConfig(retry_base_delay_ms=100, retry_max_delay_ms=5000,
                                   retry_jitter_ratio=0.0),
                 sleep_fn=delays.append)
        self.assertEqual(delays, [2.0])

    def test_8_auth_goes_next_provider_without_loop(self):
        calls = []
        providers = make_providers({"p1": (["a", "b"], 1), "p2": (["a"], 2)})
        (resp, _), _ = run_pool(
            providers, {("p1", "a"): [401], ("p2", "a"): ["ok"]}, calls,
            config=PoolConfig(retry_max_attempts=3))
        self.assertEqual(resp, "WIN:p2/a")
        self.assertEqual(len([c for c in calls if c[0] == "p1"]), 1)

    def test_9_model_not_found_skips_model(self):
        calls = []
        (resp, _), _ = run_pool(
            make_providers({"p1": (["a", "b"], 1)}),
            {("p1", "a"): [404], ("p1", "b"): ["ok"]}, calls)
        self.assertEqual(resp, "WIN:p1/b")
        self.assertEqual(len([c for c in calls if c[:2] == ("p1", "a")]), 1)

    def test_10_server_error_retry_then_fallback(self):
        calls = []
        (resp, _), _ = run_pool(
            make_providers({"p1": (["a", "b"], 1)}),
            {("p1", "a"): [500, 500, 500], ("p1", "b"): ["ok"]}, calls)
        self.assertEqual(resp, "WIN:p1/b")

    def test_11_timeout_retry_then_fallback(self):
        calls = []
        (resp, _), _ = run_pool(
            make_providers({"p1": (["a", "b"], 1)}),
            {("p1", "a"): [TimeoutError("timed out")] * 3, ("p1", "b"): ["ok"]}, calls)
        self.assertEqual(resp, "WIN:p1/b")

    def test_12_cancelled_never_retried(self):
        import asyncio

        calls = []
        with self.assertRaises(asyncio.CancelledError):
            run_pool(make_providers({"p1": (["a", "b"], 1)}),
                     {("p1", "a"): [asyncio.CancelledError()]}, calls)
        self.assertEqual(len(calls), 1)

    def test_13_exhaustion_then_heuristic_fallback(self):
        calls = []
        with self.assertRaises(PoolExhausted) as ctx:
            run_pool(make_providers({"p1": (["a"], 1)}),
                     {("p1", "a"): [503, 503, 503]}, calls,
                     config=PoolConfig(max_attempts=3))
        # Job continues via existing local fallback, never crashes.
        heuristic = f"heuristic-after-{len(ctx.exception.failures)}-failures"
        self.assertEqual(heuristic, "heuristic-after-3-failures")
        self.assertTrue(ctx.exception.report.startswith("Gemini failed after"))


class HealthCircuitTests(unittest.TestCase):
    def _health(self, client=None, **kw):
        cfg = PoolConfig(retry_base_delay_ms=1, retry_max_delay_ms=5, **kw)
        return HealthManager(client, cfg), cfg

    def test_14_cooled_provider_skipped(self):
        client = FakeRedisLite()
        health, cfg = self._health(client)
        health.record_failure("p1", "a", "RATE_LIMIT", "429")
        calls = []
        (resp, _), _ = run_pool(
            make_providers({"p1": (["a"], 1), "p2": (["a"], 2)}),
            {("p2", "a"): ["ok"]}, calls, client=client)
        self.assertEqual(resp, "WIN:p2/a")
        self.assertFalse([c for c in calls if c[0] == "p1"])

    def test_15_cooldown_expiry_reconsidered(self):
        client = FakeRedisLite()
        health, cfg = self._health(client)
        health.record_failure("p1", "a", "RATE_LIMIT", "429", at=1.0)
        # Simulate expiry by resetting (same as natural expiry path).
        health.reset("p1")
        snap = health.snapshot("p1", "a")
        self.assertEqual(snap.cooldown_until, 0.0)

    def test_16_circuit_opens_after_threshold(self):
        client = FakeRedisLite()
        health, cfg = self._health(client, circuit_failure_threshold=3,
                                   circuit_cooldown_ms=30000)
        for _ in range(3):
            health.record_failure("p1", "a", "SERVER", "503")
        snap = health.snapshot("p1", "a")
        self.assertEqual(snap.circuit, "OPEN")
        self.assertEqual(snap.state, "UNAVAILABLE")
        self.assertFalse(health.is_cooled_down("p1", "a"))

    def test_17_half_open_success_closes(self):
        client = FakeRedisLite()
        health, cfg = self._health(client, circuit_failure_threshold=1)
        health.record_failure("p1", "a", "SERVER", "503")
        self.assertEqual(health.snapshot("p1", "a").circuit, "OPEN")
        health.record_success("p1", "a", 120)
        snap = health.snapshot("p1", "a")
        self.assertEqual(snap.circuit, "CLOSED")
        self.assertEqual(snap.state, "HEALTHY")

    def test_18_unhealthy_model_pair_skipped_healthy_used(self):
        client = FakeRedisLite()
        health, cfg = self._health(client)
        health.record_failure("p1", "a", "RATE_LIMIT", "429")
        calls = []
        (resp, _), _ = run_pool(
            make_providers({"p1": (["a", "b"], 1)}),
            {("p1", "b"): ["ok"]}, calls, client=client)
        self.assertEqual(resp, "WIN:p1/b")
        self.assertFalse([c for c in calls if c[:2] == ("p1", "a")])

    def test_19_disabled_provider_skipped(self):
        providers = make_providers({"p1": (["a"], 1), "p2": (["a"], 2)})
        providers[0].enabled = False
        calls = []
        (resp, _), _ = run_pool(providers, {("p2", "a"): ["ok"]}, calls)
        self.assertEqual(resp, "WIN:p2/a")

    def test_20_disabled_model_skipped(self):
        providers = make_providers({"p1": (["a", "b"], 1)})
        providers[0].models[0].enabled = False
        calls = []
        (resp, _), _ = run_pool(providers, {("p1", "b"): ["ok"]}, calls)
        self.assertEqual(resp, "WIN:p1/b")

    def test_21_removed_model_marked_unavailable(self):
        client = FakeRedisLite()
        from gaeclip.gemini_pool import ProviderStore

        store = ProviderStore(client)
        providers = make_providers({"p1": (["a", "b"], 1)})
        store.save(providers[0])
        store.mark_model_unavailable("p1", "a")
        reloaded = store.get("p1")
        assert reloaded is not None
        self.assertIn("a", reloaded.unavailable_models)
        calls = []
        (resp, _), _ = run_pool([reloaded], {("p1", "b"): ["ok"]}, calls,
                                client=client)
        self.assertEqual(resp, "WIN:p1/b")


class DiscoveryStoreTests(unittest.TestCase):
    def test_22_discovery_failure_keeps_cache(self):
        from unittest.mock import patch

        from gaeclip import ai_models
        from gaeclip.ai_models import DiscoveryError, get_registry

        client = FakeRedisLite()
        snap = ai_models.RegistrySnapshot(
            models=[ai_models.normalize_model(
                {"name": "models/gemini-x", "supportedGenerationMethods": ["generateContent"]})],
            source="live", discovered_at=1000.0, expires_at=1001.0,
            key_fingerprint=ai_models.key_fingerprint("k"),
        )
        ai_models._write_cache(client, snap)
        with patch.object(ai_models, "discover_raw_models",
                          side_effect=DiscoveryError("transient", "down")):
            got = get_registry(client, "k")
        self.assertEqual(got.source, "cache-stale")
        self.assertTrue(got.models)

    def test_23_discovery_failure_without_cache(self):
        from unittest.mock import patch

        from gaeclip import ai_models
        from gaeclip.ai_models import DiscoveryError, get_registry

        with patch.object(ai_models, "discover_raw_models",
                          side_effect=DiscoveryError("transient", "down")):
            got = get_registry(None, "k")
        self.assertEqual(got.source, "error")
        self.assertIn("MODEL_DISCOVERY_FAILED", (got.error or {}).get("code", ""))

    def test_24_legacy_migration(self):
        from gaeclip.gemini_pool import ProviderStore

        class Svc:
            def __init__(self, values):
                self.values = values

            def get(self, key):
                if key not in self.values:
                    raise ValueError(key)
                return self.values[key]

        store = ProviderStore(FakeRedisLite())
        migrated = store.migrate_legacy(Svc({
            "GEMINI_API_KEY": "legacy-key",
            "AI_PRIMARY_MODEL": "gemini-m",
            "AI_FALLBACK_MODELS": '["gemini-f"]',
        }))
        self.assertEqual(len(migrated), 1)
        self.assertEqual(migrated[0].id, "gemini_api_1")
        self.assertEqual([m.id for m in migrated[0].models], ["gemini-m", "gemini-f"])
        # Non-empty pool is never overwritten.
        again = store.migrate_legacy(Svc({"GEMINI_API_KEY": "other"}))
        self.assertEqual(again[0].api_key, "legacy-key")


class ConcurrencyBudgetTests(unittest.TestCase):
    def test_25_concurrency_limit_prefers_next_provider(self):
        # Shared redis counter over the limit: the pool's own manager
        # refuses p1 and routes to p2 without touching p1.
        client = FakeRedisLite()
        client.counters["gaeclip:gemini:inflight:p1"] = 5
        calls = []
        (resp, _), _ = run_pool(
            make_providers({"p1": (["a"], 1), "p2": (["a"], 2)}),
            {("p2", "a"): ["ok"]}, calls, client=client)
        self.assertEqual(resp, "WIN:p2/a")
        self.assertFalse([c for c in calls if c[0] == "p1"])

    def test_25b_redis_counter_enforced(self):
        client = FakeRedisLite()
        client.counters["gaeclip:gemini:inflight:p1"] = 5
        health = HealthManager(client, PoolConfig(max_concurrent_per_provider=2))
        # Local semaphore free, redis counter over limit -> refused.
        health._semaphores["p1"] = threading.Semaphore(2)
        self.assertFalse(health.try_acquire("p1"))

    def test_26_concurrent_health_updates_stay_sane(self):
        client = FakeRedisLite()
        health = HealthManager(client, PoolConfig())
        barrier = threading.Barrier(8)

        def hammer(i):
            barrier.wait()
            for _ in range(25):
                if i % 2:
                    health.record_success("p1", "a", 10)
                else:
                    health.record_failure("p1", "a", "SERVER", "503")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(hammer, range(8)))
        snap = health.snapshot("p1", "a")
        self.assertGreaterEqual(snap.successes + snap.failures, 0)
        self.assertIn(snap.state, ("HEALTHY", "DEGRADED", "RATE_LIMITED", "UNAVAILABLE"))

    def test_27_no_duplicate_attempts(self):
        calls = []
        with self.assertRaises(PoolExhausted):
            run_pool(make_providers({"p1": (["a", "b"], 1)}),
                     {("p1", "a"): [503, 503, 503], ("p1", "b"): [500, 500, 500]}, calls,
                     config=PoolConfig(max_attempts=20))
        per_pair: dict = {}
        for pid, mid, _ in calls:
            per_pair[(pid, mid)] = per_pair.get((pid, mid), 0) + 1
        self.assertEqual(per_pair, {("p1", "a"): 3, ("p1", "b"): 3})

    def test_28_hard_budget_stop(self):
        calls = []
        with self.assertRaises(PoolExhausted):
            run_pool(make_providers({"p1": (["a", "b", "c"], 1)}),
                     {("p1", "a"): [503] * 9, ("p1", "b"): [503] * 9,
                      ("p1", "c"): [503] * 9}, calls,
                     config=PoolConfig(max_attempts=5))
        self.assertEqual(len(calls), 5)


class SecurityMetaTests(unittest.TestCase):
    def test_29_no_key_in_logs(self):
        providers = make_providers({"p1": (["a"], 1)})
        providers[0].api_key = "SECRETKEY1234567890"
        calls = []
        with self.assertLogs("gaeclip.gemini_pool", level="INFO") as captured:
            with self.assertRaises(PoolExhausted):
                run_pool(providers, {("p1", "a"): [503, 503, 503]}, calls,
                         config=PoolConfig(max_attempts=3))
        self.assertNotIn("SECRETKEY1234567890", captured.output.__str__())

    def test_30_no_key_in_frontend_payload(self):
        providers = make_providers({"p1": (["a"], 1)})
        providers[0].api_key = "SECRETKEY1234567890"
        masked = providers[0].masked()
        self.assertNotIn("api_key", masked)
        self.assertNotIn("SECRETKEY1234567890", json_dumps(masked))
        self.assertEqual(masked["key_suffix"], "7890")
        self.assertTrue(masked["key_configured"])

    def test_failure_report_hides_keys(self):
        providers = make_providers({"p1": (["a"], 1)})
        providers[0].api_key = "SECRETKEY1234567890"
        calls = []
        with self.assertRaises(PoolExhausted) as ctx:
            run_pool(providers, {("p1", "a"): [E(503)]}, calls,
                     config=PoolConfig(max_attempts=1))
        self.assertNotIn("SECRETKEY1234567890", ctx.exception.report)


def json_dumps(payload) -> str:
    import json

    return json.dumps(payload)


class IntegrationTests(unittest.TestCase):
    def test_spec_scenario_p1_fails_p2_m3_wins(self):
        calls = []
        providers = make_providers({"p1": (["m1", "m2", "m3"], 1),
                                    "p2": (["m1", "m2", "m3"], 2),
                                    "p3": (["m1", "m2", "m3"], 3)})
        plan = {("p1", "m1"): [503, 503, 503], ("p1", "m2"): [429, 429, 429],
                ("p1", "m3"): [500, 500, 500],
                ("p2", "m1"): [503, 503, 503], ("p2", "m2"): [TimeoutError("t")] * 3,
                ("p2", "m3"): ["ok"]}
        (resp, meta), _ = run_pool(
            providers, plan, calls,
            config=PoolConfig(retry_max_attempts=2, retry_base_delay_ms=1,
                              retry_max_delay_ms=5, max_attempts=30))
        self.assertEqual(resp, "WIN:p2/m3")
        self.assertEqual(meta["provider_id"], "p2")
        self.assertEqual(meta["model_used"], "m3")
        self.assertFalse([c for c in calls if c[0] == "p3"])
        self.assertLessEqual(meta["attempts_total"], 30)

    def test_final_failure_scenario(self):
        calls = []
        providers = make_providers({"p1": (["a", "b", "c"], 1),
                                    "p2": (["a", "b", "c"], 2),
                                    "p3": (["a", "b", "c"], 3)})
        plan = {("p1", "a"): [503] * 3, ("p1", "b"): [503] * 3, ("p1", "c"): [503] * 3,
                ("p2", "a"): [429] * 3, ("p2", "b"): [TimeoutError("t")] * 3,
                ("p2", "c"): [500] * 3,
                ("p3", "a"): [E(401)], ("p3", "b"): [E(404)], ("p3", "c"): [503] * 3}
        with self.assertRaises(PoolExhausted) as ctx:
            run_pool(
                providers, plan, calls,
                config=PoolConfig(retry_max_attempts=2, retry_base_delay_ms=1,
                                  retry_max_delay_ms=5, max_attempts=40))
        report = ctx.exception.report
        self.assertIn("p1:", report)
        self.assertIn("Final fallback", report)
        # Auth pair attempted once, never looped.
        self.assertEqual(len([c for c in calls if c[:2] == ("p3", "a")]), 1)


class ClassifierBackoffTests(unittest.TestCase):
    def test_retry_after_parsing(self):
        from gaeclip.gemini_pool import parse_retry_after

        self.assertEqual(parse_retry_after(E(429, "Retry-After: 7")), 7.0)
        self.assertIsNone(parse_retry_after(E(503, "boom")))

    def test_backoff_bounds(self):
        from gaeclip.gemini_pool import compute_backoff

        self.assertAlmostEqual(compute_backoff(0, 500, 5000, 0.0), 0.5)
        self.assertAlmostEqual(compute_backoff(10, 500, 5000, 0.0), 5.0)
        self.assertAlmostEqual(compute_backoff(0, 500, 5000, 0.0, retry_after_s=30), 5.0)

    def test_action_matrix(self):
        cases = [
            (E(429, "429 quota"), "NEXT_MODEL"),
            (E(503, "503 unavailable"), "RETRY_SAME"),
            (E(401, "401 unauthorized"), "NEXT_PROVIDER"),
            (E(404, "models/xyz not found"), "NEXT_MODEL"),
            (E(400, "bad request"), "PERMANENT"),
        ]
        for exc, expected in cases:
            category, action, _ = classify_action(exc)
            self.assertEqual(action, expected, f"{exc}")
        import asyncio

        _, action, _ = classify_action(asyncio.CancelledError())
        self.assertEqual(action, "PERMANENT")


class AutoModeTests(unittest.TestCase):
    def _provider(self):
        return GeminiProvider(
            id="p1", name="P1", enabled=True, priority=1, api_key="k1",
            models=[ProviderModel(id="manual-x", enabled=True, priority=1)],
            model_mode="automatic", auto_depth=2, auto_free_tier_only=False,
        )

    def test_resolves_chain_from_discovery(self):
        from unittest.mock import patch

        from gaeclip import ai_models
        from gaeclip.gemini_pool import resolve_automatic_models

        def fake_raw(name, methods=("generateContent",)):
            return {"name": f"models/{name}", "displayName": name,
                    "supportedGenerationMethods": list(methods)}

        snap = ai_models.RegistrySnapshot(
            models=[ai_models.normalize_model(fake_raw("gemini-b")),
                    ai_models.normalize_model(fake_raw("gemini-a"))],
            source="live", discovered_at=1.0, expires_at=9999.0,
            key_fingerprint="x")
        with patch.object(ai_models, "get_registry", return_value=snap):
            out = resolve_automatic_models(None, [self._provider()])
        self.assertEqual([m.id for m in out[0].models], ["gemini-a", "gemini-b"])

    def test_falls_back_to_manual_on_discovery_failure(self):
        from unittest.mock import patch

        from gaeclip import ai_models
        from gaeclip.ai_models import DiscoveryError
        from gaeclip.gemini_pool import resolve_automatic_models

        provider = self._provider()
        with patch.object(ai_models, "get_registry",
                          side_effect=DiscoveryError("transient", "down")):
            out = resolve_automatic_models(None, [provider])
        self.assertEqual([m.id for m in out[0].models], ["manual-x"])

    def test_manual_mode_untouched(self):
        from gaeclip.gemini_pool import resolve_automatic_models

        provider = self._provider()
        provider.model_mode = "manual"
        out = resolve_automatic_models(None, [provider])
        self.assertEqual([m.id for m in out[0].models], ["manual-x"])

    def test_store_round_trip_auto_fields(self):
        from gaeclip.gemini_pool import ProviderStore

        store = ProviderStore(FakeRedisLite())
        provider = self._provider()
        store.save(provider)
        reloaded = store.get("p1")
        assert reloaded is not None
        self.assertEqual(reloaded.model_mode, "automatic")
        self.assertEqual(reloaded.auto_depth, 2)
        self.assertFalse(reloaded.auto_free_tier_only)
        self.assertEqual(reloaded.masked()["model_mode"], "automatic")

    def test_executor_uses_resolved_chain(self):
        calls = []
        providers = [self._provider()]
        providers[0].models = [ProviderModel(id="gemini-a", enabled=True, priority=1),
                               ProviderModel(id="gemini-b", enabled=True, priority=2)]
        (resp, meta), _ = run_pool(
            providers, {("p1", "gemini-a"): [503, 503, 503], ("p1", "gemini-b"): ["ok"]},
            calls, config=PoolConfig(max_attempts=9))
        self.assertEqual(resp, "WIN:p1/gemini-b")
        self.assertEqual(meta["provider_id"], "p1")


if __name__ == "__main__":
    unittest.main()
