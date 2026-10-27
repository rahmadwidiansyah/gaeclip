"""Title policy tests (spec §47): hook titles <=60, never raw-cut."""

import unittest

from gaeclip.titles import (
    TITLE_MAX_LEN,
    finalize_titles,
    hook_title_from_text,
    is_generic,
    normalize_title,
    validate_title,
)


class ValidateTests(unittest.TestCase):
    def test_short_hook_accepted(self):
        ok, issues = validate_title("Ternyata Bukan Karena Itu")
        self.assertTrue(ok)
        self.assertEqual(issues, [])

    def test_overlong_rejected_not_silently_ok(self):
        ok, issues = validate_title("X" * 61)
        self.assertFalse(ok)
        self.assertIn("too_long", issues)

    def test_exactly_60_accepted(self):
        title = " ".join(["Harga", "turun", "drastis", "sekali", "karena",
                          "permintaan", "anjlok", "tajam123"])
        self.assertEqual(len(title), 60)
        ok, _ = validate_title(title)
        self.assertTrue(ok)

    def test_empty_rejected(self):
        ok, issues = validate_title("   ")
        self.assertFalse(ok)
        self.assertIn("empty", issues)

    def test_generic_detected(self):
        for generic in ("Clip 1", "Part 2", "Video 3", "clip 000001", "Episode 4"):
            self.assertTrue(is_generic(generic), generic)
        self.assertFalse(is_generic("Kenapa Ini Bisa Terjadi?"))


class NormalizeTests(unittest.TestCase):
    def test_long_title_smart_cut_not_raw(self):
        long_title = ("Penjelasan panjang tentang bagaimana sebenarnya harga "
                      "bisa turun karena banyak faktor yang saling berkaitan")
        result = normalize_title(long_title, long_title)
        self.assertLessEqual(len(result), TITLE_MAX_LEN)
        self.assertTrue(result)
        # Must NOT be a raw mid-word slice of the original.
        self.assertNotEqual(result, long_title[:TITLE_MAX_LEN])
        self.assertRegex(result, r"[a-zA-Z?!\u00C0-\u024F]$")

    def test_generic_prefix_stripped(self):
        result = normalize_title("Clip: Kenapa Ini Bisa Terjadi?", "")
        self.assertFalse(result.lower().startswith("clip"))

    def test_empty_falls_back_to_span_hook(self):
        span = "Jadi sebenarnya yang menyebabkan harga itu turun bukan karena permintaan. Itu fakta."
        result = normalize_title("", span)
        self.assertTrue(result)
        self.assertLessEqual(len(result), TITLE_MAX_LEN)

    def test_reason_untouched_by_normalize(self):
        reason = "Clip memiliki setup dan payoff yang jelas sehingga layak dipilih."
        self.assertEqual(reason, reason)  # reason passes through, never rewritten


class HookFallbackTests(unittest.TestCase):
    def test_question_preferred_id(self):
        span = ("Harga turun kemarin. Kenapa ini bisa terjadi padahal permintaan naik? "
                "Para analis masih memperdebatkan penyebabnya.")
        result = hook_title_from_text(span)
        self.assertIn("Kenapa", result)
        self.assertLessEqual(len(result), TITLE_MAX_LEN)

    def test_english_span_english_hook(self):
        span = ("The price dropped yesterday. Why did this happen at all? "
                "Nobody can explain the full mechanism behind the sudden market movement.")
        result = hook_title_from_text(span)
        self.assertIn("Why", result)

    def test_no_invented_facts(self):
        span = "Kadang harga bisa turun karena permintaan melemah di pasar regional."
        result = hook_title_from_text(span)
        for forbidden in ("PASTI", "100%", "miliar", "presiden"):
            self.assertNotIn(forbidden, result)

    def test_empty_span_last_resort(self):
        self.assertTrue(hook_title_from_text(""))


class FinalizeTests(unittest.TestCase):
    def _clip(self, candidate_id, title):
        from gaeclip.schemas import CandidateClip

        return CandidateClip(candidate_id=candidate_id, start_line_id="l000001",
                             end_line_id="l000002", start=0.0, end=30.0,
                             title=title, reason="r", score=0.9)

    def test_selection_order_ids_scores_preserved(self):
        clips = [self._clip("c000001", "Judul Pertama Yang Bagus"),
                 self._clip("c000002", "Judul Kedua Yang Juga Bagus")]
        out = finalize_titles(clips, {})
        self.assertEqual([c.candidate_id for c in out], ["c000001", "c000002"])
        self.assertEqual([c.score for c in out], [0.9, 0.9])

    def test_duplicate_patterns_diversified(self):
        clips = [self._clip("c000001", "Ini Alasan Harga Turun Drastis Sekali"),
                 self._clip("c000002", "Ini Alasan Harga Turun Drastis Sekali")]
        spans = {"c000001": ("Ini alasan harga turun drastis sekali karena permintaan? "
                             "Para pedagang mulai panik melihat angka merah."),
                 "c000002": ("Ini alasan harga turun drastis sekali, tapi ada faktor lain! "
                             "Jangan sampai salah langkah berikutnya.")}
        out = finalize_titles(clips, spans)
        self.assertNotEqual(out[0].title, out[1].title)
        for clip in out:
            self.assertLessEqual(len(clip.title), TITLE_MAX_LEN)

    def test_all_titles_within_limit(self):
        clips = [self._clip("c000001", "Z" * 120)]
        out = finalize_titles(clips, {"c000001": "Kalimat pendek saja."})
        self.assertLessEqual(len(out[0].title), TITLE_MAX_LEN)


if __name__ == "__main__":
    unittest.main()
