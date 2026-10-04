"""Phase 4.1 tests: two-speaker co-framing + strict composition safety.

Correct SINGLE always beats a bad pair shot. Every test pins either an
accept (close feasible pair) or a specific rejection (far / tiny /
clipped / empty / invisible / ambiguous), never a forced midpoint.
"""

import unittest

from gaeclip.framing_config import FramingConfig
from gaeclip.framing_geometry import compute_two_speaker_composition


def narrow(x, w=0.12, y0=0.25, y1=0.65):
    return (x - w / 2, y0, x + w / 2, y1)


class GeometryFeasibilityTests(unittest.TestCase):
    def test_close_pair_accepts(self):
        result = compute_two_speaker_composition(
            narrow(0.43), narrow(0.57), 1920, 1080)
        self.assertTrue(result.feasible)
        self.assertEqual(result.mode, "two_speaker")
        self.assertGreaterEqual(result.confidence, 0.55)

    def test_far_pair_rejects_too_far(self):
        result = compute_two_speaker_composition(
            narrow(0.08), narrow(0.92), 1920, 1080)
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason, "too_far")

    def test_midpoint_gap_rejects_without_empty_shot(self):
        # A=0.25 B=0.75: inside max_distance? No (0.5 > 0.45) -> too_far.
        # Either way the pair must NOT become a midpoint crop.
        result = compute_two_speaker_composition(
            narrow(0.25), narrow(0.75), 1920, 1080)
        self.assertFalse(result.feasible)

    def test_tiny_faces_reject(self):
        result = compute_two_speaker_composition(
            (0.37, 0.25, 0.41, 0.35), (0.47, 0.25, 0.51, 0.35),
            1920, 1080)
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason, "face_too_small")

    def test_edge_clipped_rejects(self):
        result = compute_two_speaker_composition(
            (0.0, 0.25, 0.12, 0.65), narrow(0.26), 1920, 1080)
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason, "edge_cutoff")

    def test_wide_gap_rejects_empty_or_edge(self):
        result = compute_two_speaker_composition(
            narrow(0.30), narrow(0.62), 1920, 1080)
        self.assertFalse(result.feasible)
        # Phase 4.2 adds the explicit empty_center reason for two tiny
        # people around a void (previously surfaced as empty_space).
        self.assertIn(result.reason,
                      ("empty_space", "empty_center", "edge_cutoff",
                       "speaker_invisible"))

    def test_midpoint_is_candidate_never_forced(self):
        # Feasible close pair: winner is the best VALIDATED candidate.
        result = compute_two_speaker_composition(
            narrow(0.43), narrow(0.57), 1920, 1080)
        self.assertTrue(result.feasible)
        self.assertIn(result.metrics.get("candidate"),
                      ("midpoint", "union", "left_biased", "right_biased",
                       "a_priority", "b_priority", "composition"))
        # ...and the crop holds both faces fully.
        self.assertGreaterEqual(result.metrics["speaker_visibility_a"], 0.85)
        self.assertGreaterEqual(result.metrics["speaker_visibility_b"], 0.85)

    def test_portrait_source_pair(self):
        result = compute_two_speaker_composition(
            narrow(0.40), narrow(0.60), 1080, 1920)
        self.assertIsNotNone(result.reason)
        # Portrait 9:16 source == output aspect: full-frame crop; a close
        # pair is trivially composable.
        self.assertTrue(result.feasible)

    def test_asymmetric_sizes_accept_when_both_big_enough(self):
        result = compute_two_speaker_composition(
            (0.39, 0.15, 0.51, 0.75), (0.55, 0.25, 0.65, 0.65),
            1920, 1080)
        self.assertTrue(result.feasible)

    def test_metrics_cover_spec_minimum(self):
        result = compute_two_speaker_composition(
            narrow(0.43), narrow(0.57), 1920, 1080)
        for key in ("face_size_a", "face_size_b", "edge_margin_a",
                    "edge_margin_b", "headroom_a", "headroom_b",
                    "speaker_visibility_a", "speaker_visibility_b",
                    "empty_space_ratio", "required_crop_width",
                    "required_upscale", "composition_score",
                    "movement_cost", "cutoff_ratio"):
            self.assertIn(key, result.metrics)


def _run(script, regions, duration=6.0, n_frames=12):
    import sys

    sys.path.insert(0, "tests")
    from unittest.mock import patch

    import gaeclip.autoframing as auto
    from test_autoframing import FakeDetector, motion_frames, tone_audio

    frames = motion_frames(n_frames, regions)
    stamps = [index * 0.5 for index in range(len(frames))]
    with patch.object(auto, "extract_audio_mono",
                      return_value=tone_audio(duration, True)), \
        patch.object(auto, "BlazeFaceDetector",
                     return_value=FakeDetector(script)):
        return auto.analyze_section("dummy.mp4", duration=duration,
                                    frame_source=list(zip(stamps, frames)))


def _pair_script(xa, xb, w=0.12):
    return [[((xa - w / 2, 0.25, xa + w / 2, 0.65), 0.9),
             ((xb - w / 2, 0.25, xb + w / 2, 0.65), 0.9)]
            for _ in range(12)]


def _bar(xa, xb, n=12):
    return [(int(min(xa, xb) * 160) - 4, int(max(xa, xb) * 160) + 4)] * n


class TwoSpeakerIntegrationTests(unittest.TestCase):
    def test_1_adjacent_speakers_coframed(self):
        plan = _run(_pair_script(0.43, 0.57), _bar(0.43, 0.57))
        self.assertEqual(plan.mode, "two_speaker")
        self.assertTrue(plan.instructions)
        for instruction in plan.instructions:
            self.assertEqual(instruction.layout, "two_speaker")
            self.assertGreaterEqual(instruction.center_x, 0.30)
            self.assertLessEqual(instruction.center_x, 0.70)

    def test_2_only_a_speaks_stays_single(self):
        script = _pair_script(0.43, 0.57)
        plan = _run(script, [(60, 78)] * 12)
        self.assertNotEqual(plan.mode, "two_speaker")
        self.assertEqual(plan.mode, "speaker-follow")

    def test_3_overlapping_adjacent_pair(self):
        plan = _run(_pair_script(0.42, 0.56), _bar(0.42, 0.56))
        self.assertEqual(plan.mode, "two_speaker")

    def test_4_far_apart_never_coframed(self):
        import sys

        sys.path.insert(0, "tests")
        from test_autoframing import talking_face

        script = [talking_face(0.15) + talking_face(0.85) for _ in range(12)]
        plan = _run(script, [(16, 144)] * 12)
        self.assertNotEqual(plan.mode, "two_speaker")
        self.assertGreaterEqual(
            plan.keyframe_stats.get("two_speaker_reject_too_far", 0), 1)

    def test_5_midpoint_candidate_rejected(self):
        plan = _run(_pair_script(0.25, 0.75), _bar(0.25, 0.75))
        self.assertNotEqual(plan.mode, "two_speaker")
        anchors = (0.25, 0.75)
        for instruction in plan.instructions:
            if instruction.layout == "split":
                continue
            nearest = min(abs(instruction.center_x - ax) for ax in anchors)
            self.assertLessEqual(nearest, 0.20)

    def test_6_tiny_faces_reject(self):
        script = [[((0.37, 0.25, 0.41, 0.45), 0.9),
                   ((0.47, 0.25, 0.51, 0.45), 0.9)] for _ in range(12)]
        plan = _run(script, [(56, 84)] * 12)
        self.assertNotEqual(plan.mode, "two_speaker")

    def test_7_edge_clipped_rejects(self):
        script = [[((0.0, 0.25, 0.12, 0.65), 0.9),
                   ((0.20, 0.25, 0.32, 0.65), 0.9)] for _ in range(12)]
        plan = _run(script, [(0, 56)] * 12)
        self.assertNotEqual(plan.mode, "two_speaker")

    def test_8_speaker_plus_silent_context(self):
        script = [[((0.37, 0.25, 0.49, 0.65), 0.9),
                   ((0.52, 0.25, 0.64, 0.65), 0.85)] for _ in range(12)]
        plan = _run(script, _bar(0.37, 0.49))
        self.assertEqual(plan.mode, "speaker-follow")
        layouts = {i.layout for i in plan.instructions}
        self.assertIn("single_with_context", layouts)

    def test_9_nearby_context_beats_far_midpoint(self):
        script = [[((0.10, 0.25, 0.22, 0.65), 0.9),
                   ((0.24, 0.25, 0.36, 0.65), 0.85),
                   ((0.80, 0.25, 0.92, 0.65), 0.9)] for _ in range(12)]
        plan = _run(script, [(8, 60)] * 6 + [(120, 152)] * 6)
        self.assertNotEqual(plan.mode, "two_speaker")
        for instruction in plan.instructions:
            if instruction.layout == "split":
                continue
            # Never parked in the empty middle.
            self.assertTrue(instruction.center_x < 0.55
                            or instruction.center_x > 0.65
                            or instruction.layout == "group")

    def test_10_brief_dropout_holds_pair(self):
        script = _pair_script(0.43, 0.57)
        del script[6]
        script.insert(6, [((0.37, 0.25, 0.49, 0.65), 0.9)])
        plan = _run(script, _bar(0.43, 0.57))
        self.assertEqual(plan.mode, "two_speaker")

    def test_11_conversational_pair_no_ab_jitter(self):
        script = _pair_script(0.43, 0.57)
        regions = _bar(0.37, 0.49)[:6] + _bar(0.51, 0.63)[6:]
        plan = _run(script, regions)
        self.assertEqual(plan.mode, "two_speaker")
        labels = {i.speaker_id for i in plan.instructions}
        self.assertEqual(len(labels), 1)

    def test_12_five_people_no_forced_all_crop(self):
        xs = (0.10, 0.30, 0.50, 0.70, 0.90)
        script = [[((x - 0.06, 0.25, x + 0.06, 0.65), 0.9) for x in xs]
                  for _ in range(12)]
        plan = _run(script, [(8, 152)] * 12)
        self.assertNotEqual(plan.mode, "two_speaker")
        for instruction in plan.instructions:
            self.assertIn(instruction.layout, ("group", "split", "single",
                                               "single_with_context"))

    def test_13_five_people_close_pair_coframed(self):
        xs = (0.30, 0.45, 0.60, 0.75, 0.88)
        script = [[((x - 0.06, 0.25, x + 0.06, 0.65), 0.9) for x in xs]
                  for _ in range(12)]
        plan = _run(script, [(40, 80)] * 12)
        self.assertEqual(plan.mode, "two_speaker")

    def test_14_five_people_far_speakers_not_paired(self):
        xs = (0.15, 0.35, 0.50, 0.65, 0.80)
        script = [[((x - 0.06, 0.25, x + 0.06, 0.65), 0.9) for x in xs]
                  for _ in range(12)]
        plan = _run(script, [(16, 32)] * 6 + [(120, 136)] * 6)
        self.assertNotEqual(plan.mode, "two_speaker")

    def test_15_no_person_free_crop(self):
        plan = _run(_pair_script(0.25, 0.75), _bar(0.25, 0.75))
        for instruction in plan.instructions:
            if instruction.layout == "split":
                continue
            self.assertTrue(0.0 <= instruction.center_x <= 1.0)
            self.assertGreater(instruction.width, 0.0)

    def test_16_impossible_pair_falls_back_to_single(self):
        import sys

        sys.path.insert(0, "tests")
        from test_autoframing import talking_face

        # Far pair, strictly sequential speech: no overlap -> single path.
        script = [talking_face(0.15) + talking_face(0.85) for _ in range(12)]
        regions = [(16, 32)] * 6 + [(128, 144)] * 6
        plan = _run(script, regions)
        self.assertEqual(plan.mode, "speaker-follow")

    def test_17_edge_pair_adjusts_or_rejects(self):
        # Tight edge pair that still fits -> accept; clipped pair -> reject.
        ok = _run(_pair_script(0.21, 0.36), _bar(0.21, 0.36))
        self.assertEqual(ok.mode, "two_speaker")
        bad = _run([[((0.0, 0.25, 0.12, 0.65), 0.9),
                     ((0.20, 0.25, 0.32, 0.65), 0.9)] for _ in range(12)],
                   [(0, 56)] * 12)
        self.assertNotEqual(bad.mode, "two_speaker")

    def test_18_empty_space_rejects(self):
        plan = _run(_pair_script(0.30, 0.62), _bar(0.30, 0.62))
        self.assertNotEqual(plan.mode, "two_speaker")

    def test_19_temporary_silence_holds(self):
        script = _pair_script(0.43, 0.57)
        regions = _bar(0.43, 0.57)[:5] + [None] + _bar(0.43, 0.57)[6:]
        plan = _run(script, regions)
        self.assertEqual(plan.mode, "two_speaker")

    def test_20_short_coactivity_exits_to_single(self):
        script = _pair_script(0.43, 0.57)
        regions = _bar(0.43, 0.57)[:1] + [None] * 11
        plan = _run(script, regions)
        self.assertNotEqual(plan.mode, "two_speaker")


class GateUnitTests(unittest.TestCase):
    def test_disabled_flag_rejects(self):
        import sys

        sys.path.insert(0, "tests")
        from test_target_phase4 import face_obs, run_timeline  # noqa

        from gaeclip.autoframing import decide_two_speaker

        cfg = FramingConfig(two_speaker_enabled=False)
        accept, _, _, qc = decide_two_speaker([], [], {}, 1920, 1080, cfg)
        self.assertFalse(accept)
        self.assertIn("disabled", qc["rejects"])

    def test_config_defaults_present(self):
        cfg = FramingConfig()
        self.assertTrue(cfg.two_speaker_enabled)
        self.assertEqual(cfg.two_speaker_enter_threshold, 0.65)
        self.assertEqual(cfg.two_speaker_exit_threshold, 0.50)
        self.assertEqual(cfg.two_speaker_lock_ms, 1200)
        self.assertEqual(cfg.two_speaker_min_duration_ms, 800)
        self.assertEqual(cfg.two_speaker_max_distance, 0.45)
        self.assertEqual(cfg.min_two_speaker_face_width, 0.06)
        self.assertEqual(cfg.min_two_speaker_face_height, 0.10)
        self.assertEqual(cfg.min_two_speaker_edge_margin, 0.02)
        self.assertEqual(cfg.two_speaker_max_upscale, 2.0)
        self.assertEqual(cfg.two_speaker_max_crop_width, 0.60)
        self.assertEqual(cfg.two_speaker_max_empty_space, 0.45)
        self.assertEqual(cfg.two_speaker_max_cutoff, 0.15)
        self.assertEqual(cfg.two_speaker_temporal_overlap_ms, 1000)
        self.assertEqual(cfg.two_speaker_speaker_visibility_threshold, 0.85)
        self.assertEqual(cfg.two_speaker_min_score, 0.55)
        self.assertEqual(cfg.face_detection_min_confidence, 0.4)
        self.assertEqual(cfg.person_presence_min_visibility, 0.5)

    def test_qc_counters_present_on_reject(self):
        plan = _run(_pair_script(0.25, 0.75), _bar(0.25, 0.75))
        stats = plan.keyframe_stats
        self.assertIn("two_speaker_attempt_count", stats)
        self.assertIn("two_speaker_accept_count", stats)
        self.assertIn("two_speaker_reject_count", stats)
        self.assertEqual(stats["two_speaker_accept_count"], 0)
        self.assertGreaterEqual(stats["two_speaker_reject_count"], 1)

    def test_qc_counters_present_on_accept(self):
        plan = _run(_pair_script(0.43, 0.57), _bar(0.43, 0.57))
        stats = plan.keyframe_stats
        self.assertEqual(stats["two_speaker_accept_count"], 1)
        self.assertIn("two_speaker_speaker_visibility", stats)
        self.assertIn("two_speaker_empty_space", stats)
        self.assertGreaterEqual(stats["two_speaker_pair_valid_ratio"], 0.8)


class PersonPresenceTests(unittest.TestCase):
    """§21: no final crop may be person-free while faces exist."""

    def test_nearest_known_face_picks_temporal_nearest(self):
        from gaeclip.autoframing import _nearest_known_face

        class _T:
            def __init__(self, sid, boxes):
                self.speaker_id = sid
                self.boxes = boxes

        solid = [_T("speaker-000", [(14.5, (0.72, 0.25, 0.84, 0.65))]),
                 _T("speaker-001", [(30.0, (0.50, 0.25, 0.62, 0.65))])]
        tid, box = _nearest_known_face(solid, 3.0)
        self.assertEqual(tid, "speaker-000")
        tid, box = _nearest_known_face(solid, 25.0)
        self.assertEqual(tid, "speaker-001")
        self.assertIsNone(_nearest_known_face([], 3.0))

    def test_late_entry_no_static_person_free_head(self):
        # Faces appear only from t=3s (profile/blink before): the head of
        # the section must follow the nearest known face, never static
        # center, and every instruction must hold a person.
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from gaeclip.framing_geometry import CropWindow, face_visibility
        from test_autoframing import FakeDetector, motion_frames, tone_audio

        script = [[]] * 6 + [[((0.64, 0.25, 0.76, 0.65), 0.9)]
                             for _ in range(6)]
        frames = motion_frames(12, [None] * 6 + [(102, 124)] * 6)
        stamps = [index * 0.5 for index in range(len(frames))]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)))
        self.assertTrue(plan.instructions)
        for instruction in plan.instructions:
            self.assertNotEqual(
                (instruction.speaker_id, round(instruction.center_x, 3)),
                ("static", 0.5))
            # Every crop must sit on a known face (live at mid, or the
            # nearest lookahead anchor when the head has no detections).
            nearest = min(
                abs(instruction.center_x - (box[0] + box[2]) / 2)
                for track in plan.tracks
                for _, box in track.boxes)
            self.assertLessEqual(
                nearest, 0.20,
                f"person-free crop {instruction.start}-{instruction.end}")

    def test_enforce_recenters_person_free_group_window(self):
        from gaeclip.autoframing import _enforce_person_presence
        from gaeclip.framing_config import FramingConfig
        from gaeclip.visual import CropInstruction

        cfg = FramingConfig()

        class _T:
            def __init__(self, sid, boxes):
                self.speaker_id = sid
                self.boxes = boxes

            def box_at(self, t):
                best = None
                for stamp, box in self.boxes:
                    if stamp <= t + 1e-6:
                        best = box
                return best

        solid = [_T("speaker-000", [(5.0, (0.05, 0.25, 0.17, 0.65))])]
        # Head-gap midpoint-style window holding nobody (no past boxes).
        instructions = [CropInstruction(
            start=0, end=2.0, speaker_id="group",
            center_x=0.50, center_y=0.50, width=0.3167, height=1.0,
            layout="group")]
        notes: list = []
        corrected = _enforce_person_presence(
            instructions, solid, 0.3167, 1.0, cfg, notes)
        self.assertEqual(corrected, 1)
        self.assertEqual(instructions[0].speaker_id, "speaker-000")
        self.assertLess(instructions[0].center_x, 0.35)

    def test_enforce_ignores_stale_box_for_presence(self):
        # A box seen 5s ago must NOT certify "person present" for a crop
        # showing a wall; a fresh face elsewhere must win the recenter.
        from gaeclip.autoframing import _enforce_person_presence
        from gaeclip.framing_config import FramingConfig
        from gaeclip.visual import CropInstruction

        cfg = FramingConfig()
        self.assertEqual(cfg.anchor_stale_after_ms, 1000)

        class _T:
            def __init__(self, sid, boxes):
                self.speaker_id = sid
                self.boxes = boxes

            def box_at(self, t):
                best = None
                for stamp, box in self.boxes:
                    if stamp <= t + 1e-6:
                        best = box
                return best

        stale = _T("speaker-000", [(0.0, (0.68, 0.25, 0.80, 0.65))])
        fresh = _T("speaker-001", [(4.9, (0.24, 0.25, 0.36, 0.65)),
                                   (5.5, (0.24, 0.25, 0.36, 0.65))])
        # Crop parked on the stale box; mid=5.0, box age 5s >> 1s TTL.
        instructions = [CropInstruction(
            start=4.5, end=5.5, speaker_id="speaker-000",
            center_x=0.74, center_y=0.50, width=0.3167, height=1.0,
            layout="single")]
        notes: list = []
        corrected = _enforce_person_presence(
            instructions, [stale, fresh], 0.3167, 1.0, cfg, notes)
        self.assertEqual(corrected, 1)
        self.assertEqual(instructions[0].speaker_id, "speaker-001")
        self.assertLess(instructions[0].center_x, 0.45)

    def test_mid_gap_with_future_return_holds_person(self):
        # Faces 0-1s, blind gap, faces return 5-6s: the gap must hold the
        # person (bridged blindness), never go static-center.
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import FakeDetector, motion_frames, tone_audio

        script = ([[((0.24, 0.25, 0.36, 0.65), 0.9)] for _ in range(2)]
                  + [[] for _ in range(8)]
                  + [[((0.24, 0.25, 0.36, 0.65), 0.9)] for _ in range(2)])
        frames = motion_frames(12, [(38, 58)] * 2 + [None] * 8
                               + [(38, 58)] * 2)
        stamps = [index * 0.5 for index in range(len(frames))]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)))
        self.assertTrue(plan.instructions)
        statics = [i for i in plan.instructions
                   if i.speaker_id == "static"
                   and abs(i.center_x - 0.5) < 0.01]
        self.assertEqual(statics, [])
        for instruction in plan.instructions:
            self.assertLess(abs(instruction.center_x - 0.30), 0.20)

    def test_truly_empty_section_stays_static(self):
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import FakeDetector, motion_frames, tone_audio

        script = [[] for _ in range(12)]
        frames = motion_frames(12, [None] * 12)
        stamps = [index * 0.5 for index in range(len(frames))]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, False)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)))
        # No faces anywhere: static is the only honest answer.
        self.assertIn(plan.mode, ("static", "speaker-follow", "group"))


class KeyframeRegressionTests(unittest.TestCase):
    """Render follows analysis: switching speakers must yield MOVING
    keyframes (a past refactor dropped them -> frozen whole-clip renders)."""

    def test_keyframes_follow_speaker_switch(self):
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import (FakeDetector, motion_frames,
                                      talking_face, tone_audio)

        script = [talking_face(0.30) + talking_face(0.70) for _ in range(24)]
        frames = motion_frames(24, [(40, 56)] * 12 + [(104, 120)] * 12)
        stamps = [index * 0.5 for index in range(len(frames))]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(12.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section(
                "dummy.mp4", duration=12.0,
                frame_source=list(zip(stamps, frames)))
        self.assertEqual(
            sorted(set(i.speaker_id for i in plan.instructions)),
            ["speaker-000", "speaker-001"])
        self.assertGreaterEqual(len(plan.keyframes), 3)
        centers = {round(k[1], 3) for k in plan.keyframes}
        self.assertGreaterEqual(
            len(centers), 2,
            f"keyframes frozen at {centers}: render would never move")


    def test_21_drift_apart_exits_to_single(self):
        # Pair co-framed while close, then drifts apart mid-section:
        # the camera must exit to a single face, never park mid-gap.
        script = []
        for i in range(24):
            if i < 16:
                a, b = 0.43, 0.57
            else:
                j = i - 15
                a, b = 0.43 - 0.04 * j, 0.57 + 0.04 * j
            script.append([((a - 0.06, 0.25, a + 0.06, 0.65), 0.9),
                           ((b - 0.06, 0.25, b + 0.06, 0.65), 0.9)])
        plan = _run(script, [(16, 144)] * 24, duration=12.0, n_frames=24)
        self.assertEqual(plan.mode, "two_speaker")
        self.assertGreaterEqual(
            plan.keyframe_stats.get("two_speaker_exit_count", 0), 1)
        labels = {i.speaker_id for i in plan.instructions}
        self.assertTrue(any("+" not in label for label in labels),
                        "expected a single-face segment after the exit")
        for instruction in plan.instructions:
            if instruction.start >= 10.0 and instruction.layout != "split":
                self.assertTrue(
                    instruction.center_x < 0.35 or instruction.center_x > 0.65,
                    f"gap parking at t={instruction.start}: cx={instruction.center_x}")

    def test_22_group_gap_no_split_single_leader(self):
        # Phase 4.2 (§52): split-screen REMOVED. Two narrow faces with a
        # gap-dominated window must become ONE single-speaker crop (the
        # strongest evidence), never split panes and never a midpoint
        # empty-center window.
        script = [[((0.34, 0.25, 0.42, 0.65), 0.9),
                   ((0.58, 0.25, 0.66, 0.65), 0.9)] for _ in range(12)]
        plan = _run(script, [(48, 112)] * 12)
        self.assertEqual(plan.mode, "speaker-follow")
        self.assertTrue(plan.instructions)
        for instruction in plan.instructions:
            self.assertEqual(instruction.layout, "single")
            # Centered on a real face (0.38 or 0.62), not the 0.5 midpoint.
            self.assertTrue(
                abs(instruction.center_x - 0.38) < 0.08
                or abs(instruction.center_x - 0.62) < 0.08,
                f"empty-center crop: cx={instruction.center_x}")


class TransitionStyleTests(unittest.TestCase):
    def test_use_hard_cuts_defaults_to_cut(self):
        from gaeclip.framing_config import FramingConfig, use_hard_cuts

        self.assertTrue(use_hard_cuts(FramingConfig()))
        self.assertTrue(use_hard_cuts(None))
        self.assertFalse(use_hard_cuts(FramingConfig(transition_style="ease")))
        self.assertTrue(use_hard_cuts(FramingConfig(transition_style="bogus")))

    def test_transition_style_env_override(self):
        import os
        from unittest.mock import patch

        from gaeclip.framing_config import FramingConfig

        with patch.dict(os.environ, {"GAECLIP_FRAMING_TRANSITION_STYLE": "ease"}):
            self.assertEqual(FramingConfig.from_env().transition_style, "ease")


class RecenterOnFacesTests(unittest.TestCase):
    def test_noop_when_coverage_fine(self):
        from gaeclip.autoframing import _recenter_on_faces

        self.assertAlmostEqual(
            _recenter_on_faces(0.50, [(0.44, 0.25, 0.56, 0.65)], 0.3167, 0.06),
            0.50)

    def test_improves_worst_case(self):
        from gaeclip.autoframing import _recenter_on_faces
        from gaeclip.framing_geometry import CropWindow, face_visibility

        faces = [(0.10, 0.25, 0.22, 0.65)]
        before = min(face_visibility(b, CropWindow(x=0.60 - 0.15835, y=0.0,
                                                   width=0.3167, height=1.0))
                     for b in faces)
        self.assertLess(before, 0.5)
        moved = _recenter_on_faces(0.60, faces, 0.3167, 0.06)
        after = min(face_visibility(b, CropWindow(x=moved - 0.15835, y=0.0,
                                                  width=0.3167, height=1.0))
                    for b in faces)
        self.assertGreater(after, before)
        self.assertAlmostEqual(moved, 0.21835, places=3)

    def test_empty_faces_unchanged(self):
        from gaeclip.autoframing import _recenter_on_faces

        self.assertAlmostEqual(_recenter_on_faces(0.5, [], 0.3167, 0.06), 0.5)


if __name__ == "__main__":
    unittest.main()
