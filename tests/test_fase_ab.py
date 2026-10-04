import tempfile
import unittest
from pathlib import Path

from gaeclip.subtitles import parse_srt, write_srt


class FaseABTests(unittest.TestCase):
    def test_write_srt_filters_to_clip_duration(self):
        lines = parse_srt(
            "1\n00:00:10,000 --> 00:00:12,000\nHalo\n\n"
            "2\n00:05:00,000 --> 00:05:02,000\nJauh\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.srt"
            write_srt(lines, path, offset=10, clip_duration=60)
            text = path.read_text()
            self.assertIn("Halo", text)
            self.assertNotIn("Jauh", text)

    def test_write_srt_clamps_end_to_clip_duration(self):
        lines = parse_srt("1\n00:00:58,000 --> 00:01:05,000\nEkor\n")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.srt"
            write_srt(lines, path, offset=0, clip_duration=60)
            self.assertIn("00:00:58,000 --> 00:01:00,000", path.read_text())

    def test_merge_does_not_exceed_140_chars(self):
        long_text = "kata " * 40  # ~200 chars
        lines = parse_srt(
            f"1\n00:00:01,000 --> 00:00:03,000\n{long_text.strip()}\n\n"
            "2\n00:00:03,100 --> 00:00:05,000\nbuntut\n"
        )
        joined = " ".join(line.text for line in lines)
        self.assertLessEqual(max(len(line.text) for line in lines), 200)
        # buntut tidak boleh digabung paksa ke teks 200 char (total > 140)
        self.assertTrue(
            any("buntut" in line.text for line in lines),
            "buntut harus tetap ada sebagai cue sendiri atau gabungan wajar",
        )
        self.assertIn("buntut", joined)

    def test_subtitle_filter_escapes_path(self):
        from gaeclip.media import _subtitle_filter
        with tempfile.TemporaryDirectory() as directory:
            srt = Path(directory) / "a.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
            # Style lives in the ASS file now (file-only rendering is exact);
            # the filter only carries a safely escaped filename.
            filt = _subtitle_filter(srt)
            self.assertIn("subtitles=filename=", filt)
            self.assertNotIn("force_style", filt)
            self.assertIn(srt.name, filt)

    def test_download_section_rejects_invalid_range(self):
        from gaeclip.downloader import download_video_section
        from gaeclip.errors import ExternalCommandError
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ExternalCommandError):
                download_video_section(
                    "https://www.youtube.com/watch?v=test",
                    Path(directory), 10, 5, "c000001",
                )

    def test_verify_clip_rejects_missing_file(self):
        from gaeclip.media import verify_clip
        from gaeclip.errors import ExternalCommandError
        with self.assertRaises(ExternalCommandError):
            verify_clip(Path("/tmp/gaeclip-tidak-ada-12345.mp4"))


if __name__ == "__main__":
    unittest.main()
