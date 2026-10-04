import tempfile
import unittest
from pathlib import Path

from gaeclip.media import subtitle_chars_per_line, subtitle_font_size
from gaeclip.subtitles import (
    _collapse_repeats,
    _split_long_cue,
    _wrap_text,
    parse_srt,
    write_ass,
    write_srt,
)


class SubtitleSizeTests(unittest.TestCase):
    def test_font_scales_with_smallest_dimension_and_caps(self):
        self.assertEqual(subtitle_font_size(1080, 1920), 42)  # portrait
        self.assertEqual(subtitle_font_size(1920, 1080), 42)  # landscape, same
        self.assertEqual(subtitle_font_size(1080, 1080), 42)  # square, same
        self.assertEqual(subtitle_font_size(2160, 3840), 44)  # hard cap
        self.assertEqual(subtitle_font_size(640, 480), 28)  # floor, stays readable
        self.assertEqual(subtitle_font_size(1280, 720), 28)

    def test_chars_per_line_matches_width(self):
        chars = subtitle_chars_per_line(1080, 1920)
        self.assertGreaterEqual(chars, 24)
        self.assertLessEqual(chars, 60)

    def test_ellipsis_artifacts_are_removed(self):
        from gaeclip.subtitles import _normalize_text
        self.assertEqual(_normalize_text("jadi ... gitu ... nah ..."), "jadi gitu nah")
        self.assertEqual(_normalize_text("tunggu... sebentar"), "tunggu sebentar")
        # Single periods (decimals, sentence ends) are preserved.
        self.assertEqual(_normalize_text("nilai 3.14 dan gede."), "nilai 3.14 dan gede.")

    def test_margins_raise_subtitle_off_bottom_edge(self):
        from gaeclip.media import subtitle_margins
        margin_lr, margin_v = subtitle_margins(1080, 1920)
        self.assertEqual((margin_lr, margin_v), (65, 230))
        # Lower safe area on other aspects too, never mid-frame.
        _, margin_land = subtitle_margins(1920, 1080)
        self.assertEqual(margin_land, 130)
        self.assertLess(margin_land, 1080 * 0.25)

    def test_collapse_repeats_drops_duplicated_phrase(self):
        self.assertEqual(
            _collapse_repeats("wah kamu dia karena dia tuh menganggap wah kamu"),
            "wah kamu dia karena dia tuh menganggap wah kamu",
        )
        repeated = "dia karena dia tuh menganggap wah kamu " * 2
        self.assertEqual(
            _collapse_repeats(repeated.strip()),
            "dia karena dia tuh menganggap wah kamu",
        )
        self.assertEqual(_collapse_repeats("halo dunia apa kabar"), "halo dunia apa kabar")

    def test_wrap_never_exceeds_two_lines(self):
        text = "kata " * 60
        wrapped = _wrap_text(text.strip(), max_chars=34)
        self.assertLessEqual(len(wrapped.splitlines()), 2)
        # Word order is preserved.
        self.assertEqual(wrapped.replace("\n", " ").split(), text.strip().split())

    def test_split_long_cue_preserves_text_and_time(self):
        text = "satu dua tiga empat lima enam tujuh delapan sembilan sepuluh"
        parts = _split_long_cue(0.0, 10.0, text, max_chars=20)
        self.assertGreaterEqual(len(parts), 2)
        self.assertEqual(" ".join(p[2] for p in parts).split(), text.split())
        # Timed halves are contiguous and cover the full range.
        self.assertAlmostEqual(parts[0][0], 0.0)
        self.assertAlmostEqual(parts[-1][1], 10.0)
        for first, second in zip(parts, parts[1:]):
            self.assertAlmostEqual(first[1], second[0])

    def test_end_to_end_cues_fit_two_lines(self):
        lines = parse_srt(
            "1\n00:00:00,000 --> 00:00:06,000\n"
            "iya agak dia karena dia tuh menganggap wah kamu dia karena dia tuh "
            "menganggap wah kamu ternyata enggak spesialis bisa dapat income dan gede\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.srt"
            write_srt(lines, path, offset=0, clip_duration=6, max_chars=41)
            for block in path.read_text().strip().split("\n\n"):
                cue_lines = block.splitlines()[2:]
                self.assertLessEqual(len(cue_lines), 2, f"cue exceeds 2 lines: {block!r}")
                for cue_line in cue_lines:
                    self.assertLessEqual(len(cue_line), 82)


class SentenceSegmentationTests(unittest.TestCase):
    def test_overlap_with_punctuation_difference_is_removed(self):
        from gaeclip.subtitles import _join_without_overlap
        joined = _join_without_overlap(
            "Saya beli makanan ini 300 juta dan saya makan ini cuma 3 kali,",
            "dan saya makan ini cuma 3 kali, jadi menurut saya kurang worth it.",
        )
        self.assertNotIn("cuma 3 kali, dan saya makan ini cuma 3 kali", joined)
        self.assertTrue(joined.startswith("Saya beli makanan ini"))
        self.assertTrue(joined.endswith("kurang worth it."))

    def test_case_insensitive_repeat_is_removed(self):
        from gaeclip.subtitles import _collapse_repeats
        self.assertEqual(
            _collapse_repeats("Saya Makan Nasi saya makan nasi"),
            "Saya Makan Nasi",
        )

    def test_user_bad_example_becomes_good(self):
        # Rolling-window VTT cues that repeat the sentence head.
        lines = parse_srt(
            "1\n00:00:00,000 --> 00:00:04,000\n"
            "Saya beli makanan ini 300 juta dan saya makan ini cuma 3 kali,\n\n"
            "2\n00:00:03,800 --> 00:00:08,000\n"
            "dan saya makan ini cuma 3 kali, jadi menurut saya kurang worth it.\n"
        )
        full = " ".join(line.text for line in lines)
        self.assertEqual(full.count("cuma 3 kali"), 1)
        # Timing still covers the whole speech span.
        self.assertAlmostEqual(lines[0].start, 0.0)
        self.assertAlmostEqual(lines[-1].end, 8.0)

    def test_long_sentence_splits_at_clause_without_repeat(self):
        lines = parse_srt(
            "1\n00:00:00,000 --> 00:00:10,000\n"
            "Saya sebenarnya tidak terlalu suka dengan makanan ini, karena rasanya terlalu manis\n"
        )
        texts = [line.text for line in lines]
        self.assertTrue(any("tidak terlalu suka dengan makanan ini" in t for t in texts))
        self.assertTrue(any(t.startswith("karena rasanya") for t in texts))
        # No word runs twice across the boundary.
        self.assertEqual(" ".join(texts).count("makanan ini"), 1)
        # Contiguous coverage, no gaps/overlaps in time.
        for first, second in zip(lines, lines[1:]):
            self.assertAlmostEqual(first.end, second.start)
        self.assertAlmostEqual(lines[0].start, 0.0)
        self.assertAlmostEqual(lines[-1].end, 10.0)

    def test_sentence_boundary_split_keeps_all_words(self):
        lines = parse_srt(
            "1\n00:00:00,000 --> 00:00:08,000\n"
            "Halo semuanya. Selamat datang di acara ini teman-teman\n"
        )
        self.assertEqual(len(lines), 2)
        self.assertEqual(" ".join(l.text for l in lines).split(),
                         "Halo semuanya. Selamat datang di acara ini teman-teman".split())

    def test_display_split_prefers_phrase_boundary(self):
        # No comma: display splitter must still avoid mid-phrase cuts.
        from gaeclip.subtitles import _split_long_cue
        parts = _split_long_cue(
            0.0, 10.0,
            "Saya sebenarnya tidak terlalu suka dengan makanan ini karena rasanya terlalu manis",
            max_chars=34,
        )
        self.assertGreaterEqual(len(parts), 2)
        self.assertTrue(parts[1][2].startswith("karena "))
        self.assertEqual(" ".join(p[2] for p in parts).split(),
                         "Saya sebenarnya tidak terlalu suka dengan makanan ini karena rasanya terlalu manis".split())


class WriteAssTests(unittest.TestCase):
    def test_ass_has_matching_playres_and_exact_size(self):
        lines = parse_srt("1\n00:00:00,000 --> 00:00:04,000\nHalo dunia\n")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.ass"
            write_ass(lines, path, offset=0, clip_duration=4, width=1080, height=1920)
            text = path.read_text()
            self.assertIn("PlayResX: 1080", text)
            self.assertIn("PlayResY: 1920", text)
            self.assertIn("DejaVu Sans,42", text)
            self.assertIn("Dialogue:", text)
            self.assertIn("Halo dunia", text)

    def test_ass_joins_wrapped_lines_with_backslash_n(self):
        lines = parse_srt(
            "1\n00:00:00,000 --> 00:00:04,000\n"
            "Ini kalimat yang cukup panjang sehingga harus terbagi dua baris subtitle\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.ass"
            write_ass(lines, path, offset=0, clip_duration=4, max_chars=20)
            dialogue = [line for line in path.read_text().splitlines() if line.startswith("Dialogue:")]
            self.assertTrue(dialogue)
            for line in dialogue:
                self.assertLessEqual(line.split(",")[-1].count(r"\N"), 1)


if __name__ == "__main__":
    unittest.main()
