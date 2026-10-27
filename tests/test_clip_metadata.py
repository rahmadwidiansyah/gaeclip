"""Automatic per-clip publication metadata (description + hashtags).

Covers: single-request generation, validation, provider/fallback paths,
storage on the clip record, no per-clip mixups, retry semantics, and that
metadata problems never break rendering.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "tests")

from gaeclip.errors import CancelledError


def _ctx(**over):
    from gaeclip.clip_metadata import ClipMetadataContext

    base = {
        "job_id": "job-1", "clip_id": "c000001",
        "source_title": "Belajar Masak Rendang", "source_channel": "Dapur Kita",
        "clip_title": "Rahasia Rendang Empuk", "clip_reason": "Payoff kuat di akhir.",
        "clip_duration": 45.0, "language": "id",
        "span_text": ("Pertama tumis bumbu sampai harum. Lalu masukkan santan "
                      "dan aduk perlahan selama dua jam sampai rendang empuk dan lezat."),
    }
    base.update(over)
    return ClipMetadataContext(**base)


def _ok_json():
    import json

    return json.dumps({
        "title": "Rahasia Rendang Empuk dalam 2 Jam",
        "description": "Cara membuat rendang empuk dengan bumbu harum dan santan kental.",
        "hashtags": ["#Masakan", "#Rendang", "#masakan", "#Resep"],
    })


class _FakeSettings:
    gemini_api_key = ""
    gemini_model = "gemini-3.8-flash"
    gemini_timeout_seconds = 30


class ClipMetadataCoreTests(unittest.TestCase):
    def _generate(self, **kw):
        from gaeclip.clip_metadata import generate_clip_metadata

        params = {"client": None, "settings": _FakeSettings(), "ai_chain": None}
        params.update(kw)
        return generate_clip_metadata(_ctx(), **params)

    def test_single_request_yields_both_fields(self):
        from gaeclip.social import metadata as md

        calls = []

        def fake_gemini(prompt, **kw):
            calls.append(prompt)
            return _ok_json(), "pool-1", "gemini-X"

        with patch.object(md, "_generate_via_gemini", side_effect=fake_gemini):
            out = self._generate()
        self.assertEqual(len(calls), 1)
        self.assertIn("rendang empuk", out["description"])
        self.assertIn("#rendang", out["hashtags"])
        self.assertNotIn("#masakan", out["hashtags"][1:])
        self.assertEqual(out["source"], "llm")
        self.assertEqual(out["provider"], "pool-1")
        self.assertEqual(out["model"], "gemini-X")
        self.assertIsNotNone(out["updated_at"])

    def test_invalid_json_falls_back(self):
        from gaeclip.social import metadata as md

        with patch.object(md, "_generate_via_gemini",
                          return_value=("bukan json {{", None, None)):
            out = self._generate()
        self.assertEqual(out["source"], "deterministic_fallback")
        self.assertTrue(out["description"])
        self.assertTrue(out["hashtags"])
        self.assertIsNone(out["provider"])

    def test_provider_errors_fall_back(self):
        from gaeclip.social import metadata as md
        from gaeclip.social.states import SocialError

        for exc in (
            SocialError("timeout", code="timeout", user_message="x"),
            SocialError("rate", code="rate_limit", user_message="x"),
            SocialError("auth", code="auth_failed", user_message="x"),
            TimeoutError("socket timed out"),
            ValueError("boom"),
        ):
            with patch.object(md, "_generate_via_gemini", side_effect=exc):
                out = self._generate()
            self.assertEqual(out["source"], "deterministic_fallback", exc)
            self.assertTrue(out["description"])
            self.assertTrue(out["hashtags"])

    def test_no_providers_no_llm_attempt_needed(self):
        # Fallback is pure-local: valid package without network or keys.
        out = self._generate()
        self.assertEqual(out["source"], "deterministic_fallback")
        self.assertTrue(out["description"])
        self.assertGreaterEqual(len(out["hashtags"]), 1)
        self.assertLessEqual(len(out["hashtags"]), 8)
        self.assertTrue(all(h.startswith("#") for h in out["hashtags"]))
        self.assertEqual(len(set(out["hashtags"])), len(out["hashtags"]))

    def test_empty_fields_from_llm_treated_unusable(self):
        import json

        from gaeclip.social import metadata as md

        empty = json.dumps({"title": "Ok Title Here Yes", "description": "  ", "hashtags": []})
        with patch.object(md, "_generate_via_gemini", return_value=(empty, "p", "m")):
            out = self._generate()
        self.assertEqual(out["source"], "deterministic_fallback")
        self.assertTrue(out["description"])
        self.assertTrue(out["hashtags"])

    def test_cancelled_error_propagates(self):
        from gaeclip.social import metadata as md

        with patch.object(md, "_generate_via_gemini",
                          side_effect=CancelledError("stop")):
            with self.assertRaises(CancelledError):
                self._generate()

    def test_clips_do_not_mix(self):
        from gaeclip.clip_metadata import generate_clip_metadata

        a = generate_clip_metadata(
            _ctx(clip_id="c000001", span_text="rendang empuk santan kental lezat sekali"),
            client=None, settings=_FakeSettings(), ai_chain=None)
        b = generate_clip_metadata(
            _ctx(clip_id="c000002", span_text="teknik memotong bawang cepat dan aman"),
            client=None, settings=_FakeSettings(), ai_chain=None)
        self.assertNotEqual(a["description"], b["description"])

    def test_safe_wrapper_failure_keeps_empty_failed_package(self):
        from gaeclip.clip_metadata import generate_clip_metadata_safe

        with patch("gaeclip.clip_metadata.generate_clip_metadata",
                   side_effect=RuntimeError("broken fallback")):
            out = generate_clip_metadata_safe(
                _ctx(), client=None, settings=_FakeSettings(), ai_chain=None)
        self.assertEqual(out["source"], "failed")
        self.assertEqual(out["description"], "")
        self.assertEqual(out["hashtags"], [])

    def test_slice_span_text_from_objects(self):
        from gaeclip.clip_metadata import slice_span_text
        from gaeclip.schemas import TranscriptLine

        lines = [
            TranscriptLine(schema_version=1, line_id="l000001", start=0.0, end=5.0,
                           text="Halo semua selamat datang", source="youtube_auto"),
            TranscriptLine(schema_version=1, line_id="l000002", start=40.0, end=45.0,
                           text="Rahasia rendang empuk terungkap", source="youtube_auto"),
        ]
        text, lang = slice_span_text(lines, 35.0, 50.0)
        self.assertIn("rendang", text)
        self.assertNotIn("Halo", text)
        self.assertEqual(lang, "id")


class PipelineHookTests(unittest.TestCase):
    def _run(self, fail_first_download=False, break_fallback=False):
        import tempfile

        from gaeclip import pipeline as pipe_mod
        from gaeclip.config import Settings

        from test_roadmap import _make_vtt

        calls = []
        downloaded = {"n": 0}

        def fake_metadata(url, yt_dlp="yt-dlp"):
            return {"duration": 200, "title": "Video Asli",
                    "channel": "Channel Asli"}

        def fake_subtitles(url, source_dir, language="id", yt_dlp="yt-dlp"):
            Path(source_dir).mkdir(parents=True, exist_ok=True)
            subtitle_file = Path(source_dir) / "subtitles.id.vtt"
            _make_vtt(subtitle_file)
            return subtitle_file

        def fake_section(url, output_dir, start, end, name, yt_dlp="yt-dlp"):
            from gaeclip.errors import ExternalCommandError

            downloaded["n"] += 1
            if fail_first_download and downloaded["n"] == 1:
                raise ExternalCommandError("boom.mp4")
            return Path(output_dir) / f"{name}.mp4"

        def fake_render(source, output, start=0, duration=0, subtitles=None, **kwargs):
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_bytes(b"x" * 200_000)
            return Path(output)

        def fake_meta(**kwargs):
            calls.append(kwargs["candidate"].candidate_id)
            if break_fallback:
                raise RuntimeError("metadata exploded")
            return {"description": f"Deskripsi {kwargs['candidate'].candidate_id}",
                    "hashtags": ["#shorts"], "source": "llm",
                    "provider": "p", "model": "m", "updated_at": 1.0}

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.from_env()
            object.__setattr__(settings, "storage_root", Path(directory))
            with (
                patch.object(pipe_mod, "video_metadata", fake_metadata),
                patch.object(pipe_mod, "download_subtitles", fake_subtitles),
                patch.object(pipe_mod, "download_video_section", fake_section),
                patch.object(pipe_mod, "render_static_vertical", fake_render),
                patch("gaeclip.pipeline._clip_metadata_best_effort",
                      side_effect=fake_meta),
            ):
                result = pipe_mod.generate_from_url(
                    "https://www.youtube.com/watch?v=test",
                    settings=settings, min_duration=30, max_duration=60,
                    max_clips=2,
                )
        return result, calls

    def test_hook_runs_once_per_completed_clip(self):
        result, calls = self._run(fail_first_download=True)
        done = [c for c in result.clip_tasks if c.status == "done"]
        self.assertEqual(len(done), 1)
        # Failed render clip gets no metadata call.
        self.assertEqual(calls, [done[0].clip_id])
        stored = {c.candidate_id: c for c in result.clips}
        self.assertEqual(set(stored), {done[0].clip_id})
        clip = stored[done[0].clip_id]
        self.assertTrue(clip.description.startswith("Deskripsi"))
        self.assertEqual(list(clip.hashtags), ["#shorts"])
        self.assertEqual(clip.metadata_source, "llm")
        self.assertEqual(clip.metadata_updated_at, 1.0)

    def test_metadata_blowup_does_not_fail_clip(self):
        # Even if the safe wrapper itself blows up, the pipeline helper
        # degrades to source="failed" and every rendered clip stays done.
        import tempfile

        from gaeclip import pipeline as pipe_mod
        from gaeclip.config import Settings

        from test_roadmap import _make_vtt

        def fake_metadata(url, yt_dlp="yt-dlp"):
            return {"duration": 200, "title": "Video Asli",
                    "channel": "Channel Asli"}

        def fake_subtitles(url, source_dir, language="id", yt_dlp="yt-dlp"):
            Path(source_dir).mkdir(parents=True, exist_ok=True)
            subtitle_file = Path(source_dir) / "subtitles.id.vtt"
            _make_vtt(subtitle_file)
            return subtitle_file

        def fake_section(url, output_dir, start, end, name, yt_dlp="yt-dlp"):
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
                patch("gaeclip.clip_metadata.generate_clip_metadata_safe",
                      side_effect=RuntimeError("exploded")),
            ):
                result = pipe_mod.generate_from_url(
                    "https://www.youtube.com/watch?v=test",
                    settings=settings, min_duration=30, max_duration=60,
                    max_clips=2,
                )
        done = [c for c in result.clip_tasks if c.status == "done"]
        self.assertGreaterEqual(len(done), 1)
        stored = {c.candidate_id: c for c in result.clips}
        for clip_id in [c.clip_id for c in done]:
            self.assertEqual(stored[clip_id].metadata_source, "failed")
            self.assertEqual(stored[clip_id].description, "")

    def test_failed_render_clip_carries_no_metadata_call(self):
        result, calls = self._run(fail_first_download=True)
        failed = [c for c in result.clip_tasks if c.status == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertNotIn(failed[0].clip_id, calls)


class WorkerEntryTests(unittest.TestCase):
    def test_clip_entry_carries_metadata(self):
        import types

        from gaeclip.worker import _clip_entry

        clip = types.SimpleNamespace(
            candidate_id="c000001", path="/tmp/x.mp4", title="T",
            score=0.9, reason="R", start=10.0, end=55.0,
            description="Desc", hashtags=("#a", "#b"),
            metadata_source="llm", metadata_updated_at=123.0)
        task = types.SimpleNamespace(duration_seconds=45.0, size_bytes=10)
        entry = _clip_entry(clip, {"c000001": task})
        self.assertEqual(entry["description"], "Desc")
        self.assertEqual(entry["hashtags"], ["#a", "#b"])
        self.assertEqual(entry["metadata_source"], "llm")
        self.assertEqual(entry["metadata_updated_at"], 123.0)

    def test_clip_entry_legacy_clip_without_metadata(self):
        import types

        from gaeclip.worker import _clip_entry

        clip = types.SimpleNamespace(
            candidate_id="c000001", path="/tmp/x.mp4", title="T",
            score=0.9, reason="R", start=10.0, end=55.0)
        entry = _clip_entry(clip, {})
        self.assertEqual(entry["description"], "")
        self.assertEqual(entry["hashtags"], [])
        self.assertEqual(entry["metadata_source"], "")
        self.assertIsNone(entry["metadata_updated_at"])
        self.assertIsNone(entry["duration_seconds"])


if __name__ == "__main__":
    unittest.main()
