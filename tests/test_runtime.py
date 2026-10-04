"""Dual-runtime tests: mode safety, SQLite contract, parity, LocalRunner.

Server behavior must stay byte-identical (default mode, Redis paths);
desktop must never touch Redis. SQLiteClient is exercised through the
same store flows the app runs (jobs, settings, transcripts, providers,
health), proving the shim honors the client contract.
"""

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "tests")

from test_video_metadata import FakeRedis


def make_sqlite():
    from gaeclip.runtime.sqlite_client import SQLiteClient

    directory = tempfile.mkdtemp()
    return SQLiteClient(Path(directory) / "gaeclip.db")


@contextmanager
def _closing_app(test_client):
    """Yield a TestClient, releasing its store connection on exit.

    The app owns one store client for its whole lifetime; on Windows an
    open SQLite handle blocks temp-dir cleanup (WinError 32), so the
    handle must be closed before TemporaryDirectory.__exit__ runs.
    """
    try:
        yield test_client
    finally:
        try:
            state = getattr(getattr(test_client, "app", None), "state", None)
            store = getattr(state, "store_client", None)
            close = getattr(store, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
        import gc

        gc.collect()


class ModeTests(unittest.TestCase):
    def test_default_is_server(self):
        from gaeclip.runtime import RuntimeMode, current_runtime

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GAECLIP_RUNTIME", None)
            self.assertEqual(current_runtime(), RuntimeMode.SERVER)

    def test_desktop_explicit(self):
        from gaeclip.runtime import RuntimeMode, current_runtime

        with patch.dict(os.environ, {"GAECLIP_RUNTIME": "desktop"}):
            self.assertEqual(current_runtime(), RuntimeMode.DESKTOP)

    def test_unknown_value_fails_fast(self):
        from gaeclip.errors import ConfigurationError
        from gaeclip.runtime import current_runtime

        with patch.dict(os.environ, {"GAECLIP_RUNTIME": "sqlite"}):
            with self.assertRaises(ConfigurationError):
                current_runtime()

    def test_desktop_ignores_redis_url(self):
        from gaeclip.runtime import factory

        with patch.dict(os.environ, {"GAECLIP_RUNTIME": "desktop",
                                     "REDIS_URL": "redis://evil:6379/0",
                                     "STORAGE_ROOT": tempfile.mkdtemp()}):
            client = factory.make_client()
            self.assertEqual(client.kind, "sqlite")

    def test_server_uses_redis_url(self):
        from gaeclip.runtime import factory

        with patch.dict(os.environ, {"GAECLIP_RUNTIME": "server",
                                     "REDIS_URL": "redis://example:6379/5"}):
            import redis

            seen = {}
            real_from_url = redis.Redis.from_url

            def capture(url, **kwargs):
                seen["url"] = url
                return FakeRedis()

            with patch.object(redis.Redis, "from_url", side_effect=capture):
                client = factory.make_client()
            self.assertEqual(seen.get("url"), "redis://example:6379/5")
            self.assertIsInstance(client, FakeRedis)


class SQLiteContractTests(unittest.TestCase):
    def setUp(self):
        self.client = make_sqlite()

    def test_kv_roundtrip(self):
        self.assertIsNone(self.client.get("k"))
        self.assertTrue(self.client.set("k", "v"))
        self.assertEqual(self.client.get("k"), "v")
        self.assertEqual(self.client.delete("k"), 1)
        self.assertIsNone(self.client.get("k"))
        self.assertTrue(self.client.ping())

    def test_nx_ex(self):
        import time

        self.assertTrue(self.client.set("k", "a", nx=True))
        self.assertFalse(self.client.set("k", "b", nx=True))
        self.assertEqual(self.client.get("k"), "a")
        self.client.set("e", "x", ex=1)
        time.sleep(1.05)
        self.assertIsNone(self.client.get("e"))

    def test_hashes(self):
        self.assertIsNone(self.client.hget("h", "k"))
        self.client.hset("h", "k", "v")
        self.assertEqual(self.client.hget("h", "k"), "v")
        self.client.hset("h", mapping={"a": "1", "b": "2"})
        self.assertEqual(self.client.hgetall("h"), {"k": "v", "a": "1", "b": "2"})
        self.assertEqual(self.client.hdel("h", "a", "missing"), 1)

    def test_sets(self):
        self.client.sadd("s", "a", "b", "a")
        self.assertEqual(self.client.smembers("s"), {"a", "b"})

    def test_counters(self):
        self.assertEqual(self.client.incr("c"), 1)
        self.assertEqual(self.client.incr("c"), 2)
        self.assertEqual(self.client.decr("c"), 1)
        self.assertTrue(self.client.expire("c", 60))

    def test_scan(self):
        self.client.set("gaeclip:job:1", "{}")
        self.client.set("gaeclip:job:2", "{}")
        self.client.set("other", "{}")
        self.assertEqual(sorted(self.client.scan_iter(match="gaeclip:job:*")),
                         ["gaeclip:job:1", "gaeclip:job:2"])

    def test_pipeline_nx(self):
        pipe = self.client.pipeline()
        pipe.set("a", "1", ex=10, nx=True)
        pipe.set("a", "2", ex=10, nx=True)
        self.assertEqual(pipe.execute(), [True, False])
        self.assertEqual(self.client.get("a"), "1")


class StoreParityTests(unittest.TestCase):
    """Same flows, both backends: jobs, settings, transcripts, providers."""

    def test_jobs_crud_parity(self):
        from gaeclip.jobs import JobRecord, JobRepository

        for client in (FakeRedis(), make_sqlite()):
            repo = JobRepository(client)
            record = JobRecord("job_p1", "https://youtu.be/AAA", "id",
                               created_at=1.0, updated_at=1.0)
            saved, existing = repo.create(record, "idem-1", "fp-1")
            self.assertFalse(existing)
            again, existing = repo.create(record, "idem-1", "fp-1")
            self.assertTrue(existing)
            repo.transition("job_p1", "running", "transcribing", 35)
            live = repo.get("job_p1")
            self.assertEqual((live.status, live.stage, live.progress),
                             ("running", "transcribing", 35))
            repo.update_progress("job_p1", "rendering", 80, "Render", 1, 3)
            live = repo.get("job_p1")
            self.assertEqual((live.current_clip, live.total_clips), (1, 3))
            self.assertEqual(len(repo.list(limit=10)), 1)
            repo.delete("job_p1")
            self.assertIsNone(repo.get("job_p1"))

    def test_settings_parity(self):
        from gaeclip.app_settings import AppSettings

        for client in (FakeRedis(), make_sqlite()):
            service = AppSettings(client)
            self.assertEqual(service.get("MAX_CLIPS"), 3)
            service.set("MAX_CLIPS", 5)
            self.assertEqual(service.get("MAX_CLIPS"), 5)
            service.set_many({"MIN_CLIP_DURATION_SECONDS": 10,
                              "MAX_CLIP_DURATION_SECONDS": 60})
            self.assertEqual(service.get("MIN_CLIP_DURATION_SECONDS"), 10)
            service.reset("MAX_CLIPS")
            self.assertEqual(service.get("MAX_CLIPS"), 3)

    def test_transcript_cache_parity(self):
        import tempfile

        from gaeclip.schemas import TranscriptLine
        from gaeclip.transcript_cache import TranscriptCache

        lines = [TranscriptLine(line_id="l000001", start=0.0, end=5.0,
                                text="Halo dunia.", language="id",
                                source="faster_whisper")]
        for client in (FakeRedis(), make_sqlite()):
            with tempfile.TemporaryDirectory() as directory:
                cache = TranscriptCache(client, Path(directory))
                record = cache.store("https://youtu.be/AAA", "small", "id",
                                     lines, duration=5.0)
                self.assertIsNotNone(record)
                found, _, outcome = cache.lookup("https://youtube.com/watch?v=AAA",
                                                 "small", "id", duration=5.0)
                self.assertEqual(outcome, "hit")
                self.assertEqual(len(found), 1)

    def test_providers_parity(self):
        from gaeclip.gemini_pool import ProviderStore

        for client in (FakeRedis(), make_sqlite()):
            store = ProviderStore(client)
            self.assertFalse(store.has_usable())
            self.assertEqual(store.list(), [])


class CancelParityTests(unittest.TestCase):
    def test_cancelled_record_blocks_process_job(self):
        # process_job must no-op on cancelled records regardless of backend.
        from gaeclip import worker as worker_mod
        from gaeclip.jobs import JobRecord, JobRepository

        for client in (FakeRedis(), make_sqlite()):
            repo = JobRepository(client)
            repo.save(JobRecord("job_c9", "https://youtu.be/AAA", "id",
                                status="cancelled", stage="cancelled",
                                progress=10, created_at=1.0, updated_at=1.0))
            with patch.object(worker_mod, "_redis", return_value=client):
                worker_mod.process_job("job_c9")  # must return quietly
            self.assertEqual(repo.get("job_c9").status, "cancelled")


class LocalRunnerTests(unittest.TestCase):
    def test_submit_runs_process_job(self):
        from gaeclip import worker as worker_mod
        from gaeclip.jobs import JobRecord, JobRepository
        from gaeclip.runtime.runners import LocalJobRunner

        LocalJobRunner._reset_for_tests()
        client = make_sqlite()
        JobRepository(client).save(JobRecord(
            "job_lr1", "https://youtu.be/AAA", "id", status="queued",
            stage="enqueueing", progress=0, created_at=1.0, updated_at=1.0))
        calls = []

        def fake_process(job_id):
            calls.append(job_id)
            JobRepository(client).transition(job_id, "completed", "completed", 100)

        runner = LocalJobRunner(client)
        with patch.object(worker_mod, "_redis", return_value=client), \
                patch.object(worker_mod, "process_job", side_effect=fake_process):
            task_id = runner.submit("job_lr1")
            self.assertTrue(task_id.startswith("local-"))
            self.assertTrue(runner.wait_until_idle(timeout=10))
        self.assertEqual(calls, ["job_lr1"])
        self.assertEqual(JobRepository(client).get("job_lr1").status, "completed")
        self.assertEqual(runner.active_jobs(), [])
        LocalJobRunner._reset_for_tests()

    def test_revoke_drops_queued_job(self):
        from gaeclip.jobs import JobRecord, JobRepository
        from gaeclip.runtime.runners import LocalJobRunner

        LocalJobRunner._reset_for_tests()
        client = make_sqlite()
        JobRepository(client).save(JobRecord(
            "job_lr2", "https://youtu.be/AAA", "id", status="queued",
            stage="enqueueing", progress=0, created_at=1.0, updated_at=1.0))
        runner = LocalJobRunner(client)
        with patch.object(runner, "_max_concurrent", return_value=0):
            task_id = runner.submit("job_lr2")
            self.assertEqual(runner.queued_jobs(), ["job_lr2"])
            runner.revoke(task_id)
            self.assertEqual(runner.queued_jobs(), [])
        self.assertEqual(JobRepository(client).get("job_lr2").status, "cancelled")
        LocalJobRunner._reset_for_tests()

    def test_worker_status_shape(self):
        from gaeclip.runtime.runners import LocalJobRunner

        LocalJobRunner._reset_for_tests()
        runner = LocalJobRunner()
        self.assertEqual(runner.active_jobs(), [])
        self.assertEqual(runner.queued_jobs(), [])
        LocalJobRunner._reset_for_tests()


class DesktopApiTests(unittest.TestCase):
    """Full API in desktop mode: SQLite store + local runner, no Redis."""

    def _desktop_env(self, directory):
        env = {key: value for key, value in os.environ.items()
               if key not in ("GAECLIP_RUNTIME", "REDIS_URL")}
        env.update({"GAECLIP_RUNTIME": "desktop",
                    "REDIS_URL": "redis://should-be-ignored:6379/0",
                    "STORAGE_ROOT": directory})
        return env

    def _make_app(self, directory):
        from fastapi.testclient import TestClient

        import gaeclip.api as api_mod

        with patch.dict(os.environ, self._desktop_env(directory), clear=True):
            app = api_mod.create_app()
        return TestClient(app)

    def test_desktop_flow_queued_completed_cancelled(self):
        import tempfile

        from gaeclip.runtime.runners import LocalJobRunner

        LocalJobRunner._reset_for_tests()
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, self._desktop_env(directory), clear=True), \
                _closing_app(self._make_app(directory)) as client:
            self.assertEqual(client.get("/health/config").json()["runtime"], "desktop")

            created: dict = {}

            def fake_submit(job_id, task_name="process_job"):
                created["job_id"] = job_id
                from gaeclip.jobs import JobRepository
                from gaeclip.runtime.factory import make_client

                JobRepository(make_client()).transition(
                    job_id, "completed", "completed", 100)
                return "local-test-1"

            with patch.object(LocalJobRunner, "submit", autospec=True,
                              side_effect=lambda self, j, task_name="process_job":
                              fake_submit(j, task_name)):
                res = client.post(
                    "/api/v1/jobs",
                    json={"source_url": "https://youtu.be/ZZZ9x"},
                    headers={"Idempotency-Key": "desk-flow-1"},
                )
            self.assertEqual(res.status_code, 202, res.text)
            job_id = res.json()["job_id"]
            self.assertEqual(created["job_id"], job_id)
            self.assertEqual(res.json()["worker_task_id"], "local-test-1")
            live = client.get(f"/api/v1/jobs/{job_id}").json()
            self.assertEqual(live["status"], "queued")

            status = client.get("/api/v1/worker/status").json()
            self.assertTrue(status["worker_up"])
            self.assertEqual(status["runtime"], "desktop")
            self.assertEqual(status["runner"], "local")
        LocalJobRunner._reset_for_tests()

    def test_desktop_cancel_and_cookies(self):
        import tempfile

        from gaeclip.runtime.runners import LocalJobRunner

        LocalJobRunner._reset_for_tests()
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, self._desktop_env(directory), clear=True), \
                _closing_app(self._make_app(directory)) as client:
            with patch.object(LocalJobRunner, "submit", return_value="local-test-2"):
                res = client.post(
                    "/api/v1/jobs",
                    json={"source_url": "https://youtu.be/ZZZ9y"},
                    headers={"Idempotency-Key": "desk-flow-2"},
                )
            job_id = res.json()["job_id"]
            cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel").json()
            self.assertEqual(cancelled["status"], "cancelled")

            self.assertFalse(client.get("/api/v1/settings/youtube-cookies").json()["configured"])
            saved = client.post("/api/v1/settings/youtube-cookies", json={
                "cookies_txt": "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertTrue(client.get("/api/v1/settings/youtube-cookies").json()["configured"])
            bad = client.post("/api/v1/settings/youtube-cookies",
                              json={"cookies_txt": "not cookies"})
            self.assertEqual(bad.status_code, 422)
        LocalJobRunner._reset_for_tests()


class DesktopToolsTests(unittest.TestCase):
    """Bundled bin/ lands on PATH when frozen (PyInstaller sidecar)."""

    def test_bundled_bin_prepended_when_frozen(self):
        import sys

        from gaeclip import desktop as desktop_mod

        with tempfile.TemporaryDirectory() as directory:
            bindir = Path(directory) / "bin"
            bindir.mkdir()
            exe = str(Path(directory) / "gaeclip-api.exe")
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "executable", exe), \
                    patch.dict(os.environ, {"PATH": "KEEP"}):
                got = desktop_mod._ensure_bundled_tools_on_path()
                # resolve(): Windows expands 8.3 short names (RUNNER~1).
                want = str(bindir.resolve())
                self.assertEqual(got, want)
                self.assertTrue(os.environ["PATH"].startswith(want + os.pathsep))
                self.assertTrue(os.environ["PATH"].endswith("KEEP"))

    def test_noop_when_not_frozen(self):
        import sys

        from gaeclip import desktop as desktop_mod

        if getattr(sys, "frozen", False):
            self.skipTest("only meaningful on a non-frozen interpreter")
        with patch.dict(os.environ, {"PATH": "KEEP"}):
            self.assertIsNone(desktop_mod._ensure_bundled_tools_on_path())
            self.assertEqual(os.environ["PATH"], "KEEP")


class SecretsCookiesTests(unittest.TestCase):
    def test_dpapi_passthrough_off_windows(self):
        import sys

        from gaeclip.runtime import secrets as secrets_mod

        if sys.platform == "win32":
            self.skipTest("Windows-only behavior differs")
        self.assertIsNone(secrets_mod._dpapi_protect(b"x"))
        self.assertIsNone(secrets_mod._dpapi_unprotect(b"x"))

    def test_desktop_secret_roundtrip_no_env(self):
        import base64

        from gaeclip.runtime import secrets as secrets_mod

        with tempfile.TemporaryDirectory() as home:
            env = {key: value for key, value in os.environ.items()
                   if key != "SETTINGS_SECRET"}
            env["LOCALAPPDATA"] = home
            with patch.dict(os.environ, env, clear=True):
                with patch("sys.platform", "linux"):
                    first = secrets_mod.ensure_desktop_secret()
                    second = secrets_mod.ensure_desktop_secret()
        self.assertEqual(first, second)
        base64.urlsafe_b64decode(first)  # must be valid base64

    def test_cookie_resolution_order(self):
        from gaeclip.runtime import cookies as cookies_mod

        with tempfile.TemporaryDirectory() as directory:
            env = {key: value for key, value in os.environ.items()
                   if key != "YTDLP_COOKIES_FILE"}
            with patch.dict(os.environ, env, clear=True):
                self.assertIsNone(cookies_mod.resolve_cookies_file(directory))
                path = cookies_mod.save_managed_cookies(
                    "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n",
                    directory)
                self.assertTrue(path.is_file())
                self.assertEqual(cookies_mod.resolve_cookies_file(directory), str(path))
            with patch.dict(os.environ, {"YTDLP_COOKIES_FILE": "/explicit/cookies.txt"}):
                self.assertEqual(cookies_mod.resolve_cookies_file(directory),
                                 "/explicit/cookies.txt")
            with self.assertRaises(ValueError):
                cookies_mod.save_managed_cookies("garbage without tabs", directory)

    def test_export_cookies_sets_env(self):
        from gaeclip.runtime import cookies as cookies_mod

        with tempfile.TemporaryDirectory() as directory:
            env = {key: value for key, value in os.environ.items()
                   if key != "YTDLP_COOKIES_FILE"}
            env["STORAGE_ROOT"] = directory
            with patch.dict(os.environ, env, clear=True):
                self.assertIsNone(cookies_mod.export_cookies_file())
                cookies_mod.save_managed_cookies("# Netscape HTTP Cookie File\n", directory)
                self.assertEqual(cookies_mod.export_cookies_file(),
                                 str(cookies_mod.managed_cookies_path(directory)))


if __name__ == "__main__":
    unittest.main()
