"""Max-words-per-line subtitle formatting (shared Whisper/YouTube path).

No network, no API keys. Covers tokenization, phrase-aware chunking,
timestamp distribution, config plumbing, and vertical-layout safety.
"""

import sys
import unittest

sys.path.insert(0, "tests")

from gaeclip.schemas import TranscriptLine
from gaeclip.subtitles import (
    chunk_words,
    normalize_max_words_per_line,
    split_cue_words,
    split_words,
)


def _line(text, start=0.0, end=10.0):
    return TranscriptLine(
        schema_version=1, line_id="l000001", start=start, end=end,
        text=text, source="youtube_auto",
    )


class TokenizeTests(unittest.TestCase):
    def test_short_sentences_untouched(self):
        self.assertEqual(split_words("Lagi ya."), ["Lagi", "ya."])
        self.assertEqual(split_words("Jangan kasih incest"), ["Jangan", "kasih", "incest"])
        self.assertEqual(split_words(""), [])
        self.assertEqual(split_words("   "), [])

    def test_hyphen_numbers_abbrev_apostrophe_unicode(self):
        self.assertEqual(split_words("gift-gift saweran"), ["gift-gift", "saweran"])
        self.assertEqual(split_words("JANGAN KASIH INCEST GIFT-GIFT SAWERAN LAGI YA.")[3], "GIFT-GIFT")
        self.assertEqual(split_words("tahun 2024, Dr. Budi"), ["tahun", "2024,", "Dr.", "Budi"])
        self.assertEqual(split_words("don't tak'kan berhenti"), ["don't", "tak'kan", "berhenti"])
        self.assertEqual(split_words("makanan enak 😋 sekali"), ["makanan", "enak", "😋", "sekali"])
        self.assertEqual(split_words("apa?! benarkah… ya"), ["apa?!", "benarkah…", "ya"])

    def test_punctuation_never_a_word_alone(self):
        for token in split_words("Halo, dunia! Apa kabar? Baik-baik saja."):
            self.assertTrue(any(ch.isalnum() or ord(ch) > 127 for ch in token), token)


class ChunkTests(unittest.TestCase):
    def test_short_needs_no_split(self):
        self.assertEqual(chunk_words(["a"]), [["a"]])
        self.assertEqual(chunk_words(["a", "b", "c"]), [["a", "b", "c"]])

    def test_spec_example_six_words(self):
        words = "JANGAN KASIH INCEST GIFT-GIFT SAWERAN LAGI YA.".split()
        self.assertEqual(
            chunk_words(words, 3),
            [["JANGAN", "KASIH", "INCEST"], ["GIFT-GIFT", "SAWERAN", "LAGI"], ["YA."]],
        )

    def test_twelve_words_four_lines(self):
        words = [f"k{i}" for i in range(12)]
        chunks = chunk_words(words, 3)
        self.assertEqual(len(chunks), 4)
        self.assertTrue(all(len(c) <= 3 for c in chunks))
        self.assertEqual([w for c in chunks for w in c], words)

    def test_punctuation_preferred_cut(self):
        self.assertEqual(
            chunk_words(["Jangan", "kasih", "gift-gift", "saweran", "lagi", "ya."], 3),
            [["Jangan", "kasih", "gift-gift"], ["saweran", "lagi", "ya."]],
        )
        # Cut lands after the period even though the window holds more words.
        self.assertEqual(
            chunk_words(["A", "B.", "C", "D"], 3),
            [["A", "B."], ["C", "D"]],
        )

    def test_order_no_add_no_drop(self):
        words = "satu dua, tiga! empat-lima enam? tujuh delapan sembilan sepuluh sebelas dua-belas".split()
        flat = [w for c in chunk_words(words, 3) for w in c]
        self.assertEqual(flat, words)

    def test_custom_cap_one_and_two(self):
        words = "a b c d e".split()
        self.assertEqual(chunk_words(words, 1), [["a"], ["b"], ["c"], ["d"], ["e"]])
        self.assertEqual(chunk_words(words, 2), [["a", "b"], ["c", "d"], ["e"]])

    def test_cap_never_exceeded(self):
        import random

        rng = random.Random(42)
        vocab = ["kata", "ya.", "gift-gift", "Dr.", "2024,", "apa?!", "saweran", "lagi"]
        for _ in range(200):
            words = [rng.choice(vocab) for _ in range(rng.randint(0, 15))]
            for cap in (1, 2, 3):
                for chunk in chunk_words(words, cap):
                    self.assertLessEqual(len(chunk), cap)


class TimestampTests(unittest.TestCase):
    def test_invalid_spans_yield_nothing(self):
        self.assertEqual(split_cue_words(5.0, 5.0, "halo dunia"), [])
        self.assertEqual(split_cue_words(6.0, 5.0, "halo dunia"), [])
        self.assertEqual(split_cue_words(0.0, 1.0, "   "), [])
        self.assertEqual(split_cue_words("a", "b", "halo"), [])

    def test_single_chunk_passthrough(self):
        self.assertEqual(split_cue_words(1.0, 4.0, "halo dunia"), [(1.0, 4.0, "halo dunia")])

    def test_proportional_chronological_no_overlap(self):
        cues = split_cue_words(10.0, 16.0, "satu dua tiga empat lima enam", 3)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0][0], 10.0)
        self.assertEqual(cues[-1][1], 16.0)
        for i, (start, end, text) in enumerate(cues):
            self.assertGreater(end, start, f"cue {i} has no duration")
            self.assertLessEqual(len(text.split()), 3)
            if i:
                self.assertGreaterEqual(start, cues[i - 1][1])
                self.assertGreater(start, cues[i - 1][0])

    def test_short_cue_still_positive(self):
        cues = split_cue_words(0.0, 0.3, "satu dua tiga empat lima enam", 3)
        self.assertEqual(len(cues), 2)
        for start, end, _ in cues:
            self.assertGreater(end, start)

    def test_words_kept_verbatim_with_punctuation(self):
        cues = split_cue_words(0.0, 9.0, "Jangan kasih gift-gift, saweran lagi ya!", 3)
        flat = " ".join(text for _, _, text in cues)
        self.assertEqual(flat, "Jangan kasih gift-gift, saweran lagi ya!")

    def test_deterministic(self):
        args = (2.5, 9.75, "satu dua tiga empat lima", 3)
        self.assertEqual(split_cue_words(*args), split_cue_words(*args))


class ConfigTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_max_words_per_line(None), 3)
        self.assertEqual(normalize_max_words_per_line(2), 2)
        self.assertEqual(normalize_max_words_per_line(1), 1)
        self.assertEqual(normalize_max_words_per_line(0), 1)
        self.assertEqual(normalize_max_words_per_line(8), 3)
        self.assertEqual(normalize_max_words_per_line("x"), 3)
        self.assertEqual(normalize_max_words_per_line(2.9), 2)

    def test_job_record_default_and_fingerprint(self):
        from gaeclip.jobs import JobRecord, JobRepository

        record = JobRecord(job_id="j", source_url="u", language="id")
        self.assertEqual(record.subtitle_max_words_per_line, 3)
        base = ("u", "id", 3, 30, 60, True, "casual_center", "bottom_center")
        same_a = JobRepository.request_fingerprint(*base, transcript_source="youtube_first")
        same_b = JobRepository.request_fingerprint(
            *base, transcript_source="youtube_first",
            subtitle_max_words_per_line=3)
        diff = JobRepository.request_fingerprint(
            *base, transcript_source="youtube_first",
            subtitle_max_words_per_line=2)
        self.assertEqual(same_a, same_b)
        self.assertNotEqual(same_a, diff)


class EndToEndTests(unittest.TestCase):
    def _ass_texts(self, **kwargs):
        import tempfile
        from pathlib import Path

        from gaeclip.subtitles import write_ass

        params = {"max_chars": 41, "width": 1080, "height": 1920}
        params.update(kwargs)
        lines = [
            _line("JANGAN KASIH INCEST GIFT-GIFT SAWERAN LAGI YA.", 0.0, 6.0),
            _line("Jangan kasih gift-gift saweran lagi ya.", 6.0, 12.0),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.ass"
            write_ass(lines, path, offset=0, clip_duration=12.0, **params)
            return path.read_text(encoding="utf-8")

    def _dialogues(self, ass_text):
        return [line for line in ass_text.splitlines() if line.startswith("Dialogue:")]

    def test_ass_every_line_max_3_words_default(self):
        for dialogue in self._dialogues(self._ass_texts()):
            text = dialogue.split(",", 9)[-1].replace("\\N", " ")
            for line in text.split("\n"):
                self.assertLessEqual(len(line.split()), 3, line)

    def test_ass_custom_cap_two(self):
        for dialogue in self._dialogues(self._ass_texts(max_words=2)):
            text = dialogue.split(",", 9)[-1].replace("\\N", " ")
            for line in text.split("\n"):
                self.assertLessEqual(len(line.split()), 2, line)

    def test_ass_timings_chronological_in_span(self):
        import re

        def stamp(value):
            hours, rest = value.split(":", 1)
            minutes, rest = rest.split(":", 1)
            secs, centis = rest.split(".")
            return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(centis) / 100

        prev_end = 0.0
        for dialogue in self._dialogues(self._ass_texts()):
            match = re.match(r"Dialogue: 0,([^,]+),([^,]+),", dialogue)
            assert match is not None
            start, end = stamp(match.group(1)), stamp(match.group(2))
            self.assertGreater(end, start)
            self.assertGreaterEqual(start, prev_end - 0.001)
            self.assertLessEqual(end, 12.001)
            prev_end = end

    def test_whisper_and_youtube_share_formatter(self):
        from gaeclip.subtitles import parse_srt, parse_vtt, write_srt

        import tempfile
        from pathlib import Path

        srt = "1\n00:00:01,000 --> 00:00:07,000\nJANGAN KASIH INCEST GIFT-GIFT SAWERAN LAGI YA.\n"
        vtt = "WEBVTT\n\n00:01.000 --> 00:07.000\nJANGAN KASIH INCEST GIFT-GIFT SAWERAN LAGI YA.\n"
        for parse, payload in ((parse_srt, srt), (parse_vtt, vtt)):
            lines = parse(payload)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "out.srt"
                write_srt(lines, path, offset=0, clip_duration=60)
                body = path.read_text(encoding="utf-8")
            for block in body.strip().split("\n\n"):
                words = " ".join(block.splitlines()[2:]).split()
                self.assertLessEqual(len(words), 3, block)
                self.assertGreater(len(words), 0)

    def test_old_records_render_with_default(self):
        # A JobRecord without the new field behaves like max_words=3.
        from gaeclip.jobs import JobRecord

        record = JobRecord(job_id="j", source_url="u", language="id")
        self.assertEqual(normalize_max_words_per_line(record.subtitle_max_words_per_line), 3)


if __name__ == "__main__":
    unittest.main()
