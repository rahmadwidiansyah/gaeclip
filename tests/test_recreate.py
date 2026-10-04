"""Recreate job + transcript-cache pipeline integration (no network)."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, "tests")

from test_video_metadata import FakeRedis, make_client, seed_job

from gaeclip.schemas import TranscriptLine


def _whisper_lines(n=12, step=10):
    return [
        TranscriptLine(
            line_id=f"l{i:06d}", start=float(i * step), end=float(i * step + 9),
            text=f"kalimat penting {i}", language="id", source="faster_whisper",
        )
        for i in range(1, n + 1)
    ]


def _vtt(path: Path, cues=12, step=10):
    def stamp(total):
        h, r = divmod(total, 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}.000"
    blocks = ["WEBVTT"]
    for i in range(cues):
        blocks.append(f"{stamp(i * step)} --> {stamp((i + 1) * step)}\nTeks cue {i}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def _run_pipeline(settings=None, storage_dir=None, **overrides):
    """Run generate_from_url with everything stubbed except the unit under test."""
    from gaeclip import pipeline as pipe_mod
    from gaeclip.config import Settings

    directory = storage_dir or tempfile.mkdtemp()
    settings = settings or Settings.from_env()
    object.__setattr__(settings, "storage_root", Path(directory))
    state = {"yt_calls": 0, "wh_calls": 0, "audio_calls": 0}
    progress_log = []

    def fake_meta(url, yt_dlp="yt-dlp"):
        return {"duration": 200}

    def fake_yt(url, source_dir, language="id", yt_dlp="yt-dlp"):
        state["yt_calls"] += 1
        if overrides.get("yt_subs", True):
            Path(source_dir).mkdir(parents=True, exist_ok=True)
            path = Path(source_dir) / "subtitles.id.vtt"
            _vtt(path)
            return path
        return None

    def fake_audio(url, source_dir, yt_dlp="yt-dlp"):
        state["audio_calls"] += 1
        Path(source_dir).mkdir(parents=True, exist_ok=True)
        path = Path(source_dir) / "audio.webm"
        path.write_bytes(b"x")
        return path

    def fake_wh(source, model_name="small", language="id", should_cancel=None,
                on_progress=None, total_duration=None, **kwargs):
        state["wh_calls"] += 1
        return list(_whisper_lines())

    def fake_section(url, output_dir, start, end, name, yt_dlp="yt-dlp"):
        return Path(output_dir) / f"{name}.mp4"

    def fake_render(source, output, start=0, duration=0, subtitles=None, **kwargs):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"x" * 200_000)
        return Path(output)

    def fake_progress(stage, pct, detail=None, current=0, total=0):
        progress_log.append((stage, pct, detail, current, total))

    kwargs = dict(
        min_duration=30, max_duration=60, max_clips=1,
        progress=fake_progress,
        redis_client=overrides.get("redis_client"),
        adopt_transcript_job_id=overrides.get("adopt_transcript_job_id"),
        force_retranscribe=overrides.get("force_retranscribe", False),
        job_id=overrides.get("job_id"),
    )
    if overrides.get("redis_client") is not None and overrides.get("job_id"):
        import time as _time

        from gaeclip.jobs import JobRecord, JobRepository

        now = _time.time()
        JobRepository(overrides["redis_client"]).save(JobRecord(
            overrides["job_id"], "https://www.youtube.com/watch?v=ABC123", "id",
            status="running", stage="transcribing", progress=35,
            created_at=now, updated_at=now))
    with (
        patch.object(pipe_mod, "video_metadata", fake_meta),
        patch.object(pipe_mod, "download_subtitles", fake_yt),
        patch.object(pipe_mod, "download_audio", fake_audio),
        patch.object(pipe_mod, "transcribe_with_whisper", fake_wh),
        patch.object(pipe_mod, "download_video_section", fake_section),
        patch.object(pipe_mod, "render_static_vertical", fake_render),
    ):
        result = pipe_mod.generate_from_url(
            "https://www.youtube.com/watch?v=ABC123", settings=settings, **kwargs)
    return result, state, progress_log


class PipelineCacheTests(unittest.TestCase):
    def _settings(self, storage_dir, transcript_source="youtube_first"):
        from gaeclip.config import Settings

        settings = Settings.from_env()
        object.__setattr__(settings, "storage_root", Path(storage_dir))
        object.__setattr__(settings, "transcript_source", transcript_source)
        return settings

    def test_first_whisper_stores_cache_second_hits(self):
        with tempfile.TemporaryDirectory() as directory:
            redis = FakeRedis()
            settings = self._settings(directory, "whisper_only")
            first, state1, _ = _run_pipeline(
                settings, directory, redis_client=redis, job_id="job_first0001")
            self.assertEqual(state1["wh_calls"], 1)
            self.assertFalse(first.transcript_reused)
            self.assertIsNotNone(first.transcript_cache_id)
            self.assertTrue((Path(directory) / "transcripts").is_dir())

            settings2 = self._settings(directory, "whisper_only")
            second, state2, _ = _run_pipeline(
                settings2, directory, redis_client=redis, job_id="job_secon0002")
            self.assertEqual(state2["wh_calls"], 0, "cache hit must skip Whisper")
            self.assertEqual(state2["audio_calls"], 0, "cache hit must skip audio download")
            self.assertTrue(second.transcript_reused)
            self.assertEqual(second.transcript_cache_id, first.transcript_cache_id)

    def test_force_retranscribe_bypasses_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            redis = FakeRedis()
            first, _, _ = _run_pipeline(
                self._settings(directory, "whisper_only"), directory,
                redis_client=redis, job_id="job_first0001")
            second, state2, _ = _run_pipeline(
                self._settings(directory, "whisper_only"), directory,
                redis_client=redis, job_id="job_secon0002", force_retranscribe=True)
            self.assertEqual(state2["wh_calls"], 1)
            self.assertFalse(second.transcript_reused)
            self.assertNotEqual(second.transcript_cache_id, first.transcript_cache_id)

    def test_youtube_only_ignores_whisper_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            redis = FakeRedis()
            _run_pipeline(self._settings(directory, "whisper_only"), directory,
                          redis_client=redis, job_id="job_first0001")
            _, state2, _ = _run_pipeline(
                self._settings(directory, "youtube_only"), directory,
                redis_client=redis, job_id="job_secon0002")
            self.assertEqual(state2["wh_calls"], 0)
            self.assertEqual(state2["yt_calls"], 1)

    def test_corrupt_cache_falls_back_to_whisper(self):
        with tempfile.TemporaryDirectory() as directory:
            redis = FakeRedis()
            first, _, _ = _run_pipeline(
                self._settings(directory, "whisper_only"), directory,
                redis_client=redis, job_id="job_first0001")
            (Path(directory) / "transcripts" / f"{first.transcript_cache_id}.json").write_text(
                "corrupt", encoding="utf-8")
            _, state2, _ = _run_pipeline(
                self._settings(directory, "whisper_only"), directory,
                redis_client=redis, job_id="job_secon0002")
            self.assertEqual(state2["wh_calls"], 1, "invalid cache must fall back to Whisper")

    def test_redis_failure_does_not_crash_pipeline(self):
        class BrokenRedis(FakeRedis):
            def get(self, key):
                raise ConnectionError("redis down")

        with tempfile.TemporaryDirectory() as directory:
            result, state, _ = _run_pipeline(
                self._settings(directory, "whisper_only"), directory,
                redis_client=BrokenRedis(), job_id="job_broken000")
            self.assertEqual(state["wh_calls"], 1)
            self.assertTrue(result.clips)

    def test_job_transcript_json_written(self):
        with tempfile.TemporaryDirectory() as directory:
            redis = FakeRedis()
            result, _, _ = _run_pipeline(
                self._settings(directory, "whisper_only"), directory,
                redis_client=redis, job_id="job_first0001")
            payload = json.loads(
                (Path(directory) / "jobs" / result.job_id / "source" / "transcript.json")
                .read_text(encoding="utf-8"))
            self.assertEqual(payload["engine"], "faster_whisper")
            self.assertEqual(len(payload["lines"]), 12)
            self.assertTrue(all(line["end"] > line["start"] for line in payload["lines"]))

    def test_adopt_historical_parent_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            redis = FakeRedis()
            # Historical job: transcript.json exists but no shared cache entry.
            parent_dir = Path(directory) / "jobs" / "job_parent01" / "source"
            parent_dir.mkdir(parents=True, exist_ok=True)
            (parent_dir / "transcript.json").write_text(json.dumps({
                "schema_version": 1, "engine": "faster_whisper",
                "whisper_model": "small", "language": "id",
                "lines": [line.model_dump(mode="json") for line in _whisper_lines()],
            }), encoding="utf-8")
            result, state, _ = _run_pipeline(
                self._settings(directory, "whisper_only"), directory,
                redis_client=redis, job_id="job_child0001",
                adopt_transcript_job_id="job_parent01")
            self.assertEqual(state["wh_calls"], 0, "adopted transcript must skip Whisper")
            self.assertTrue(result.transcript_reused)
            self.assertIsNotNone(result.transcript_cache_id)

    def test_transcribe_progress_keeps_clip_counters_clean(self):
        # Regression: audio seconds must not pollute current/total clip counters.
        with tempfile.TemporaryDirectory() as directory:
            _, _, log = _run_pipeline(
                self._settings(directory, "whisper_only"), directory,
                redis_client=FakeRedis(), job_id="job_first0001", yt_subs=False)
            transcribing = [entry for entry in log if entry[0] == "transcribing"]
            self.assertTrue(transcribing)
            for _, _, _, current, total in transcribing:
                self.assertEqual((current, total), (0, 0))


class RecreateApiTests(unittest.TestCase):
    def setUp(self):
        self.client, self.fake = make_client()

    def _make_parent(self, status="failed"):
        seed_job(self.fake, "job_parent01", "https://youtu.be/ABC123",
                 video_id="ABC123", status=status)
        return "job_parent01"

    def _recreate(self, job_id, body=None, key="recreate-key-1"):
        return self.client.post(
            f"/api/v1/jobs/{job_id}/recreate",
            json=body or {},
            headers={"Idempotency-Key": key},
        )

    def test_recreate_creates_new_job_parent_untouched(self):
        from gaeclip.jobs import JobRepository

        parent_id = self._make_parent()
        with patch("gaeclip.worker.celery_task") as celery_task:
            fake_result = MagicMock()
            fake_result.id = "celery-uuid-recreate-1"
            fake_task = MagicMock()
            fake_task.delay.return_value = fake_result
            celery_task.return_value = (MagicMock(), fake_task)
            res = self._recreate(parent_id)
        self.assertEqual(res.status_code, 202, res.text)
        child = res.json()
        self.assertNotEqual(child["job_id"], parent_id)
        self.assertEqual(child["recreated_from_job_id"], parent_id)
        self.assertEqual(child["source_url"], "https://youtu.be/ABC123")
        self.assertEqual(child["status"], "queued")
        parent = JobRepository(self.fake).get(parent_id)
        self.assertEqual(parent.status, "failed")  # unchanged
        self.assertIsNone(parent.recreated_from_job_id)

    def test_recreate_copies_parent_params(self):
        from gaeclip.jobs import JobRecord, JobRepository

        repo = JobRepository(self.fake)
        now = time.time()
        repo.save(JobRecord("job_p9", "https://youtu.be/ABC123", "id", status="completed",
                            stage="completed", progress=100, created_at=now, updated_at=now,
                            max_clips=5, min_duration=10, max_duration=45,
                            burn_subtitles=False, video_id="ABC123"))
        with patch("gaeclip.worker.celery_task") as celery_task:
            fake_task = MagicMock()
            fake_task.delay.return_value = MagicMock(id="celery-x")
            celery_task.return_value = (MagicMock(), fake_task)
            res = self._recreate("job_p9", key="copy-params-1")
        child = res.json()
        self.assertEqual(child["max_clips"], 5)
        self.assertEqual(child["min_duration"], 10)
        self.assertEqual(child["max_duration"], 45)
        self.assertFalse(child["burn_subtitles"])

    def test_duplicate_recreate_returns_active_child(self):
        with patch("gaeclip.worker.celery_task") as celery_task:
            fake_task = MagicMock()
            fake_task.delay.return_value = MagicMock(id="celery-dup")
            celery_task.return_value = (MagicMock(), fake_task)
            parent_id = self._make_parent()
            first = self._recreate(parent_id, key="dup-a").json()
            second = self._recreate(parent_id, key="dup-b").json()
        self.assertEqual(first["job_id"], second["job_id"])

    def test_force_retranscribe_stored(self):
        with patch("gaeclip.worker.celery_task") as celery_task:
            fake_task = MagicMock()
            fake_task.delay.return_value = MagicMock(id="celery-f")
            celery_task.return_value = (MagicMock(), fake_task)
            parent_id = self._make_parent()
            res = self._recreate(parent_id, body={"force_retranscribe": True}, key="force-1")
        self.assertTrue(res.json()["force_retranscribe"])

    def test_recreate_missing_parent_404(self):
        res = self._recreate("job_nope123", key="missing-1")
        self.assertEqual(res.status_code, 404)

    def test_preview_reports_availability(self):
        self._make_parent()
        res = self.client.get("/api/v1/jobs/job_parent01/recreate-preview")
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["job_id"], "job_parent01")
        self.assertIn("transcript_available", body)
        self.assertIn("cache_hit", body)

    def test_stats_endpoint(self):
        res = self.client.get("/api/v1/transcripts/stats")
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertIn("cached_transcripts", body)
        self.assertIn("storage_bytes", body)


class RecreateWorkerHandoffTests(unittest.TestCase):
    def test_force_flag_consumed_once(self):
        # Worker clears force_retranscribe before running so retries never inherit it.
        import gaeclip.worker as worker_mod
        from gaeclip.jobs import JobRecord, JobRepository

        fake = FakeRedis()
        repo = JobRepository(fake)
        now = time.time()
        repo.save(JobRecord("job_w1", "https://youtu.be/ABC123", "id", status="running",
                            stage="transcribing", progress=35, created_at=now, updated_at=now,
                            force_retranscribe=True, recreated_from_job_id="job_parent01"))
        seen = {}

        def fake_generate(url, settings=None, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here")

        with patch.object(worker_mod, "generate_from_url", side_effect=fake_generate):
            with patch.object(worker_mod, "_redis", return_value=fake):
                worker_mod.process_job("job_w1")
        self.assertTrue(seen["force_retranscribe"])
        self.assertEqual(seen["adopt_transcript_job_id"], "job_parent01")
        self.assertFalse(repo.get("job_w1").force_retranscribe)


if __name__ == "__main__":
    unittest.main()
