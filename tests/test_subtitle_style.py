import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gaeclip.subtitle_style import (
    DEFAULT_SUBTITLE_POSITION,
    DEFAULT_SUBTITLE_STYLE,
    SUBTITLE_POSITIONS,
    SUBTITLE_STYLES,
    approximate_word_timing,
    ass_style_row,
    force_style_string,
    karaoke_events,
    resolve_subtitle_config,
)
from gaeclip.subtitles import parse_srt, write_ass


def sample_lines():
    return parse_srt(
        "1\n00:00:01,000 --> 00:00:05,000\nHalo semuanya hari ini kita akan belajar Docker\n"
    )


class ConfigTests(unittest.TestCase):
    def test_known_styles_and_positions(self):
        self.assertEqual(set(SUBTITLE_STYLES),
                         {"bold_pop", "dynamic_highlight", "casual_center", "elegant_serif"})
        self.assertEqual(set(SUBTITLE_POSITIONS), {"center", "bottom_center", "bottom"})
        self.assertEqual(DEFAULT_SUBTITLE_STYLE, "casual_center")
        self.assertEqual(DEFAULT_SUBTITLE_POSITION, "bottom_center")

    def test_invalid_names_rejected(self):
        with self.assertRaises(ValueError):
            resolve_subtitle_config("abc", "center")
        with self.assertRaises(ValueError):
            resolve_subtitle_config("bold_pop", "top")

    def test_1_bold_pop_center(self):
        cfg = resolve_subtitle_config("bold_pop", "center", 1080, 1920)
        self.assertTrue(cfg.uppercase)
        self.assertEqual(cfg.primary, "&H0000FFFF")
        self.assertGreaterEqual(cfg.outline, 3)
        self.assertEqual(cfg.alignment, 5)
        self.assertIn("FontName=DejaVu Sans", force_style_string(cfg))
        self.assertIn("Bold=1", force_style_string(cfg))
        row = ass_style_row(cfg)
        self.assertIn("DejaVuSans,42", row.replace(" ", ""))

    def test_3_casual_center_bottom(self):
        cfg = resolve_subtitle_config("casual_center", "bottom", 1080, 1920)
        self.assertFalse(cfg.uppercase)
        self.assertEqual(cfg.primary, "&H00FFFFFF")
        self.assertEqual(cfg.alignment, 2)
        # bottom margin smaller than bottom_center but still safe (>=4% height)
        mid = resolve_subtitle_config("casual_center", "bottom_center", 1080, 1920)
        self.assertLess(cfg.margin_v, mid.margin_v)
        self.assertGreaterEqual(cfg.margin_v, round(1920 * 0.04))

    def test_4_elegant_serif_center(self):
        cfg = resolve_subtitle_config("elegant_serif", "center", 1080, 1920)
        self.assertIn("Serif", cfg.font_name)
        self.assertFalse(cfg.bold)
        self.assertEqual(cfg.primary, "&H00FFFFFF")
        self.assertLessEqual(cfg.outline, 1)
        self.assertEqual(cfg.alignment, 5)

    def test_positions_responsive_not_hardcoded(self):
        small = resolve_subtitle_config("casual_center", "bottom", 640, 640)
        big = resolve_subtitle_config("casual_center", "bottom", 1080, 1920)
        self.assertLess(small.margin_v, big.margin_v)
        self.assertEqual(small.alignment, big.alignment)

    def test_ass_and_force_style_agree(self):
        for style in SUBTITLE_STYLES:
            cfg = resolve_subtitle_config(style, "bottom_center", 1080, 1920)
            row = ass_style_row(cfg).replace(" ", "")
            filt = force_style_string(cfg)
            self.assertIn(f"FontSize={cfg.font_size}", filt)
            self.assertIn(f"FontName={cfg.font_name}", filt)
            self.assertIn(f"Alignment={cfg.alignment}", filt)
            self.assertIn(f"MarginV={cfg.margin_v}", filt)
            self.assertIn(f"{cfg.font_name},{cfg.font_size}".replace(" ", ""), row)


class KaraokeTests(unittest.TestCase):
    def test_approximate_timing_covers_span(self):
        timed = approximate_word_timing(1.0, 3.0, ["a", "bb", "ccc"])
        self.assertEqual(timed[0][1], 1.0)
        self.assertEqual(timed[-1][2], 3.0)
        for (_, word_start, _), (_, _, prev_end) in zip(timed[1:], timed):
            self.assertAlmostEqual(word_start, prev_end)

    def test_2_dynamic_highlight_events(self):
        events = karaoke_events(0.0, 4.0, "Halo semuanya hari ini", 34)
        self.assertEqual(len(events), 4)
        starts = [start for start, _, _ in events]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(events[0][0], 0.0)
        self.assertEqual(events[-1][1], 4.0)
        # Exactly one green word per event, stepping through.
        for index, (_, _, body) in enumerate(events):
            tags = re.findall(r"\{\\c&H0000FF00&\}", body)
            self.assertEqual(len(tags), 1, body)
        # Note: karaoke_events itself does not uppercase; write_ass applies
        # the style transform first. Here input is already plain words.
        self.assertIn("{\\c&H0000FF00&}Halo{\\c}", events[0][2])
        self.assertIn("{\\c&H0000FF00&}ini{\\c}", events[-1][2])

    def test_highlight_layout_stable_across_events(self):
        events = karaoke_events(0.0, 4.0, "Halo semuanya hari ini", 10)
        stripped = [" ".join(re.sub(r"\{\\c.*?\}", "", e[2]).split()) for e in events]
        self.assertTrue(all(text == stripped[0] for text in stripped))


class AssFileTests(unittest.TestCase):
    def test_default_output_matches_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.ass"
            write_ass(sample_lines(), path, offset=0, clip_duration=6,
                      max_chars=41, width=1080, height=1920)
            text = path.read_text()
            self.assertIn("PlayResX: 1080", text)
            self.assertIn("PlayResY: 1920", text)
            self.assertIn("DejaVu Sans,42", text)
            self.assertIn(",2,", text)  # bottom-center alignment in style row

    def test_dynamic_highlight_ass_has_karaoke(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.ass"
            write_ass(sample_lines(), path, offset=0, clip_duration=6,
                      max_chars=41, width=1080, height=1920,
                      style="dynamic_highlight", position="bottom_center")
            text = path.read_text()
            dialogues = [line for line in text.splitlines() if line.startswith("Dialogue:")]
            # 8 words -> 8 karaoke events stepping through the line.
            # (dynamic_highlight uppercases the transcript first.)
            self.assertEqual(len(dialogues), 8)
            self.assertIn(r"{\c&H0000FF00&}HALO{\c}", dialogues[0])

    def test_bold_pop_uppercases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.ass"
            write_ass(sample_lines(), path, offset=0, clip_duration=6,
                      max_chars=41, width=1080, height=1920,
                      style="bold_pop", position="center")
            text = path.read_text()
            self.assertIn("HALO SEMUANYA", text)
            self.assertIn("&H0000FFFF", text)
            self.assertIn(",5,", text)  # centered alignment in style row

    def test_elegant_uses_serif_and_landscape_playres(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.ass"
            write_ass(sample_lines(), path, offset=0, clip_duration=6,
                      max_chars=60, width=1920, height=1080,
                      style="elegant_serif", position="center")
            text = path.read_text()
            self.assertIn("PlayResX: 1920", text)
            self.assertIn("PlayResY: 1080", text)
            self.assertIn("DejaVu Serif", text)
            # Transcript casing preserved (no forced uppercase).
            self.assertIn("Halo semuanya", text)


class ApiValidationTests(unittest.TestCase):
    def test_5_invalid_style_rejected(self):
        from fastapi.testclient import TestClient
        import gaeclip.api as api_mod

        fake = MagicMock()
        redis_mock = MagicMock()
        redis_mock.from_url.return_value = fake
        with patch("redis.Redis", redis_mock):
            app = api_mod.create_app()
        client = TestClient(app)
        res = client.post("/api/v1/jobs", json={
            "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "subtitle_style": "abc",
        })
        self.assertEqual(res.status_code, 422)

    def test_6_invalid_position_rejected(self):
        from fastapi.testclient import TestClient
        import gaeclip.api as api_mod

        fake = MagicMock()
        redis_mock = MagicMock()
        redis_mock.from_url.return_value = fake
        with patch("redis.Redis", redis_mock):
            app = api_mod.create_app()
        client = TestClient(app)
        res = client.post("/api/v1/jobs", json={
            "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "subtitle_position": "top",
        })
        self.assertEqual(res.status_code, 422)

    def test_7_defaults_preserve_existing_behavior(self):
        from fastapi.testclient import TestClient
        import gaeclip.api as api_mod

        fake = MagicMock()
        redis_mock = MagicMock()
        redis_mock.from_url.return_value = fake
        with patch("redis.Redis", redis_mock):
            app = api_mod.create_app()
        client = TestClient(app)
        # Defaults must be accepted; stored record keeps legacy values.
        from gaeclip.api import CreateJobRequest
        payload = CreateJobRequest(source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(payload.subtitle_style, "casual_center")
        self.assertEqual(payload.subtitle_position, "bottom_center")
        _ = client  # client boots fine with new schema


class PipelinePassthroughTests(unittest.TestCase):
    def test_style_reaches_renderer(self):
        import tempfile

        from gaeclip import pipeline as pipe_mod
        from gaeclip.config import Settings

        seen: dict = {}

        def fake_subtitles(url, source_dir, language="id", yt_dlp="yt-dlp"):
            from gaeclip.subtitles import parse_srt
            lines = parse_srt(
                "1\n00:00:00,000 --> 00:00:40,000\nHalo semuanya hari ini\n"
            )
            pipe_mod.__dict__["_lines"] = lines
            return None

        def fake_audio(url, source_dir, yt_dlp="yt-dlp"):
            return Path(source_dir) / "audio.m4a"

        def fake_transcribe(audio, model_name="small", language="id", **kwargs):
            return pipe_mod.__dict__["_lines"]

        def fake_section(url, output_dir, start, end, name, yt_dlp="yt-dlp"):
            return Path(output_dir) / f"{name}.mp4"

        def fake_write(lines, path, **kwargs):
            seen["write_kwargs"] = kwargs
            Path(path).write_text("dummy")

        def fake_render(source, output, **kwargs):
            seen["render_kwargs"] = kwargs
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_bytes(b"x" * 200_000)
            return Path(output)

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.from_env()
            object.__setattr__(settings, "storage_root", Path(directory))
            with (
                patch.object(pipe_mod, "video_metadata", lambda url, yt_dlp="yt-dlp": {"duration": 200}),
                patch.object(pipe_mod, "download_subtitles", fake_subtitles),
                patch.object(pipe_mod, "download_audio", fake_audio),
                patch.object(pipe_mod, "transcribe_with_whisper", fake_transcribe),
                patch.object(pipe_mod, "download_video_section", fake_section),
                patch.object(pipe_mod, "write_ass", fake_write),
                patch.object(pipe_mod, "render_static_vertical", fake_render),
            ):
                result = pipe_mod.generate_from_url(
                    "https://www.youtube.com/watch?v=test",
                    settings=settings,
                    min_duration=30,
                    max_duration=60,
                    max_clips=1,
                    subtitle_style="bold_pop",
                    subtitle_position="center",
                )
        self.assertEqual(len(result.clips), 1)
        # write_ass is imported into pipeline namespace; patch there.
        self.assertEqual(seen["write_kwargs"].get("style"), "bold_pop")
        self.assertEqual(seen["write_kwargs"].get("position"), "center")
        # Renderer needs no style args: the ASS file (written at actual
        # output dims) is the single source of truth.
        self.assertNotIn("subtitle_style", seen["render_kwargs"])
        self.assertNotIn("subtitle_position", seen["render_kwargs"])


if __name__ == "__main__":
    unittest.main()
