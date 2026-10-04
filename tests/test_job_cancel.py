"""Job cancellation: revoke real task ids, abort transcription, no swallowing."""

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, "tests")

from test_video_metadata import FakeRedis, make_client


def install_fake_whisper(n_segments=120, fail_after=None):
    """Stub faster_whisper with a streaming segment generator."""
    seen = {"count": 0}

    class FakeSegment:
        def __init__(self, i):
            self.start = float(i) * 10.0
            self.end = float(i) * 10.0 + 0.9
            self.text = f"kata {i}"

    class FakeModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, *args, **kwargs):
            def gen():
                for i in range(n_segments):
                    seen["count"] += 1
                    yield FakeSegment(i)

            return gen(), None

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeModel
    sys.modules["faster_whisper"] = module
    return seen


class TranscriptionCancelTests(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("faster_whisper", None)

    def test_cancel_mid_stream(self):
        from gaeclip.errors import CancelledError
        from gaeclip.transcription import transcribe_with_whisper

        install_fake_whisper(n_segments=120)
        calls = []

        def should_cancel():
            calls.append(1)
            return len(calls) >= 3

        with self.assertRaises(CancelledError):
            transcribe_with_whisper(Path("/tmp/x.mp4"), should_cancel=should_cancel)
        # Stops within a few checks, not after all 120 segments.
        self.assertLess(len(calls), 10)

    def test_no_cancel_runs_full_with_offsets(self):
        from gaeclip.transcription import transcribe_with_whisper

        seen = install_fake_whisper(n_segments=10)
        cues = transcribe_with_whisper(Path("/tmp/x.mp4"), should_cancel=lambda: False)
        self.assertEqual(len(cues), 10)
        self.assertAlmostEqual(cues[0].start, 0.0)
        self.assertAlmostEqual(cues[9].start, 90.0)
        self.assertEqual(seen["count"], 10)


class CuratorCancelTests(unittest.TestCase):
    def test_cancelled_error_propagates(self):
        from gaeclip.curation import curate_with_fallback
        from gaeclip.errors import CancelledError

        class BadCurator:
            provider_name = "gemini"

            def curate(self, transcript):
                raise CancelledError("Job dibatalkan pengguna")

        with self.assertRaises(CancelledError):
            curate_with_fallback([], BadCurator(), 15, 90, 3)


class CancelEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client, self.fake = make_client()

    def _make_job(self):
        res = self.client.post(
            "/api/v1/jobs",
            json={"source_url": "https://youtu.be/ZZZ9x"},
            headers={"Idempotency-Key": "cancel-test-1"},
        )
        self.assertEqual(res.status_code, 202, res.text)
        return res.json()["job_id"]

    def test_create_stores_worker_task_id(self):
        from gaeclip import worker as worker_mod

        fake_result = MagicMock()
        fake_result.id = "celery-task-uuid-1"
        fake_task = MagicMock()
        fake_task.delay.return_value = fake_result
        with patch.object(worker_mod, "celery_task", return_value=(MagicMock(), fake_task)):
            job_id = self._make_job()
        from gaeclip.jobs import JobRepository

        record = JobRepository(self.fake).get(job_id)
        self.assertEqual(record.worker_task_id, "celery-task-uuid-1")

    def test_cancel_revokes_worker_task_id(self):
        from gaeclip import worker as worker_mod

        fake_result = MagicMock()
        fake_result.id = "celery-task-uuid-2"
        fake_task = MagicMock()
        fake_task.delay.return_value = fake_result
        with patch.object(worker_mod, "celery_task", return_value=(MagicMock(), fake_task)):
            job_id = self._make_job()
        app_mock = MagicMock()
        with patch.object(worker_mod, "celery_task", return_value=(app_mock, MagicMock())):
            res = self.client.post(f"/api/v1/jobs/{job_id}/cancel")
        self.assertEqual(res.status_code, 200)
        app_mock.control.revoke.assert_called_once_with("celery-task-uuid-2", terminate=True)
        self.assertEqual(res.json()["status"], "cancelled")

    def test_worker_status_endpoint(self):
        fake_inspect = MagicMock()
        fake_inspect.active.return_value = {"w@h": [{"name": "gaeclip.process_job", "args": ["job_OTHER"]}]}
        fake_inspect.reserved.return_value = {}
        fake_app = MagicMock()
        fake_app.control.inspect.return_value = fake_inspect
        # Endpoint does `from celery import Celery` at call time.
        import celery

        with patch.object(celery, "Celery", return_value=fake_app):
            res = self.client.get("/api/v1/worker/status")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["worker_up"])
        self.assertEqual(body["active_jobs"], ["job_OTHER"])

    def test_worker_status_degraded(self):
        import celery

        with patch.object(celery, "Celery", side_effect=RuntimeError("down")):
            res = self.client.get("/api/v1/worker/status")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["worker_up"])


class DeleteEndpointTests(unittest.TestCase):
    """Regression: DELETE terminal job tanpa folder storage tetap 204.

    Bug nyata: job_qE852vJ9mssQVrOe (cancelled) orphan — Redis ada,
    storage/jobs/<id> hilang — DELETE 500 FileNotFoundError tanpa header
    CORS sehingga browser menampilkan opaque "Failed to fetch".
    """

    def setUp(self):
        self.client, self.fake = make_client()

    def _seed(self, job_id, status):
        import time

        from gaeclip.jobs import JobRecord, JobRepository

        now = time.time()
        record = JobRecord(
            job_id, "https://youtu.be/ZZZ9x", "id",
            status=status, stage=status, progress=100,
            created_at=now, updated_at=now,
        )
        return JobRepository(self.fake).save(record)

    def test_delete_missing_dir_still_204(self):
        from gaeclip.jobs import JobRepository

        self._seed("job_orphan123", "cancelled")
        res = self.client.delete("/api/v1/jobs/job_orphan123")
        self.assertEqual(res.status_code, 204, res.text)
        self.assertIsNone(JobRepository(self.fake).get("job_orphan123"))

    def test_delete_missing_job_404(self):
        res = self.client.delete("/api/v1/jobs/job_nope123")
        self.assertEqual(res.status_code, 404)

    def test_delete_non_terminal_409(self):
        self._seed("job_running1", "running")
        res = self.client.delete("/api/v1/jobs/job_running1")
        self.assertEqual(res.status_code, 409)


if __name__ == "__main__":
    unittest.main()
