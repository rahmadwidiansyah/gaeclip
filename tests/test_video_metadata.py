import json
import time
import unittest
from unittest.mock import MagicMock, patch

from gaeclip.url import extract_video_id
from gaeclip.videos import VideoStore, build_snapshot


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}

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

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed

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

    def sadd(self, name, *members):
        table = self.sets.setdefault(name, set())
        added = 0
        for member in members:
            value = member.decode() if isinstance(member, bytes) else str(member)
            if value not in table:
                table.add(value)
                added += 1
        return added

    def smembers(self, name):
        return {m.encode() for m in self.sets.get(name, set())}

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


SAMPLE_META = {
    "id": "ABC123xyz",
    "title": "Review Makanan 300 Juta",
    "channel": "Food Channel",
    "uploader": "Food Channel Uploader",
    "duration": 3478,
    "upload_date": "20240914",
    "thumbnails": [
        {"url": "https://img/small.jpg", "width": 120},
        {"url": "https://img/mid.jpg", "width": 640},
        {"url": "https://img/big.jpg", "width": 1280},
    ],
}


class ExtractVideoIdTests(unittest.TestCase):
    def test_all_url_forms(self):
        cases = {
            "https://www.youtube.com/watch?v=ABC123xyz": "ABC123xyz",
            "https://youtube.com/watch?v=ABC123xyz&t=10s": "ABC123xyz",
            "https://youtu.be/ABC123xyz": "ABC123xyz",
            "https://youtu.be/ABC123xyz?t=5": "ABC123xyz",
            "https://www.youtube.com/shorts/ABC123xyz": "ABC123xyz",
            "https://www.youtube.com/embed/ABC123xyz": "ABC123xyz",
            "https://www.youtube.com/live/ABC123xyz": "ABC123xyz",
            "https://m.youtube.com/watch?v=ABC123xyz": "ABC123xyz",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(extract_video_id(url), expected)

    def test_same_video_different_urls(self):
        self.assertEqual(
            extract_video_id("https://youtube.com/watch?v=ABC"),
            extract_video_id("https://youtu.be/ABC"),
        )

    def test_invalid_returns_none(self):
        self.assertIsNone(extract_video_id("not a url"))
        self.assertIsNone(extract_video_id("https://example.com/video"))


class SnapshotTests(unittest.TestCase):
    def test_maps_fields(self):
        snap = build_snapshot("ABC123xyz", "https://youtu.be/ABC123xyz", SAMPLE_META)
        self.assertEqual(snap["title"], "Review Makanan 300 Juta")
        self.assertEqual(snap["channel_name"], "Food Channel")
        self.assertEqual(snap["thumbnail_url"], "https://img/mid.jpg")
        self.assertEqual(snap["published_at"], "2024-09-14")
        self.assertEqual(snap["duration"], 3478.0)

    def test_missing_fields_are_null(self):
        snap = build_snapshot("X", "https://youtu.be/X", {})
        self.assertIsNone(snap["title"])
        self.assertIsNone(snap["channel_name"])
        self.assertIsNone(snap["thumbnail_url"])
        self.assertIsNone(snap["published_at"])
        self.assertIsNone(snap["duration"])


class VideoStoreTests(unittest.TestCase):
    def test_upsert_no_duplicates(self):
        store = VideoStore(FakeRedis())
        first = store.upsert("ABC", "https://youtu.be/ABC", SAMPLE_META)
        second = store.upsert("ABC", "https://youtube.com/watch?v=ABC", {**SAMPLE_META, "title": "Renamed"})
        self.assertEqual(first.created_at, second.created_at)
        self.assertEqual(store.get("ABC").title, "Renamed")
        self.assertEqual(store.list_ids(), ["ABC"])

    def test_get_missing_returns_none(self):
        self.assertIsNone(VideoStore(FakeRedis()).get("nope"))
        self.assertIsNone(VideoStore(FakeRedis()).get(""))


def make_client():
    from fastapi.testclient import TestClient
    import gaeclip.api as api_mod

    fake = FakeRedis()
    redis_mock = MagicMock()
    redis_mock.from_url.return_value = fake
    with patch("redis.Redis", redis_mock):
        app = api_mod.create_app()
    return TestClient(app), fake


def seed_job(fake, job_id, url, video=None, video_id=None, status="completed", created=None):
    from gaeclip.jobs import JobRecord, JobRepository

    repo = JobRepository(fake)
    now = created or time.time()
    record = JobRecord(
        job_id, url, "id", status=status, stage=status, progress=100,
        created_at=now, updated_at=now, video_id=video_id, video=video,
    )
    repo.save(record)
    return record


class SearchApiTests(unittest.TestCase):
    def setUp(self):
        self.client, self.fake = make_client()
        seed_job(
            self.fake, "job_1", "https://youtu.be/AAA",
            video={"youtube_video_id": "AAA", "title": "Review Makanan 300 Juta",
                   "channel_name": "Food Channel", "url": "https://youtu.be/AAA"},
            video_id="AAA", created=1000,
        )
        seed_job(
            self.fake, "job_2", "https://youtu.be/BBB",
            video={"youtube_video_id": "BBB", "title": "Review Laptop XYZ",
                   "channel_name": "Tech Channel", "url": "https://youtu.be/BBB"},
            video_id="BBB", status="running", created=2000,
        )
        seed_job(self.fake, "job_3", "https://youtu.be/CCC", video_id="CCC", created=3000)

    def test_search_by_title_case_insensitive(self):
        res = self.client.get("/api/v1/jobs/search", params={"q": "makanan"})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["jobs"][0]["job_id"], "job_1")

    def test_search_by_channel(self):
        res = self.client.get("/api/v1/jobs/search", params={"q": "food channel"})
        self.assertEqual(res.json()["total"], 1)

    def test_search_by_video_id_and_job_id(self):
        self.assertEqual(self.client.get("/api/v1/jobs/search", params={"q": "bbb"}).json()["total"], 1)
        self.assertEqual(self.client.get("/api/v1/jobs/search", params={"q": "job_3"}).json()["total"], 1)

    def test_status_filter_and_processing_mapping(self):
        self.assertEqual(self.client.get("/api/v1/jobs/search", params={"status": "completed"}).json()["total"], 2)
        body = self.client.get("/api/v1/jobs/search", params={"status": "processing"}).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["jobs"][0]["job_id"], "job_2")

    def test_channel_filter(self):
        body = self.client.get("/api/v1/jobs/search", params={"channel": "tech channel"}).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["jobs"][0]["job_id"], "job_2")

    def test_sort_and_pagination(self):
        body = self.client.get("/api/v1/jobs/search", params={"sort": "oldest", "per_page": 2, "page": 1}).json()
        self.assertEqual(body["total"], 3)
        self.assertEqual([j["job_id"] for j in body["jobs"]], ["job_1", "job_2"])
        body = self.client.get("/api/v1/jobs/search", params={"sort": "oldest", "per_page": 2, "page": 2}).json()
        self.assertEqual([j["job_id"] for j in body["jobs"]], ["job_3"])
        body = self.client.get("/api/v1/jobs/search", params={"sort": "title_az"}).json()
        titles = [(j.get("video") or {}).get("title") or j["job_id"] for j in body["jobs"]]
        self.assertEqual(titles, sorted(titles, key=str.lower))

    def test_date_filter(self):
        body = self.client.get("/api/v1/jobs/search", params={"date_from": 1500, "date_to": 2500}).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["jobs"][0]["job_id"], "job_2")

    def test_channels_endpoint(self):
        body = self.client.get("/api/v1/channels").json()
        names = {c["name"]: c["jobs"] for c in body["channels"]}
        self.assertEqual(names.get("Food Channel"), 1)
        self.assertEqual(names.get("Tech Channel"), 1)

    def test_videos_endpoint_groups_jobs(self):
        from gaeclip.videos import VideoStore
        VideoStore(self.fake).upsert("AAA", "https://youtu.be/AAA", SAMPLE_META)
        seed_job(self.fake, "job_4", "https://youtube.com/watch?v=AAA", video_id="AAA", created=4000)
        body = self.client.get("/api/v1/videos").json()
        entry = next(v for v in body["videos"] if v["video"].get("youtube_video_id") == "AAA")
        self.assertEqual(entry["jobs"], 2)
        self.assertIn("job_1", entry["job_ids"])
        self.assertIn("job_4", entry["job_ids"])

    def test_missing_metadata_falls_back_gracefully(self):
        body = self.client.get("/api/v1/jobs/search", params={"q": "job_3"}).json()
        job = body["jobs"][0]
        self.assertIsNone(job["video"])

    def test_backfill_skips_filled_and_uses_cache(self):
        from gaeclip.videos import VideoStore
        VideoStore(self.fake).upsert("CCC", "https://youtu.be/CCC", SAMPLE_META)
        with patch("gaeclip.worker.refresh_task") as mock_task:
            mock_task.return_value = (MagicMock(), MagicMock())
            res = self.client.post("/api/v1/videos/backfill", params={"limit": 20})
        body = res.json()
        self.assertEqual(res.status_code, 202)
        self.assertEqual(body["filled_from_cache"], 1)
        self.assertEqual(body["queued"], 0)
        # job_3 now carries the cached snapshot
        job = self.client.get("/api/v1/jobs/job_3").json()
        self.assertEqual(job["video"]["title"], "Review Makanan 300 Juta")

    def test_backfill_enqueues_missing(self):
        with patch("gaeclip.worker.refresh_task") as mock_task:
            task_mock = MagicMock()
            mock_task.return_value = (MagicMock(), task_mock)
            res = self.client.post("/api/v1/videos/backfill", params={"limit": 20})
        self.assertEqual(res.json()["queued"], 1)
        task_mock.delay.assert_called_once_with("job_3")

    def test_refresh_endpoint(self):
        res = self.client.post("/api/v1/jobs/nope/refresh-metadata")
        self.assertEqual(res.status_code, 404)
        with patch("gaeclip.worker.refresh_task") as mock_task:
            task_mock = MagicMock()
            mock_task.return_value = (MagicMock(), task_mock)
            res = self.client.post("/api/v1/jobs/job_3/refresh-metadata")
        self.assertEqual(res.status_code, 202)
        task_mock.delay.assert_called_once_with("job_3")

    def test_create_attaches_video_id(self):
        res = self.client.post(
            "/api/v1/jobs",
            json={"source_url": "https://youtu.be/ZZZ9x"},
            headers={"Idempotency-Key": "vid-test-1"},
        )
        self.assertEqual(res.status_code, 202, res.text)
        self.assertEqual(res.json()["video_id"], "ZZZ9x")


class YtdlpResilienceTests(unittest.TestCase):
    def test_base_args_retry_and_spacing(self):
        from gaeclip.downloader import _ytdlp_base_args

        args = _ytdlp_base_args()
        for flag in ("--retries", "--fragment-retries", "--retry-sleep",
                     "--sleep-requests", "--no-playlist",
                     "--js-runtimes", "--remote-components"):
            self.assertIn(flag, args)

    def test_base_args_cookies_from_env(self):
        import os
        from gaeclip.downloader import _ytdlp_base_args

        with patch.dict(os.environ, {"YTDLP_COOKIES_FILE": "/tmp/c.txt"}):
            self.assertIn("--cookies", _ytdlp_base_args())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YTDLP_COOKIES_FILE", None)
            self.assertNotIn("--cookies", _ytdlp_base_args())


if __name__ == "__main__":
    unittest.main()
