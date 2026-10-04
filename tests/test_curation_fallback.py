"""Fallback hierarchy tests: AI -> heatmap -> heuristic -> deterministic."""

import unittest

from gaeclip.curation import (
    classify_gemini_error,
    curate_with_fallback,
    deterministic_candidates,
    heatmap_usable,
)
from gaeclip.errors import ValidationError
from gaeclip.heuristic import HEURISTIC_MIN_SCORE, heuristic_candidates
from gaeclip.schemas import TranscriptLine


def make_lines(specs, language="id"):
    """specs: list of (start, end, text)."""
    return [
        TranscriptLine(
            line_id=f"l{i + 1:06d}", start=start, end=end, text=text,
            language=language, source="youtube_auto",
        )
        for i, (start, end, text) in enumerate(specs)
    ]


def block_lines(n_blocks, block_secs=10, text="Kalimat isi konten video."):
    specs = []
    for i in range(n_blocks):
        specs.append((i * block_secs, (i + 1) * block_secs, f"{text} Bagian {i}."))
    return make_lines(specs)


class FakeCurator:
    provider_name = "gemini"

    def __init__(self, candidates=None, error=None):
        self._candidates = candidates or []
        self._error = error

    def curate(self, transcript):
        if self._error is not None:
            raise self._error
        from gaeclip.curation import CurationResult
        return CurationResult(candidates=self._candidates, source="gemini", usage=None)


class HierarchyTests(unittest.TestCase):
    def test_a_gemini_success_uses_ai(self):
        from gaeclip.schemas import CandidateClip
        lines = block_lines(6)
        curator = FakeCurator(candidates=[
            CandidateClip(
                candidate_id="c000001", start_line_id="l000001", end_line_id="l000004",
                start=0, end=40, title="Hook", reason="AI", score=0.9,
            )
        ])
        result = curate_with_fallback(lines, curator, 30, 60, 3)
        self.assertEqual(result.curation_method, "ai")
        self.assertTrue(result.ai_used)
        self.assertFalse(result.fallback_used)
        self.assertEqual(result.source, "gemini")

    def test_b_timeout_with_heatmap_uses_heatmap(self):
        lines = block_lines(6)
        curator = FakeCurator(error=TimeoutError("timed out"))
        heatmap = [{"start_time": 30, "end_time": 40, "value": 5.0}]
        result = curate_with_fallback(lines, curator, 30, 60, 1, heatmap=heatmap)
        self.assertEqual(result.curation_method, "heatmap")
        self.assertTrue(result.heatmap_used)
        self.assertTrue(result.fallback_used)
        self.assertIn("heatmap", result.fallback_reason.lower())

    def test_c_timeout_no_heatmap_uses_heuristic(self):
        specs = [(i * 10, (i + 1) * 10, "Obrolan biasa saja tanpa makna khusus.") for i in range(12)]
        specs[6] = (60, 70, "Ternyata harganya 300 juta! Kenapa bisa semahal itu?")
        lines = make_lines(specs)
        curator = FakeCurator(error=TimeoutError("timed out"))
        result = curate_with_fallback(lines, curator, 30, 60, 1)
        self.assertEqual(result.curation_method, "heuristic")
        self.assertFalse(result.heatmap_used)
        self.assertTrue(result.fallback_used)
        self.assertEqual(len(result.candidates), 1)
        # The hook-rich middle window must win over the first windows.
        self.assertGreaterEqual(result.candidates[0].start, 30)

    def test_d_unusable_transcript_falls_to_deterministic(self):
        lines = make_lines([(0, 5, "Halo.")])
        result = curate_with_fallback(lines, FakeCurator(error=TimeoutError("x")), 30, 60, 3)
        self.assertEqual(result.curation_method, "deterministic")
        self.assertEqual(result.candidates, [])

    def test_all_zero_heatmap_goes_to_heuristic(self):
        lines = block_lines(6)
        curator = FakeCurator(error=RuntimeError("boom"))
        heatmap = [{"start_time": 0, "end_time": 60, "value": 0.0}]
        result = curate_with_fallback(lines, curator, 30, 60, 1, heatmap=heatmap)
        self.assertIn(result.curation_method, ("heuristic", "deterministic"))
        self.assertFalse(result.heatmap_used)


class FailureClassificationTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify_gemini_error(TimeoutError("timed out")), "gemini_timeout")
        self.assertEqual(classify_gemini_error(RuntimeError("429 quota exceeded")), "gemini_rate_limit")
        self.assertEqual(classify_gemini_error(RuntimeError("401 unauthorized")), "gemini_auth_error")
        self.assertEqual(classify_gemini_error(RuntimeError("model not found")), "gemini_model_unavailable")
        self.assertEqual(classify_gemini_error(ConnectionError("dns failed")), "gemini_network_error")
        self.assertEqual(
            classify_gemini_error(ValidationError("LLM returned invalid JSON")),
            "gemini_invalid_json",
        )
        self.assertEqual(
            classify_gemini_error(ValidationError("LLM response does not match the curation schema")),
            "gemini_invalid_response",
        )
        self.assertEqual(classify_gemini_error(RuntimeError("weird")), "unknown_ai_error")

    def test_skip_kinds(self):
        lines = make_lines([(0, 40, "Isi konten biasa.")])
        result = curate_with_fallback(lines, None, 30, 60, 1, skip_kind="gemini_api_key_missing",
                                      skipped_reason="AI tidak dicoba (GEMINI_API_KEY kosong)")
        self.assertIn("gemini_api_key_missing", result.fallback_reason)
        result = curate_with_fallback(lines, None, 30, 60, 1, skip_kind="transcript_too_long",
                                      skipped_reason="AI dilewati (transkrip kepanjangan)")
        self.assertIn("transcript_too_long", result.fallback_reason)


class HeuristicSignalTests(unittest.TestCase):
    def test_e_middle_beats_early_filler_on_long_video(self):
        specs = []
        for i in range(180):  # 30 minutes, 10s lines
            specs.append((i * 10, (i + 1) * 10, "Eh anu ya kan gitu dong halo semuanya."))
        specs[90] = (900, 910, "Ternyata harganya 300 juta! Kenapa bisa semahal itu, gimana menurut kalian?")
        lines = make_lines(specs)
        selected, _, best = heuristic_candidates(lines, 30, 60, 3)
        self.assertTrue(selected, "expected heuristic candidates")
        self.assertGreaterEqual(best, HEURISTIC_MIN_SCORE)
        starts = [c.start for c in selected]
        self.assertTrue(any(s >= 600 for s in starts), f"middle/akhir harus menang: {starts}")
        self.assertFalse(all(s < 180 for s in starts), "tidak boleh semuanya dari 0-3 menit")

    def test_f_specificity_wins(self):
        specs = [
            (0, 35, "Ini mahal banget. Pokoknya mahal sekali dan sangat mahal."),
            (35, 70, "Ini harganya 300 juta dan terjual 500 unit dalam 2 hari."),
        ]
        lines = make_lines(specs)
        selected, _, _ = heuristic_candidates(lines, 30, 60, 1)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].start, 35)

    def test_g_question_hook_wins(self):
        specs = [
            (0, 35, "Hari ini cuacanya cerah sekali dan burung berkicau merdu."),
            (35, 70, "Kenapa hal ini bisa terjadi? Ternyata jawabannya mengejutkan!"),
        ]
        lines = make_lines(specs)
        selected, _, _ = heuristic_candidates(lines, 30, 60, 1)
        self.assertEqual(selected[0].start, 35)

    def test_h_filler_penalized(self):
        specs = [
            (0, 40, "Jadi saya makan ini, saya makan ini, saya makan ini eh anu ya kan."),
            (40, 80, "Ternyata hasilnya luar biasa terbaik pertama kali dalam sejarah!"),
        ]
        lines = make_lines(specs)
        selected, _, _ = heuristic_candidates(lines, 30, 60, 1)
        self.assertEqual(selected[0].start, 40)

    def test_i_boundary_adjustment_reaches_sentence_end(self):
        from gaeclip.heuristic import _snap_end
        specs = [
            (0, 10, "Pembuka kalimat biasa saja."),
            (10, 20, "Lalu NARASI PENTING dimulai di sini"),
            (20, 30, "dan berlanjut ke bagian ini."),
            (30, 43, "Penutup yang selesai."),
        ]
        lines = make_lines(specs)
        # A target ending mid-sentence must extend to the sentence end (30s).
        self.assertEqual(_snap_end(lines, 1, 1, 10, 60), 2)
        # Selected windows must end on punctuation or at duration/transcript edge.
        selected, _, _ = heuristic_candidates(lines, 15, 60, 3)
        self.assertTrue(selected)
        for candidate in selected:
            end_line = next(line for line in lines if line.end == candidate.end)
            at_edge = candidate.end >= 43 or (candidate.end - candidate.start) >= 60 - 1e-6
            self.assertTrue(
                end_line.text.strip().endswith((".", "!", "?", ",", ";", ":")) or at_edge,
                f"window {(candidate.start, candidate.end)} terpotong di tengah kalimat",
            )

    def test_breakdown_present_for_debugging(self):
        lines = block_lines(6)
        _, breakdown, _ = heuristic_candidates(lines, 30, 60, 1)
        self.assertTrue(breakdown)
        sample = next(iter(breakdown.values()))
        for key in ("hook", "density", "sentence", "emphasis", "specificity",
                    "curiosity", "completeness", "position", "repetition_penalty", "final"):
            self.assertIn(key, sample)


class ConstraintTests(unittest.TestCase):
    def _check(self, candidates, min_d, max_d, max_clips):
        self.assertLessEqual(len(candidates), max_clips)
        for candidate in candidates:
            duration = candidate.end - candidate.start
            self.assertGreaterEqual(duration, min_d - 1e-6)  # L
            self.assertLessEqual(duration, max_d + 1e-6)  # M

    def test_j_non_overlapping(self):
        lines = block_lines(20)
        selected, _, _ = heuristic_candidates(lines, 30, 60, 3)
        for i, first in enumerate(selected):
            for second in selected[i + 1:]:
                overlap = first.start < second.end and second.start < first.end
                self.assertFalse(overlap, f"overlap: {first} vs {second}")

    def test_k_max_clips_respected(self):
        lines = block_lines(20)
        selected, _, _ = heuristic_candidates(lines, 30, 60, 3)  # K
        self.assertLessEqual(len(selected), 3)

    def test_lm_durations_within_bounds(self):
        lines = block_lines(20)
        selected, _, _ = heuristic_candidates(lines, 30, 60, 3)
        self._check(selected, 30, 60, 3)

    def test_deterministic_constraints(self):
        lines = block_lines(20)
        selected = deterministic_candidates(lines, 30, 60, 3)
        self._check(selected, 30, 60, 3)


class DeterministicUpgradeTests(unittest.TestCase):
    def test_sentence_boundary_preferred_over_chronology(self):
        specs = [
            (0, 35, "dan melanjutkan obrolan yang belum selesai"),
            (35, 70, "Fakta mengejutkan terungkap hari ini."),
        ]
        lines = make_lines(specs)
        selected = deterministic_candidates(lines, 30, 60, 1)
        self.assertEqual(len(selected), 1)
        # Clean sentence edges beat the earlier filler start.
        self.assertEqual(selected[0].start, 35)

    def test_heatmap_still_wins_over_bonus(self):
        lines = make_lines([
            (0, 10, "Pembuka."),
            (10, 20, "Bagian ramai."),
            (20, 30, "Penutup."),
        ])
        selected = deterministic_candidates(
            lines, min_duration=10, max_duration=20, max_clips=1,
            heatmap=[{"start_time": 10, "end_time": 20, "value": 1.0}],
        )
        self.assertEqual(selected[0].start, 10)
        self.assertEqual(selected[0].reason, "YouTube audience heatmap fallback")


if __name__ == "__main__":
    unittest.main()
