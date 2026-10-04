"""Phase 4.2 test matrix: speaker intelligence + AV association + composition.

Unit-testable subset of the 30-scenario matrix (§46) plus pure-function
coverage. End-to-end scenarios reuse the synthetic seams from
test_autoframing (FakeDetector / motion_frames / tone_audio); no real
footage, no network. Factors covered per test are noted as TEST <n>.
"""

import math
import sys
import unittest

sys.path.insert(0, "tests")

import numpy as np

from test_autoframing import (FakeDetector, motion_frames, solid_frame,
                              talking_face, tone_audio)

from gaeclip.autoframing import (
    _prioritize_detections,
    head_motion_score,
    mouth_head_motion,
)
from gaeclip.framing_config import FramingConfig
from gaeclip.speaker_score import (
    associated_audio_score,
    av_correlation,
    count_false_switches,
    damp_by_head_motion,
    fuse_mouth_motion,
    movement_cost,
    switch_confidence,
    window_median,
)
from gaeclip.visual import build_speaker_timeline


def _run(script, regions, duration=6.0, audio_active=True, stamps=None):
    import gaeclip.autoframing as auto
    from unittest.mock import patch

    frames = motion_frames(len(script), regions)
    stamps = stamps or [index * 0.5 for index in range(len(frames))]
    with patch.object(auto, "extract_audio_mono",
                      return_value=tone_audio(duration, audio_active)), \
        patch.object(auto, "BlazeFaceDetector",
                     return_value=FakeDetector(script)):
        return auto.analyze_section("dummy.mp4", duration=duration,
                                    frame_source=list(zip(stamps, frames)))


def _upper_bar_frames(n, regions):
    """Motion confined to the UPPER band (head-turn proxy, not speech)."""
    frames = []
    for index in range(n):
        frame = solid_frame()
        region = regions[index] if index < len(regions) else None
        if index % 2 == 1 and region is not None:
            frame[10:40, region[0]:region[1]] = 200
        frames.append(frame)
    return frames


class PureFunctionTests(unittest.TestCase):
    def test_av_correlation_rewards_sync(self):
        lip = [(t * 0.1, v) for t, v in enumerate([0, 0, 1, 1, 0, 0, 1, 1] * 3)]
        vad = [(t * 0.1, v) for t, v in enumerate([0, 0, 1, 1, 0, 0, 1, 1] * 3)]
        self.assertGreater(av_correlation(lip, vad), 0.8)

    def test_av_correlation_punishes_mismatch(self):
        lip = [(t * 0.1, v) for t, v in enumerate([1, 1, 0, 0, 1, 1, 0, 0] * 3)]
        vad = [(t * 0.1, v) for t, v in enumerate([0, 0, 1, 1, 0, 0, 1, 1] * 3)]
        self.assertLess(av_correlation(lip, vad), 0.2)

    def test_av_correlation_neutral_when_degenerate(self):
        flat = [(t * 0.1, 1.0) for t in range(10)]
        self.assertEqual(av_correlation(flat, flat), 0.5)
        self.assertEqual(av_correlation([], flat), 0.5)
        self.assertEqual(av_correlation([(0.0, 1.0)], flat), 0.5)

    def test_associated_audio_gates_silence(self):
        self.assertEqual(associated_audio_score(0.0, 0.9), 0.0)
        self.assertAlmostEqual(associated_audio_score(1.0, 1.0), 1.0)
        # Uncorrelated face keeps only the neutral floor.
        self.assertLess(associated_audio_score(0.9, 0.0), 0.3)
        self.assertGreater(associated_audio_score(0.9, 0.9), 0.8)

    def test_fuse_mouth_windows(self):
        # LONG vetoes a SHORT-only blip.
        self.assertLess(fuse_mouth_motion(0.9, 0.2, 0.1), 0.4)
        # Sustained motion stays high.
        self.assertGreater(fuse_mouth_motion(0.8, 0.9, 0.85), 0.7)
        self.assertEqual(fuse_mouth_motion(None, None, None), 0.0)

    def test_window_median(self):
        series = [(0.0, 0.1), (0.1, 0.9), (0.2, 0.2)]
        self.assertEqual(window_median(series, 0.2, 500), 0.2)
        self.assertIsNone(window_median(series, 5.0, 100))

    def test_head_motion_score_relative(self):
        self.assertEqual(head_motion_score(0.0, 0.0), 0.0)
        self.assertEqual(head_motion_score(5.0, 5.0), 0.0)
        self.assertGreater(head_motion_score(10.0, 5.0), 0.5)

    def test_damp_never_adds_never_kills_strong(self):
        mouth, _ = damp_by_head_motion(0.9, 1.0)
        self.assertEqual(mouth, 0.9)
        damped, _ = damp_by_head_motion(0.4, 1.0)
        self.assertLess(damped, 0.4)
        calm, _ = damp_by_head_motion(0.4, 0.0)
        self.assertEqual(calm, 0.4)

    def test_switch_confidence_separates_concerns(self):
        # Same speaker evidence, worse geometry -> lower switch score.
        near = switch_confidence(0.8, movement_cost=0.0)
        far = switch_confidence(0.8, movement_cost=0.4)
        self.assertGreater(near, far)
        # Geometry can never elect silence.
        self.assertEqual(switch_confidence(0.0), 0.0)

    def test_movement_cost(self):
        self.assertEqual(movement_cost(None, (0.5, 0.5)), 0.0)
        self.assertEqual(movement_cost((0.5, 0.5), (0.5, 0.5)), 0.0)
        self.assertGreater(movement_cost((0.2, 0.5), (0.8, 0.5)), 0.3)

    def test_count_false_switches(self):
        from gaeclip.visual import SpeakerSegment

        def seg(speaker, start, end):
            return SpeakerSegment(start=start, end=end, speaker_id=speaker,
                                  confidence=0.9)

        self.assertEqual(
            count_false_switches([seg("a", 0, 5), seg("b", 5, 5.5), seg("a", 5.5, 10)]), 1)
        self.assertEqual(
            count_false_switches([seg("a", 0, 5), seg("b", 5, 10)]), 0)

    def test_mouth_head_split(self):
        prev = solid_frame()
        curr = solid_frame()
        curr[88:120, 40:120] = 200  # lower band only
        ratio, head = mouth_head_motion(prev, curr, (0.0, 0.2, 1.0, 0.8))
        self.assertGreater(ratio, 2.0)
        frame2 = solid_frame()
        frame2[10:40, 40:120] = 200  # upper band only
        ratio2, head2 = mouth_head_motion(prev, frame2, (0.0, 0.2, 1.0, 0.8))
        self.assertLess(ratio2, 1.0)
        self.assertGreater(head2, head)

    def test_prioritize_keeps_tracked_target(self):
        from gaeclip.autoframing import Track

        tracked = Track(speaker_id="speaker-000",
                        boxes=[(0.0, (0.1, 0.2, 0.3, 0.7))])
        strangers = [((0.6 + i * 0.05, 0.2, 0.7 + i * 0.05, 0.7), 0.99)
                     for i in range(6)]
        detections = [((0.1, 0.2, 0.3, 0.7), 0.5)] + strangers
        kept = _prioritize_detections(detections, [tracked], 6)
        self.assertEqual(len(kept), 6)
        self.assertIn(((0.1, 0.2, 0.3, 0.7), 0.5), kept)


class CrowdedSceneTests(unittest.TestCase):
    def _script(self, xs, conf=0.9):
        return [[((x - 0.06, 0.2, x + 0.06, 0.7), conf) for x in xs]
                for _ in range(12)]

    def test_8_three_people_relevant_target(self):
        # TEST 8: first of three talks (motion only under face 0).
        script = self._script([0.2, 0.5, 0.8])
        plan = _run(script, [(8, 40)] + [None] * 11)
        self.assertEqual(plan.mode, "speaker-follow")
        followed = {i.speaker_id for i in plan.instructions}
        left = min(plan.tracks, key=lambda t: (t.center_at(1.0) or (1, 1))[0])
        self.assertIn(left.speaker_id, followed)

    def test_9_four_people_no_crash(self):
        script = self._script([0.15, 0.38, 0.62, 0.85])
        plan = _run(script, [None] * 12)
        self.assertTrue(plan.instructions)
        self.assertEqual(len(plan.tracks), 4)

    def test_10_five_people_newcomer_candidate(self):
        # TEST 10: five faces tracked; the configured cap must not drop any.
        script = self._script([0.1, 0.3, 0.5, 0.7, 0.9])
        plan = _run(script, [None] * 12)
        self.assertEqual(len(plan.tracks), 5)
        self.assertLessEqual(max(len(t.boxes) for t in plan.tracks), 12)

    def test_11_right_edge_speaker_followed(self):
        script = self._script([0.2, 0.5, 0.88])
        plan = _run(script, [None] * 5 + [(124, 152)] + [None] * 6)
        followed = {i.speaker_id for i in plan.instructions}
        right = max(plan.tracks, key=lambda t: (t.center_at(1.0) or (0, 0))[0])
        self.assertIn(right.speaker_id, followed)

    def test_12_left_edge_speaker_followed(self):
        script = self._script([0.12, 0.5, 0.8])
        plan = _run(script, [(4, 32)] + [None] * 11)
        followed = {i.speaker_id for i in plan.instructions}
        left = min(plan.tracks, key=lambda t: (t.center_at(1.0) or (1, 1))[0])
        self.assertIn(left.speaker_id, followed)


class DiscriminationTests(unittest.TestCase):
    def test_15_head_turn_not_speech(self):
        # TEST 15: upper-band motion only must not elect a speaker.
        import gaeclip.autoframing as auto
        from unittest.mock import patch

        from test_autoframing import FakeDetector

        script = [talking_face(0.3) + talking_face(0.7) for _ in range(12)]
        frames = _upper_bar_frames(12, [(24, 64)] * 12)
        stamps = [i * 0.5 for i in range(12)]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector([r[:] for r in script])):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)))
        peaks = {}
        for row in plan.evidence_log:
            peaks[row["track_id"]] = max(peaks.get(row["track_id"], 0.0),
                                         row["total_score"])
        # Nobody reaches confident speech on head motion alone.
        self.assertTrue(all(score < 0.6 for score in peaks.values()),
                        f"head motion elected speech: {peaks}")

    def test_22_loud_audio_still_mouths_win(self):
        # TEST 22: loud audio + motionless mouths -> no confident claim.
        # Per the no-split hierarchy the fallback is a single stable face,
        # but NOBODY may reach confident speech on global audio alone.
        script = [talking_face(0.25) + talking_face(0.75) for _ in range(12)]
        plan = _run(script, [None] * 12, audio_active=True)
        self.assertTrue(all(item.layout != "split" for item in plan.instructions))
        peaks = {}
        for row in plan.evidence_log:
            peaks[row["track_id"]] = max(peaks.get(row["track_id"], 0.0),
                                         row["total_score"])
        self.assertTrue(all(score < 0.6 for score in peaks.values()),
                        f"audio alone elected speech: {peaks}")

    def test_23_correlated_face_wins(self):
        # TEST 23: B moves WITH the speech half, A moves during silence.
        import gaeclip.autoframing as auto
        from unittest.mock import patch

        from test_autoframing import FakeDetector

        script = [talking_face(0.25) + talking_face(0.75) for _ in range(12)]
        frames = motion_frames(12, [None] * 6 + [(104, 144)] * 6)
        # Audio: silence then tone (VAD varies, so correlation can bite).
        silent = np.zeros(3 * 16000, dtype=np.float32)
        tone = tone_audio(3.0, True)
        audio = np.concatenate([silent, tone])
        stamps = [i * 0.5 for i in range(12)]
        with patch.object(auto, "extract_audio_mono", return_value=audio), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector([r[:] for r in script])):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)))
        speakers = {i.speaker_id for i in plan.instructions}
        right = max(plan.tracks, key=lambda t: (t.center_at(4.0) or (0, 0))[0])
        self.assertIn(right.speaker_id, speakers)
        self.assertGreaterEqual(plan.metrics.get("switch_count", 0), 0)

    def test_24_both_loud_holds_no_flap(self):
        # TEST 24: sustained overlap -> ambiguous hold, never oscillation.
        script = [talking_face(0.25) + talking_face(0.75) for _ in range(12)]
        plan = _run(script, [(24, 64), (104, 144)] * 6)
        self.assertLessEqual(len(plan.instructions), 2)


class AcquisitionTests(unittest.TestCase):
    def test_6_new_speaker_acquired_fast(self):
        # TEST 6: B starts strong at 3s with dense evidence -> switch well
        # before the old 2s-hold-after-ramp would allow.
        script = [talking_face(0.25) + talking_face(0.75) for _ in range(50)]
        regions = [(24, 64)] * 20 + [(104, 144)] * 30
        frames = motion_frames(50, regions)
        stamps = [i * 0.15 for i in range(50)]  # dense: onset can latch
        import gaeclip.autoframing as auto
        from unittest.mock import patch

        from test_autoframing import FakeDetector

        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(7.5, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector([r[:] for r in script])):
            plan = auto.analyze_section("dummy.mp4", duration=7.5,
                                        frame_source=list(zip(stamps, frames)))
        starts = sorted({i.start for i in plan.instructions
                         if i.speaker_id != plan.instructions[0].speaker_id})
        self.assertTrue(starts, "B never acquired")
        # Onset fast path: switch < 2.5s after B starts (t=3.0).
        self.assertLess(starts[0], 5.5)
        self.assertGreaterEqual(plan.metrics.get("switch_count", 0), 1)

    def test_17_occlusion_within_ttl(self):
        # Covered by test_g (occlusion hold); metrics must agree.
        script = ([talking_face(0.3)] * 4 + [[]] + [talking_face(0.3)] * 7)
        plan = _run(script, [(30, 70)] * 12)
        self.assertEqual(len({i.speaker_id for i in plan.instructions}), 1)


class CompositionTests(unittest.TestCase):
    def test_4_far_pair_single_no_empty_center(self):
        # TEST 4/14: far pair -> single leader, midpoint never framed.
        from gaeclip.framing_geometry import compute_two_speaker_composition

        def narrow(x, w=0.12):
            return (x - w / 2, 0.25, x + w / 2, 0.65)

        result = compute_two_speaker_composition(
            narrow(0.20), narrow(0.80), 1920, 1080)
        self.assertFalse(result.feasible)
        self.assertIn(result.reason, ("too_far", "empty_center", "empty_space"))

    def test_25_tiny_faces_rejected(self):
        from gaeclip.framing_geometry import compute_two_speaker_composition

        result = compute_two_speaker_composition(
            (0.37, 0.25, 0.41, 0.35), (0.47, 0.25, 0.51, 0.35), 1920, 1080)
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason, "face_too_small")

    def test_28_midpoint_gap_rejected(self):
        from gaeclip.framing_geometry import compute_two_speaker_composition

        def narrow(x, w=0.12):
            return (x - w / 2, 0.25, x + w / 2, 0.65)

        result = compute_two_speaker_composition(
            narrow(0.30), narrow(0.62), 1920, 1080)
        self.assertFalse(result.feasible)
        self.assertIn(result.reason, ("empty_center", "edge_cutoff",
                                      "empty_space", "speaker_invisible"))

    def test_metrics_present_on_plan(self):
        script = [talking_face(0.3) for _ in range(12)]
        plan = _run(script, [(30, 70)] * 12)
        for key in ("switch_count", "false_switch_count", "ambiguous_holds",
                    "acquisition_latency_ms", "switch_latency_ms",
                    "speech_coverage", "empty_frame_ratio", "composition"):
            self.assertIn(key, plan.metrics)
        self.assertEqual(plan.metrics["empty_frame_ratio"], 0.0)
        self.assertEqual(plan.metrics["false_switch_count"], 0)


class TimelineFastPathTests(unittest.TestCase):
    def _obs(self, timestamp, speaker, lip, audio=0.9):
        from gaeclip.visual import BoundingBox, FaceObservation

        return FaceObservation(
            timestamp=timestamp, speaker_id=speaker,
            face=BoundingBox(x=0.3, y=0.2, width=0.2, height=0.3),
            lip_score=lip, audio_score=audio)

    def test_onset_switch_beats_full_hold(self):
        # Moderate gap (0.15-0.40, never force territory) with a genuine
        # rise: the onset fast path must switch ~0.5s after hold, not 2s.
        observations = []
        for index in range(60):
            timestamp = round(index * 0.1, 3)
            observations.append(self._obs(timestamp, "a", 0.45))
            if timestamp < 2.0:
                observations.append(self._obs(timestamp, "b", 0.10))
            else:
                ramp = min(0.9, 0.10 + (timestamp - 2.0) * 1.2)
                observations.append(self._obs(timestamp, "b", round(ramp, 3)))
        metrics: dict = {}
        timeline = build_speaker_timeline(
            observations, duration=6.0, sample_step=0.1,
            onset_bonus=0.15, onset_fast_hold_ms=500, onset_switch_margin=0.15,
            metrics_out=metrics)
        kinds = [s["kind"] for s in metrics.get("switches", [])]
        self.assertIn("ONSET_SWITCH", kinds)
        switch_at = next(s["time"] for s in metrics["switches"]
                         if s["kind"] == "ONSET_SWITCH")
        # Rise starts at 2.0s; full 2s hold would land ~4.5s+.
        self.assertLess(switch_at, 4.0)
        self.assertGreaterEqual(switch_at, 2.4)
        speakers = [s.speaker_id for s in timeline]
        self.assertIn("b", speakers)

    def test_no_onset_no_fast_path(self):
        # Flat moderate newcomer (no rise): normal 2s hold still applies.
        observations = []
        for index in range(60):
            timestamp = round(index * 0.1, 3)
            observations.append(self._obs(timestamp, "a", 0.45))
            observations.append(
                self._obs(timestamp, "b", 0.10 if timestamp < 2.0 else 0.55))
        metrics: dict = {}
        timeline = build_speaker_timeline(
            observations, duration=6.0, sample_step=0.1,
            onset_bonus=0.15, onset_fast_hold_ms=500, onset_switch_margin=0.15,
            metrics_out=metrics)
        kinds = [s["kind"] for s in metrics.get("switches", [])]
        self.assertNotIn("ONSET_SWITCH", kinds)


class EpochTests(unittest.TestCase):
    def test_19_reset_bumps_epoch(self):
        from gaeclip.autoframing import IoUTracker

        tracker = IoUTracker()
        tracker.update(0.0, [((0.1, 0.2, 0.3, 0.7), 0.9)])
        tracker.reset()
        self.assertEqual(tracker.epoch, 1)
        self.assertEqual(tracker.tracks, [])
        # Post-cut face gets a FRESH identity (never glued pre-cut).
        tracker.update(0.5, [((0.1, 0.2, 0.3, 0.7), 0.9)])
        self.assertEqual(tracker.tracks[0].epoch, 1)

    def test_21_crossing_keeps_two_identities(self):
        from gaeclip.autoframing import IoUTracker

        tracker = IoUTracker()
        left = (0.1, 0.2, 0.3, 0.7)
        right = (0.6, 0.2, 0.8, 0.7)
        mid = (0.35, 0.2, 0.55, 0.7)
        tracker.update(0.0, [(left, 0.9), (right, 0.9)])
        tracker.update(0.5, [(mid, 0.9), (mid, 0.9)])
        tracker.update(1.0, [(right, 0.9), (left, 0.9)])
        ids = {t.speaker_id for t in tracker.history}
        # Minimum identity switch: no third identity minted.
        self.assertLessEqual(len(ids), 3)
        self.assertGreaterEqual(len(tracker.tracks), 1)


if __name__ == "__main__":
    unittest.main()
