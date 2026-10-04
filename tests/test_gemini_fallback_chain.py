"""Fallback-chain proof: P1..P5 are REALLY attempted at runtime.

Simulated transports (failure injection via the request_fn seam — no network,
no sleeping). Complements test_gemini_pool.py with explicit multi-provider
chain scenarios, the error matrix, and the budget-starvation regression that
was observed live (9 attempts died inside provider #1, P2-P5 never attempted).
"""

import unittest
import unittest.mock

from gaeclip.gemini_pool import (
    CAT_RATE_LIMIT,
    GeminiProvider,
    HealthManager,
    PoolConfig,
    PoolContext,
    PoolExhausted,
    ProviderModel,
    ProviderStore,
    execute_pool,
    generate_via_pool,
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

    def get(self, k):
        raise AttributeError("unused")

    def set(self, *a, **k):
        raise AttributeError("unused")


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
            return FakeResponse(f"WIN:{pid}/{mid}")
        if isinstance(value, BaseException):
            raise value
        raise E(value)
    return request_fn


class FakeResponse:
    def __init__(self, text):
        self.text = text
        self.usage_metadata = None


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


def providers_of(calls):
    return [pid for pid, _, _ in calls]


class FiveProviderChainTests(unittest.TestCase):
    def _five(self):
        return make_providers({
            "p1": (["m1"], 1), "p2": (["m1"], 2), "p3": (["m1"], 3),
            "p4": (["m1"], 4), "p5": (["m1"], 5),
        })

    def test_p1_p2_fail_p3_success(self):
        calls = []
        (resp, meta), _ = run_pool(
            make_providers({"p1": (["m1"], 1), "p2": (["m1"], 2), "p3": (["m1"], 3)}),
            {("p1", "m1"): [503], ("p2", "m1"): [503], ("p3", "m1"): ["ok"]},
            calls,
            config=PoolConfig(retry_max_attempts=0, retry_base_delay_ms=1, retry_max_delay_ms=5))
        self.assertEqual(resp.text, "WIN:p3/m1")
        self.assertEqual(meta["provider_id"], "p3")
        self.assertEqual(providers_of(calls), ["p1", "p2", "p3"])
        self.assertTrue(meta["fallback_used"])
        self.assertGreater(meta["attempts_total"], 1)

    def test_five_providers_last_wins(self):
        calls = []
        (resp, meta), _ = run_pool(
            self._five(),
            {("p1", "m1"): [503], ("p2", "m1"): [500], ("p3", "m1"): [429],
             ("p4", "m1"): [TimeoutError("timed out")], ("p5", "m1"): ["ok"]},
            calls,
            config=PoolConfig(retry_max_attempts=0, retry_base_delay_ms=1, retry_max_delay_ms=5))
        self.assertEqual(resp.text, "WIN:p5/m1")
        self.assertEqual(meta["provider_id"], "p5")
        self.assertEqual(providers_of(calls), ["p1", "p2", "p3", "p4", "p5"])

    def test_reverse_first_success_stops_chain(self):
        calls = []
        (resp, meta), _ = run_pool(self._five(), {}, calls)
        self.assertEqual(resp.text, "WIN:p1/m1")
        self.assertEqual(calls, [("p1", "m1", 0)])
        self.assertFalse(meta["fallback_used"])

    def test_p2_receives_real_request_after_p1_forced_failure(self):
        """Explicit P2-as-fallback proof: transport SAW provider p2."""
        calls = []
        (resp, meta), _ = run_pool(
            make_providers({"p1": (["m1"], 1), "p2": (["m1"], 2)}),
            {("p1", "m1"): [E(503, "Service Unavailable")], ("p2", "m1"): ["ok"]},
            calls,
            config=PoolConfig(retry_max_attempts=0, retry_base_delay_ms=1, retry_max_delay_ms=5))
        self.assertIn(("p2", "m1", 0), calls)
        self.assertEqual(meta["provider_id"], "p2")


class NestedModelProviderTests(unittest.TestCase):
    def test_model_404_stays_inside_provider(self):
        calls = []
        (resp, meta), _ = run_pool(
            make_providers({"p1": (["m1", "m2"], 1), "p2": (["m1"], 2)}),
            {("p1", "m1"): [404], ("p1", "m2"): ["ok"]}, calls,
            config=PoolConfig(retry_max_attempts=0, retry_base_delay_ms=1, retry_max_delay_ms=5))
        self.assertEqual(resp.text, "WIN:p1/m2")
        self.assertNotIn("p2", providers_of(calls))

    def test_full_p1_sweep_then_p2(self):
        calls = []
        (resp, meta), _ = run_pool(
            make_providers({"p1": (["m1", "m2", "m3"], 1), "p2": (["m1"], 2)}),
            {("p1", "m1"): [503], ("p1", "m2"): [503], ("p1", "m3"): [503],
             ("p2", "m1"): ["ok"]}, calls,
            config=PoolConfig(retry_max_attempts=0, retry_base_delay_ms=1, retry_max_delay_ms=5))
        self.assertEqual(resp.text, "WIN:p2/m1")
        self.assertEqual(providers_of(calls), ["p1", "p1", "p1", "p2"])

    def test_budget_starvation_regression(self):
        """Live bug: 3 failing P1 models x (1+2 retries) = 9 attempts ate the
        whole default budget, P2-P5 never attempted. Default budget must now
        leave room for the next provider."""
        calls = []
        providers = make_providers({"p1": (["m1", "m2", "m3"], 1), "p2": (["m1"], 2)})
        plan = {("p1", "m1"): [503], ("p1", "m2"): [503], ("p1", "m3"): [503],
                ("p2", "m1"): ["ok"]}
        (resp, meta), _ = run_pool(providers, plan, calls)  # default PoolConfig
        self.assertEqual(resp.text, "WIN:p2/m1")
        self.assertIn("p2", providers_of(calls))
        self.assertGreater(meta["attempts_total"], 9)


class ErrorMatrixTests(unittest.TestCase):
    def test_400_fails_fast_no_quota_burn(self):
        calls = []
        with self.assertRaises(PoolExhausted):
            run_pool(
                make_providers({"p1": (["m1"], 1), "p2": (["m1"], 2)}),
                {("p1", "m1"): [400]}, calls,
                config=PoolConfig(retry_max_attempts=2, retry_base_delay_ms=1, retry_max_delay_ms=5))
        self.assertEqual(calls, [("p1", "m1", 0)])
        self.assertNotIn("p2", providers_of(calls))

    def test_401_goes_next_provider_without_same_retry(self):
        calls = []
        (resp, meta), _ = run_pool(
            make_providers({"p1": (["m1"], 1), "p2": (["m1"], 2)}),
            {("p1", "m1"): [401], ("p2", "m1"): ["ok"]}, calls,
            config=PoolConfig(retry_max_attempts=2, retry_base_delay_ms=1, retry_max_delay_ms=5))
        self.assertEqual(resp.text, "WIN:p2/m1")
        self.assertEqual([c for c in calls if c[0] == "p1"], [("p1", "m1", 0)])

    def test_429_fails_over_without_same_pair_retry(self):
        calls = []
        (resp, meta), _ = run_pool(
            make_providers({"p1": (["m1"], 1), "p2": (["m1"], 2)}),
            {("p1", "m1"): [429, 429, 429], ("p2", "m1"): ["ok"]}, calls,
            config=PoolConfig(retry_max_attempts=1, retry_base_delay_ms=1, retry_max_delay_ms=5))
        p1_calls = [c for c in calls if c[0] == "p1"]
        self.assertEqual(len(p1_calls), 1)
        self.assertEqual(resp.text, "WIN:p2/m1")

    def test_timeout_falls_to_next_provider(self):
        calls = []
        (resp, _), _ = run_pool(
            make_providers({"p1": (["m1"], 1), "p2": (["m1"], 2)}),
            {("p1", "m1"): [TimeoutError("request timed out")], ("p2", "m1"): ["ok"]},
            calls,
            config=PoolConfig(retry_max_attempts=0, retry_base_delay_ms=1, retry_max_delay_ms=5))
        self.assertEqual(providers_of(calls), ["p1", "p2"])

    def test_cancelled_aborts_immediately(self):
        calls = []
        with self.assertRaises(Exception):
            run_pool(
                make_providers({"p1": (["m1"], 1), "p2": (["m1"], 2)}),
                {("p1", "m1"): [Exception("Cancelled: user abort")]}, calls)
        self.assertNotIn("p2", providers_of(calls))


class PriorityAndEligibilityTests(unittest.TestCase):
    def test_execution_follows_priority_not_input_order(self):
        calls = []
        providers = make_providers({"pa": (["m1"], 3), "pb": (["m1"], 1), "pc": (["m1"], 2)})
        (resp, _), _ = run_pool(providers, {}, calls)
        self.assertEqual(resp.text, "WIN:pb/m1")

    def test_disabled_and_keyless_and_modeless_skipped(self):
        calls = []
        p_off = make_providers({"p1": (["m1"], 1)})[0]
        p_off.enabled = False
        p_nokey = make_providers({"p2": (["m1"], 2)})[0]
        p_nokey.api_key = "  "
        p_nomodel = make_providers({"p3": (["m1"], 3)})[0]
        p_nomodel.models = []
        ok = make_providers({"p4": (["m1"], 4)})[0]
        (resp, _), _ = run_pool([p_off, p_nokey, p_nomodel, ok], {}, calls)
        self.assertEqual(resp.text, "WIN:p4/m1")
        self.assertEqual(providers_of(calls), ["p4"])

    def test_cooldown_p1_skipped_p2_attempted(self):
        calls = []
        providers = make_providers({"p1": (["m1"], 1), "p2": (["m1"], 2)})
        cfg = PoolConfig(retry_base_delay_ms=1, retry_max_delay_ms=5)
        redis = FakeRedisLite()
        health = HealthManager(redis, cfg)
        for _ in range(5):
            health.record_failure("p1", "m1", CAT_RATE_LIMIT, "429")
        from gaeclip.gemini_pool import execute_pool as _exec

        (resp, _meta) = _exec(
            providers, task_type="curation", request_fn=script_transport({}, calls),
            config=cfg, health=health, sleep_fn=lambda s: None)
        self.assertEqual(resp.text, "WIN:p2/m1")
        self.assertNotIn("p1", providers_of(calls))


class PoolOnlyCuratorTests(unittest.TestCase):
    def test_curator_allows_empty_key_with_pool_ctx(self):
        from gaeclip.curation import GeminiCurator

        cfg = PoolConfig(retry_base_delay_ms=1, retry_max_delay_ms=5)
        ctx = PoolContext(providers=make_providers({"p1": (["m"], 1)}),
                          config=cfg, health=HealthManager(None, cfg))
        curator = GeminiCurator("", model="m", pool_ctx=ctx)
        self.assertIs(curator.pool_ctx, ctx)

    def test_curator_still_rejects_empty_key_without_pool(self):
        from gaeclip.curation import GeminiCurator
        from gaeclip.errors import ValidationError

        with self.assertRaises(ValidationError):
            GeminiCurator("", model="m", pool_ctx=None)

    def test_curator_pool_wiring_reports_p2(self):
        import gaeclip.gemini_pool as pool_mod
        from gaeclip.curation import GeminiCurator
        from gaeclip.schemas import TranscriptLine

        calls = []
        cfg = PoolConfig(retry_max_attempts=0, retry_base_delay_ms=1, retry_max_delay_ms=5)
        providers = make_providers({"p1": (["m1"], 1), "p2": (["m1"], 2)})
        ctx = PoolContext(providers=providers, config=cfg,
                          health=HealthManager(None, cfg), job_id="job-test")
        curator = GeminiCurator("", model="m1", pool_ctx=ctx,
                                min_duration=5, max_duration=60, max_clips=1)

        def fake_via_pool(plist, **kw):
            return generate_via_pool(
                plist, task_type=kw.get("task_type", "curation"),
                contents=kw.get("contents"), config=kw.get("config"),
                health=kw.get("health"), job_id=kw.get("job_id"),
                transport=script_transport(
                    {("p1", "m1"): [503], ("p2", "m1"): ["ok"]}, calls))

        lines = [TranscriptLine(
            line_id="l000001", start=0.0, end=35.0,
            text=" ".join(["kata penting"] * 12),
            language="id", source="faster_whisper")]
        with unittest.mock.patch.object(pool_mod, "generate_via_pool",
                                        side_effect=fake_via_pool):
            # FakeResponse carries no JSON; short-circuit discovery parse
            # with one valid pool item (new flow validates per candidate).
            import gaeclip.curation as cur_mod
            from gaeclip.schemas import CandidateClip

            clip = CandidateClip(
                candidate_id="c000001", start_line_id="l000001",
                end_line_id="l000001", start=0, end=35,
                title="T", reason="R", score=0.9)
            pool_item = {"candidate_id": "c000001", "start_line_id": "l000001",
                         "end_line_id": "l000001", "start": 0, "end": 35,
                         "title": "T", "reason": "R", "score": 0.9,
                         "source": "ai", "semantic": None,
                         "text": lines[0].text, "clip": clip}
            with unittest.mock.patch.object(
                    cur_mod, "parse_discovery_pool",
                    return_value=([pool_item], [])):
                result = curator.curate(lines)
        self.assertIn(("p2", "m1", 0), calls)
        self.assertEqual(result.provider_id, "p2")
        self.assertTrue(result.fallback_used)
        self.assertGreater(result.attempts_total, 1)

    def test_per_provider_keys_used_not_singleton(self):
        """Each attempt authenticates with ITS provider's key (no global P1 client)."""
        import gaeclip.gemini_service as svc_mod

        seen_keys = []

        def fake_generate_content(*args, **kwargs):
            key = kwargs.get("api_key", args[0] if args else None)
            seen_keys.append(key)
            if key == "key-p1":
                raise E(503, "Service Unavailable")
            return FakeResponse("ok")

        providers = make_providers({"p1": (["m1"], 1), "p2": (["m1"], 2)})
        cfg = PoolConfig(retry_max_attempts=0, retry_base_delay_ms=1, retry_max_delay_ms=5)
        with unittest.mock.patch.object(
                svc_mod, "generate_content", side_effect=fake_generate_content):
            (_resp, meta) = generate_via_pool(
                providers, task_type="curation", contents="x",
                config=cfg, health=HealthManager(None, cfg))
        self.assertEqual(seen_keys, ["key-p1", "key-p2"])
        self.assertEqual(meta["provider_id"], "p2")


class HasUsableTests(unittest.TestCase):
    def test_empty_store_not_usable(self):
        store = ProviderStore(FakeRedisLite())
        self.assertFalse(store.has_usable())

    def test_disabled_provider_not_usable(self):
        store = ProviderStore(FakeRedisLite())
        with unittest.mock.patch.object(
                ProviderStore, "list",
                return_value=make_providers({"p1": (["m1"], 1)})) as m:
            providers = m.return_value
            providers[0].enabled = False
            self.assertFalse(store.has_usable())

    def test_enabled_provider_usable(self):
        store = ProviderStore(FakeRedisLite())
        with unittest.mock.patch.object(
                ProviderStore, "list",
                return_value=make_providers({"p1": (["m1"], 1)})):
            self.assertTrue(store.has_usable())


if __name__ == "__main__":
    unittest.main()
