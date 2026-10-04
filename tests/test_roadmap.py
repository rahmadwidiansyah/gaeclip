import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gaeclip.errors import ExternalCommandError, ValidationError


def _stamp(total_seconds: int) -> str:
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.000"


def _make_vtt(path: Path, cues: int = 12, step: int = 10) -> None:
    blocks = ["WEBVTT"]
    for i in range(cues):
        start, end = i * step, (i + 1) * step
        blocks.append(f"{_stamp(start)} --> {_stamp(end)}\nTeks cue {i}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


class RoadmapPipelineTests(unittest.TestCase):
    def _run_pipeline(self, fail_first_download: bool, fail_all: bool = False):
        from gaeclip import pipeline as pipe_mod
        from gaeclip.config import Settings

        snapshots: list[list] = []
        calls = {"n": 0}

        def fake_metadata(url, yt_dlp="yt-dlp"):
            return {"duration": 200}

        def fake_subtitles(url, source_dir, language="id", yt_dlp="yt-dlp"):
            Path(source_dir).mkdir(parents=True, exist_ok=True)
            subtitle_file = Path(source_dir) / "subtitles.id.vtt"
            _make_vtt(subtitle_file)
            return subtitle_file

        def fake_section(url, output_dir, start, end, name, yt_dlp="yt-dlp"):
            calls["n"] += 1
            if fail_all or (fail_first_download and calls["n"] == 1):
                raise ExternalCommandError("Rendered clip has no video stream: boom.mp4")
            return Path(output_dir) / f"{name}.mp4"

        def fake_render(source, output, start=0, duration=0, subtitles=None, **kwargs):
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_bytes(b"x" * 200_000)
            return Path(output)

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.from_env()
            object.__setattr__(settings, "storage_root", Path(directory))
            with (
                patch.object(pipe_mod, "video_metadata", fake_metadata),
                patch.object(pipe_mod, "download_subtitles", fake_subtitles),
                patch.object(pipe_mod, "download_video_section", fake_section),
                patch.object(pipe_mod, "render_static_vertical", fake_render),
            ):
                result = pipe_mod.generate_from_url(
                    "https://www.youtube.com/watch?v=test",
                    settings=settings,
                    min_duration=30,
                    max_duration=60,
                    max_clips=2,
                    on_clip_task=lambda tasks: snapshots.append([t.to_dict() for t in tasks]),
                )
        return result, snapshots

    def test_partial_success_records_failed_task(self):
        result, snapshots = self._run_pipeline(fail_first_download=True)
        self.assertEqual(len(result.clips), 1)
        self.assertIsNotNone(result.clip_tasks)
        self.assertEqual(len(result.clip_tasks), 2)
        by_status = {t.clip_id: t.status for t in result.clip_tasks}
        self.assertIn("failed", by_status.values())
        self.assertIn("done", by_status.values())
        failed = next(t for t in result.clip_tasks if t.status == "failed")
        self.assertIsNotNone(failed.error_message)
        done = next(t for t in result.clip_tasks if t.status == "done")
        self.assertIsNotNone(done.duration_seconds)
        self.assertEqual(done.size_bytes, 200_000)
        # progressive snapshots were emitted (append + done/failed each)
        self.assertGreaterEqual(len(snapshots), 2)

    def test_all_clips_failing_raises_with_first_error(self):
        with self.assertRaises(ValidationError) as ctx:
            self._run_pipeline(fail_first_download=False, fail_all=True)
        self.assertIn("Semua clip gagal", str(ctx.exception))

    def test_burn_subtitles_false_still_searches_with_transcript(self):
        from gaeclip import pipeline as pipe_mod
        from gaeclip.config import Settings
        from gaeclip.subtitles import parse_srt

        def fake_subtitles(url, source_dir, language="id", yt_dlp="yt-dlp"):
            Path(source_dir).mkdir(parents=True, exist_ok=True)
            subtitle_file = Path(source_dir) / "subtitles.id.vtt"
            _make_vtt(subtitle_file)
            return subtitle_file

        def fake_section(url, output_dir, start, end, name, yt_dlp="yt-dlp"):
            return Path(output_dir) / f"{name}.mp4"

        seen: dict = {}

        def fake_render(source, output, start=0, duration=0, subtitles=None, **kwargs):
            seen["subtitles"] = subtitles
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_bytes(b"x" * 200_000)
            return Path(output)

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.from_env()
            object.__setattr__(settings, "storage_root", Path(directory))
            with (
                patch.object(pipe_mod, "video_metadata", lambda url, yt_dlp="yt-dlp": {"duration": 200}),
                patch.object(pipe_mod, "download_subtitles", fake_subtitles),
                patch.object(pipe_mod, "download_video_section", fake_section),
                patch.object(pipe_mod, "render_static_vertical", fake_render),
            ):
                result = pipe_mod.generate_from_url(
                    "https://www.youtube.com/watch?v=test",
                    settings=settings,
                    min_duration=30,
                    max_duration=60,
                    max_clips=1,
                    burn_subtitles=False,
                )
        # Clip search still ran on the transcript...
        self.assertEqual(len(result.clips), 1)
        # ...but nothing was burned into the output.
        self.assertIsNone(seen["subtitles"])

    def test_cancelled_error_is_not_swallowed(self):
        from gaeclip import pipeline as pipe_mod
        from gaeclip.config import Settings
        from gaeclip.errors import CancelledError

        def fake_metadata(url, yt_dlp="yt-dlp"):
            return {"duration": 200}

        def fake_subtitles(url, source_dir, language="id", yt_dlp="yt-dlp"):
            Path(source_dir).mkdir(parents=True, exist_ok=True)
            subtitle_file = Path(source_dir) / "subtitles.id.vtt"
            _make_vtt(subtitle_file)
            return subtitle_file

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.from_env()
            object.__setattr__(settings, "storage_root", Path(directory))
            with (
                patch.object(pipe_mod, "video_metadata", fake_metadata),
                patch.object(pipe_mod, "download_subtitles", fake_subtitles),
            ):
                def cancelling_progress(stage, pct, *args):
                    raise CancelledError("Job dibatalkan pengguna")

                with self.assertRaises(CancelledError):
                    pipe_mod.generate_from_url(
                        "https://www.youtube.com/watch?v=test",
                        settings=settings,
                        progress=cancelling_progress,
                    )


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def get(self, key):
        value = self.store.get(key)
        return value.encode() if isinstance(value, str) else value

    def set(self, key, value, ex=None, nx=None):
        if nx and key in self.store:
            return False
        self.store[key] = value if isinstance(value, str) else value.decode()
        return True

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
        removed = 0
        for key in keys:
            if key in table:
                del table[key]
                removed += 1
        return removed

    def hgetall(self, name):
        return {
            k.encode(): v.encode() if isinstance(v, str) else v
            for k, v in self.hashes.get(name, {}).items()
        }

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

    def scan_iter(self, match="", count=100):
        import fnmatch
        return [k for k in self.store if fnmatch.fnmatch(k, match)]


class RoadmapWorkerTests(unittest.TestCase):
    def test_partial_result_sets_partial_status_and_tasks(self):
        import gaeclip.worker as worker_mod
        from gaeclip.jobs import JobRecord
        from gaeclip.pipeline import ClipTask, GeneratedClip, GenerationResult
        from gaeclip.curation import CurationResult

        redis = FakeRedis()
        repository_holder: dict = {}

        with (
            patch.object(worker_mod, "_redis", return_value=redis),
            patch.object(worker_mod.Settings, "from_env", return_value=worker_mod.Settings.from_env()),
        ):
            from gaeclip.jobs import JobRepository
            repository = JobRepository(redis)
            record = JobRecord("job_road", "https://www.youtube.com/watch?v=x", "id", max_clips=2, total_clips=2)
            repository.save(record)
            repository_holder["repo"] = repository

            tasks = [
                ClipTask(clip_id="c000001", title="Hook A", status="done", score=0.9,
                         duration_seconds=33.0, size_bytes=1000),
                ClipTask(clip_id="c000002", title="Hook B", status="failed",
                         error_code="EXTERNAL_COMMAND_FAILED", error_message="no video"),
            ]
            fake_result = GenerationResult(
                job_id="job_road", source=Path("/tmp"),
                curation=CurationResult(candidates=[], source="fallback", usage=None),
                clips=[GeneratedClip(candidate_id="c000001", path=Path("/tmp/c000001.mp4"), title="Hook A")],
                clip_tasks=tasks,
            )
            with patch.object(worker_mod, "generate_from_url", return_value=fake_result):
                worker_mod.process_job("job_road")

            final = repository.get("job_road")
            self.assertEqual(final.status, "partial")
            self.assertEqual(len(final.clip_tasks or []), 2)
            self.assertEqual(final.clips[0]["duration_seconds"], 33.0)
            self.assertEqual(final.clips[0]["size_bytes"], 1000)
            failed = next(t for t in (final.clip_tasks or []) if t["clip_id"] == "c000002")
            self.assertEqual(failed["error"]["code"], "EXTERNAL_COMMAND_FAILED")


class CurationBranchTests(unittest.TestCase):
    def _lines(self):
        from gaeclip.subtitles import parse_srt
        return parse_srt("1\n00:00:00,000 --> 00:00:40,000\nHook panjang sekali\n")

    def test_failing_curator_records_reason(self):
        from gaeclip.curation import curate_with_fallback

        class Boom:
            provider_name = "gemini"

            def curate(self, transcript):
                raise RuntimeError("quota habis bos")

        result = curate_with_fallback(self._lines(), Boom(), 10, 50, 1)
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.attempted_provider, "gemini")
        self.assertIn("quota habis", result.fallback_reason or "")

    def test_invalid_response_records_reason(self):
        from gaeclip.curation import curate_with_fallback
        from gaeclip.errors import ValidationError as AppValidationError

        class Invalid:
            provider_name = "gemini"

            def curate(self, transcript):
                raise AppValidationError("skema salah")

        result = curate_with_fallback(self._lines(), Invalid(), 10, 50, 1)
        self.assertIn("tidak valid", result.fallback_reason or "")

    def test_skipped_reason_propagates(self):
        from gaeclip.curation import curate_with_fallback
        result = curate_with_fallback(
            self._lines(), None, 10, 50, 1,
            skipped_reason="AI tidak dicoba (GEMINI_API_KEY kosong)",
        )
        self.assertIsNone(result.attempted_provider)
        self.assertIn("API_KEY", result.fallback_reason or "")


class RoadmapSerializationTests(unittest.TestCase):
    def test_job_record_carries_clip_tasks(self):
        from gaeclip.jobs import JobRecord
        record = JobRecord("job_z", "https://www.youtube.com/watch?v=x", "id")
        record.clip_tasks = [{"clip_id": "c000001", "status": "done"}]
        payload = record.to_dict()
        self.assertEqual(payload["clip_tasks"], [{"clip_id": "c000001", "status": "done"}])
        self.assertNotIn("fingerprint", payload)


if __name__ == "__main__":
    unittest.main()
