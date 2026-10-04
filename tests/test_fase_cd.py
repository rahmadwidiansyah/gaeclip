import unittest

from gaeclip.api import CreateJobRequest, MAX_CLIPS_HARD_LIMIT
from gaeclip.jobs import JobRecord, JobRepository
from pydantic import ValidationError as PydanticValidationError


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        value = self.store.get(key)
        return value.encode() if isinstance(value, str) else value

    def set(self, key, value, ex=None, nx=None):
        if nx and key in self.store:
            return False
        self.store[key] = value if isinstance(value, str) else value.decode()
        return True

    def pipeline(self):
        client = self

        class Pipe:
            def __init__(self):
                self.ops = []

            def set(self, key, value, ex=None, nx=None):
                self.ops.append((key, value, ex, nx))
                return self

            def execute(self):
                return [client.set(k, v, ex=e, nx=n) for k, v, e, n in self.ops]

        return Pipe()

    def scan_iter(self, match="", count=100):
        import fnmatch
        return [k for k in self.store if fnmatch.fnmatch(k, match)]


class FaseCDTests(unittest.TestCase):
    def test_create_job_request_accepts_custom_clip_count(self):
        req = CreateJobRequest(
            source_url="https://www.youtube.com/watch?v=x",
            language="id", max_clips=5, min_duration=20, max_duration=45,
        )
        self.assertEqual(req.max_clips, 5)

    def test_create_job_request_rejects_too_many_clips(self):
        with self.assertRaises(PydanticValidationError):
            CreateJobRequest(
                source_url="https://www.youtube.com/watch?v=x",
                max_clips=MAX_CLIPS_HARD_LIMIT + 1,
            )

    def test_fingerprint_differs_by_params(self):
        repo = JobRepository(FakeRedis())
        a = repo.request_fingerprint("https://www.youtube.com/watch?v=x", "id", 3, 30, 60)
        b = repo.request_fingerprint("https://www.youtube.com/watch?v=x", "id", 5, 30, 60)
        self.assertNotEqual(a, b)
        c = repo.request_fingerprint("https://www.youtube.com/watch?v=x", "id", 3, 30, 60,
                                     transcript_source="whisper_only")
        self.assertNotEqual(a, c)

    def test_job_record_carries_progress_fields(self):
        record = JobRecord("job_x", "https://www.youtube.com/watch?v=x", "id")
        self.assertEqual(record.max_clips, 3)
        self.assertEqual(record.current_clip, 0)
        repo = JobRepository(FakeRedis())
        repo.client.set(repo._key("job_x"), '{"job_id": "job_x", "source_url": "https://www.youtube.com/watch?v=x", "language": "id"}')
        loaded = repo.get("job_x")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.max_clips, 3)  # defaults applied for old records

    def test_update_progress_sets_detail_and_counters(self):
        repo = JobRepository(FakeRedis())
        record = JobRecord("job_y", "https://www.youtube.com/watch?v=x", "id")
        repo.save(record)
        repo.update_progress("job_y", "rendering", 80, "Merender clip 2/5", 2, 5)
        loaded = repo.get("job_y")
        self.assertEqual(loaded.stage, "rendering")
        self.assertEqual(loaded.stage_detail, "Merender clip 2/5")
        self.assertEqual((loaded.current_clip, loaded.total_clips), (2, 5))

    def test_pipeline_rejects_min_over_max(self):
        from gaeclip.pipeline import generate_from_url
        from gaeclip.config import Settings
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.from_env()
            object.__setattr__(settings, "storage_root", Path(directory))
            with self.assertRaises(Exception):
                generate_from_url(
                    "https://www.youtube.com/watch?v=x", settings=settings,
                    max_clips=3, min_duration=90, max_duration=30,
                )


if __name__ == "__main__":
    unittest.main()
