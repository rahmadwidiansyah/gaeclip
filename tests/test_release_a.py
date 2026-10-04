import os
import tempfile
import unittest
from pathlib import Path

from gaeclip.config import Settings
from gaeclip.curation import parse_provider_response, curate_with_fallback
from gaeclip.subtitles import parse_srt, parse_vtt
from gaeclip.url import validate_youtube_url
from gaeclip.errors import ValidationError


class ReleaseATests(unittest.TestCase):
    def test_srt_normalization_assigns_stable_ids(self):
        lines = parse_srt(
            "1\n00:00:01,000 --> 00:00:03,000\nHalo\n\n"
            "2\n00:00:03,100 --> 00:00:05,000\n dunia\n"
        )
        self.assertEqual([line.line_id for line in lines], ["l000001"])
        self.assertEqual(lines[0].text, "Halo dunia")

    def test_vtt_normalization_supports_short_timestamps(self):
        lines = parse_vtt("WEBVTT\n\n00:01.000 --> 00:03.000\nHalo\n")
        self.assertEqual(lines[0].start, 1)
        self.assertEqual(lines[0].text, "Halo")

    def test_vtt_strips_youtube_word_timing_tags(self):
        lines = parse_vtt(
            "WEBVTT\n\n00:01.000 --> 00:03.000\n"
            "Halo<00:01.500><c> dunia</c>\n"
        )
        self.assertEqual(lines[0].text, "Halo dunia")

    def test_write_srt_shifts_timestamps(self):
        from gaeclip.subtitles import write_srt
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.srt"
            write_srt(parse_srt("1\n00:00:10,000 --> 00:00:12,000\nHalo\n"), path, offset=10)
            self.assertIn("00:00:00,000 --> 00:00:02,000", path.read_text())

    def test_write_srt_wraps_long_lines_for_vertical_video(self):
        from gaeclip.subtitles import write_srt
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.srt"
            lines = parse_srt(
                "1\n00:00:00,000 --> 00:00:04,000\n"
                "Ini adalah subtitle yang cukup panjang agar terbagi menjadi beberapa baris\n"
            )
            write_srt(lines, path)
            text = path.read_text()
            # Long cue is split into timed cues, each wrapped to at most 2 lines.
            cues = [block for block in text.strip().split("\n\n") if block.strip()]
            self.assertGreaterEqual(len(cues), 2)
            for cue in cues:
                cue_lines = cue.splitlines()[2:]
                self.assertLessEqual(len(cue_lines), 2)
                for cue_line in cue_lines:
                    self.assertLessEqual(len(cue_line), 34)
            # No text is lost across the split.
            joined = " ".join(text.split())
            for word in "Ini adalah subtitle yang cukup panjang agar terbagi menjadi beberapa baris".split():
                self.assertIn(word, joined)

    def test_url_allowlist(self):
        self.assertEqual(
            validate_youtube_url("https://www.youtube.com/watch?v=test"),
            "https://www.youtube.com/watch?v=test",
        )
        with self.assertRaises(ValidationError):
            validate_youtube_url("http://localhost/video")

    def test_settings_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            old = os.environ.get("STORAGE_ROOT")
            os.environ["STORAGE_ROOT"] = directory
            try:
                settings = Settings.from_env()
                self.assertEqual(settings.max_clips, 3)
            finally:
                if old is None:
                    os.environ.pop("STORAGE_ROOT", None)
                else:
                    os.environ["STORAGE_ROOT"] = old

    def test_gemini_timeout_defaults_to_spec(self):
        settings = Settings.from_env()
        self.assertEqual(settings.gemini_timeout_seconds, 300)

    def test_heatmap_fallback_prefers_highest_audience_window(self):
        from gaeclip.curation import deterministic_candidates
        lines = parse_srt(
            "1\n00:00:00,000 --> 00:00:10,000\nPembuka\n\n"
            "2\n00:00:10,000 --> 00:00:20,000\nBagian ramai\n\n"
            "3\n00:00:20,000 --> 00:00:30,000\nPenutup\n"
        )
        candidates = deterministic_candidates(
            lines, min_duration=10, max_duration=20, max_clips=1,
            heatmap=[{"start_time": 10, "end_time": 20, "value": 1.0}],
        )
        self.assertEqual(candidates[0].start, 10)
        self.assertEqual(candidates[0].reason, "YouTube audience heatmap fallback")

    def test_provider_response_is_strictly_validated(self):
        lines = parse_srt(
            "1\n00:00:00,000 --> 00:00:08,000\nSatu\n\n"
            "2\n00:00:08,100 --> 00:00:16,000\nDua\n"
        )
        candidates = parse_provider_response(
            '{"candidates":[{"candidate_id":"c000001","start_line_id":"l000001",'
            '"end_line_id":"l000002","start":0,"end":16,"title":"Hook",'
            '"reason":"Context","score":0.8}]}',
            lines,
            min_duration=10,
            max_duration=20,
            max_clips=3,
        )
        self.assertEqual(candidates[0].candidate_id, "c000001")
        with self.assertRaises(ValidationError):
            parse_provider_response('{"candidates":[{"unknown":true}]}', lines, 10, 20, 3)

    def test_empty_provider_result_uses_deterministic_fallback(self):
        lines = parse_srt("1\n00:00:00,000 --> 00:00:16,000\nHook panjang\n")
        result = curate_with_fallback(lines, None, 10, 20, 3)
        self.assertEqual(result.source, "fallback")
        self.assertEqual(len(result.candidates), 1)


if __name__ == "__main__":
    unittest.main()
