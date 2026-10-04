"""Phase 3 tests: Kalman trajectory filter, easing, trajectory output."""

import unittest

from gaeclip.smoothing import (
    KalmanFilter,
    apply_deadzone,
    build_keyframes,
    ease_in_out,
    filter_run,
    split_contiguous,
    subdivide_eased_runs,
    trajectory_to_json,
)


class KalmanTests(unittest.TestCase):
    def test_constant_position_no_drift(self):
        kf = KalmanFilter(process_noise=1e-3, measurement_noise=2e-2)
        last = None
        for _ in range(10):
            last = kf.step(0.5, (0.3, 0.5, 1.0))
        self.assertAlmostEqual(last[0], 0.3, places=3)
        self.assertAlmostEqual(last[1], 0.5, places=3)
        self.assertAlmostEqual(last[3], 0.0, places=2)  # vx settled
        self.assertAlmostEqual(last[4], 0.0, places=2)

    def test_velocity_tracking_beats_laggy_ema(self):
        true = [0.2 + 0.075 * i for i in range(17)]
        kf = KalmanFilter(process_noise=1e-3, measurement_noise=2e-2)
        kal = [kf.step(0.5, (z, 0.5, 1.0))[0] for z in true]
        lag_kal = sum(abs(a - b) for a, b in zip(kal, true)) / len(true)
        ema, s = [], true[0]
        for z in true:
            s = s + 0.2 * (z - s)
            ema.append(s)
        lag_ema = sum(abs(a - b) for a, b in zip(ema, true)) / len(true)
        self.assertLess(lag_kal, lag_ema / 3.0)

    def test_step_no_overshoot(self):
        # Realistic stop: ramp then constant. Overshoot must stay small
        # (no floaty sail-past) while settling onto the stopped target.
        seq = [0.2 + 0.1 * i for i in range(7)] + [0.8] * 7
        kf = KalmanFilter(process_noise=1e-3, measurement_noise=2e-2,
                          velocity_decay=0.7)
        outs = [kf.step(0.5, (z, 0.5, 1.0))[0] for z in seq]
        self.assertLessEqual(max(outs), 0.8 + 0.03)
        self.assertAlmostEqual(outs[-1], 0.8, delta=0.02)

    def test_coast_through_missing_measurement(self):
        kf = KalmanFilter(process_noise=1e-3, measurement_noise=2e-2)
        for i in range(6):
            kf.step(0.5, (0.2 + 0.1 * i, 0.5, 1.0))
        before = kf.step(0.5, None)
        # Coasting continues along learned velocity (positive direction).
        self.assertGreater(before[0], 0.6)
        self.assertGreater(before[3], 0.0)

    def test_reset_starts_fresh(self):
        kf = KalmanFilter()
        for _ in range(5):
            kf.step(0.5, (0.9, 0.5, 1.0))
        kf.reset((0.1, 0.2, 1.0))
        out = kf.step(0.5, (0.1, 0.2, 1.0))
        self.assertAlmostEqual(out[0], 0.1, places=3)
        self.assertAlmostEqual(out[3], 0.0, places=6)


class EasingTests(unittest.TestCase):
    def test_endpoints_and_symmetry(self):
        self.assertEqual(ease_in_out(0.0), 0.0)
        self.assertEqual(ease_in_out(1.0), 1.0)
        self.assertAlmostEqual(ease_in_out(0.25) + ease_in_out(0.75), 1.0)
        values = [ease_in_out(i / 20) for i in range(21)]
        self.assertTrue(all(b >= a for a, b in zip(values, values[1:])))

    def test_subdivide_eases_only_flagged_onsets(self):
        merged = [(0.0, 2.0, "a", 0.3, 0.5),
                  (2.0, 4.0, "b", 0.7, 0.5),   # switch: hard cut, untouched
                  (4.0, 6.0, "b", 0.5, 0.5)]   # fallback acquisition: eased
        ref = {round(t * 0.5, 3): (0.5, 0.5) for t in range(13)}
        runs, spans = subdivide_eased_runs(merged, {4.0}, ref,
                                           easing_s=0.6, waypoint_step=0.25)
        # First two runs byte-identical (switch preserved as a cut).
        self.assertEqual(runs[0], merged[0])
        self.assertEqual(runs[1], merged[1])
        eased = [r for r in runs if r[0] >= 4.0 - 1e-9]
        self.assertGreater(len(eased), 1)
        times = [r[0] for r in eased] + [eased[-1][1]]
        self.assertTrue(all(b > a for a, b in zip(times, times[1:])))
        # Converges exactly onto the run target, never overshoots past it.
        self.assertAlmostEqual(eased[-1][3], 0.5, places=3)
        xs = [r[3] for r in eased]
        self.assertTrue(all(0.68 <= x <= 0.72 or 0.5 <= x <= 0.7 for x in xs),
                        f"eased x out of [prev, target] corridor: {xs}")
        self.assertEqual(len(spans), 1)
        self.assertAlmostEqual(spans[0][0], 4.0)

    def test_eased_run_never_reads_next_run_curve(self):
        # Regression: the final eased waypoint once stretched to the run's
        # END, so its reference lookup landed on the NEXT run's curve and
        # corrupted the handover (static 0.53 instead of 0.5). Waypoints
        # must stay inside [start, start+span]; the remainder keeps the
        # run's own constant.
        merged = [(0.0, 2.0, "a", 0.3, 0.5),
                  (2.0, 4.0, "static", 0.5, 0.5),
                  (4.0, 6.0, "b", 0.7, 0.5)]
        ref = {round(t * 0.5, 3): (0.5, 0.5) for t in range(5, 9)}
        ref.update({round(t * 0.5, 3): (0.7, 0.5) for t in range(8, 13)})
        runs, spans = subdivide_eased_runs(merged, {2.0}, ref,
                                           easing_s=0.6, waypoint_step=0.25)
        statics = [r for r in runs if r[2] == "static"]
        # Ease span is [2.0, 2.6]; everything after stays exactly 0.5.
        self.assertGreater(len(statics), 1)
        for start, end, _, cx, _ in statics:
            if start >= 2.6 - 1e-9:
                self.assertAlmostEqual(cx, 0.5, places=3)
        # B run untouched (its own onset is a timeline switch, not marked).
        b_runs = [r for r in runs if r[2] == "b"]
        self.assertEqual(len(b_runs), 1)
        self.assertAlmostEqual(b_runs[0][3], 0.7, places=3)

    def test_subdivide_skips_first_run_and_micro_moves(self):
        merged = [(0.0, 2.0, "a", 0.3, 0.5)]
        runs, spans = subdivide_eased_runs(merged, {0.0}, {0.0: (0.3, 0.5)})
        self.assertEqual(runs, merged)
        self.assertEqual(spans, [])
        merged2 = [(0.0, 1.0, "a", 0.3, 0.5), (1.0, 3.0, "a", 0.305, 0.5)]
        runs2, spans2 = subdivide_eased_runs(merged2, {1.0}, {1.0: (0.305, 0.5)})
        self.assertEqual(runs2, merged2)  # 0.005 < min_distance: no spam
        self.assertEqual(spans2, [])


class DeadzoneExitEaseTests(unittest.TestCase):
    def test_legacy_snap_is_default(self):
        seq = [(0.0, 0.30, 0.5), (0.5, 0.30, 0.5), (1.0, 0.45, 0.5)]
        gated, held = apply_deadzone(seq, ["a"] * 3, [0] * 3,
                                     deadzone_x=0.04, deadzone_y=0.06)
        self.assertEqual(held, 1)
        self.assertAlmostEqual(gated[2][1], 0.45)

    def test_eased_exit_removes_pop(self):
        seq = [(0.0, 0.30, 0.5), (0.5, 0.30, 0.5), (1.0, 0.45, 0.5),
               (1.5, 0.46, 0.5), (2.0, 0.46, 0.5)]
        gated, held = apply_deadzone(seq, ["a"] * 5, [0] * 5,
                                     deadzone_x=0.04, deadzone_y=0.06,
                                     exit_ease_s=1.0)
        self.assertEqual(held, 1)
        # First exit sample still at the anchor (progress 0)...
        self.assertAlmostEqual(gated[2][1], 0.30)
        # ...then converges monotonically to the live target, no snap.
        xs = [x for _, x, _ in gated]
        self.assertTrue(all(b >= a for a, b in zip(xs, xs[1:])))
        self.assertAlmostEqual(xs[-1], 0.46)
        import math
        steps = [abs(b - a) for a, b in zip(xs, xs[1:])]
        self.assertLess(max(steps), 0.15 - 0.06)

    def test_ease_cancels_when_target_returns(self):
        seq = [(0.0, 0.30, 0.5), (1.0, 0.45, 0.5), (1.5, 0.31, 0.5)]
        gated, held = apply_deadzone(seq, ["a"] * 3, [0] * 3,
                                     deadzone_x=0.04, deadzone_y=0.06,
                                     exit_ease_s=1.0)
        self.assertAlmostEqual(gated[2][1], 0.30)
        self.assertEqual(held, 1)


class TrajectoryStructureTests(unittest.TestCase):
    def test_split_contiguous(self):
        samples = [(0.0, "a", 0.1, 0.5), (0.5, "a", 0.2, 0.5),
                   (1.0, "b", 0.7, 0.5), (1.5, "b", 0.7, 0.5),
                   (2.0, "a", 0.2, 0.5)]
        scenes = [0, 0, 0, 1, 1]
        runs = split_contiguous(samples, scenes)
        # Speaker change AND scene change both split (last two differ
        # in speaker despite sharing a scene).
        self.assertEqual(len(runs), 4)
        self.assertEqual([len(r) for r, _ in runs], [2, 1, 1, 1])
        self.assertEqual([s for _, s in runs], [0, 0, 1, 1])

    def test_filter_run_shape(self):
        samples = [(t * 0.5, "a", 0.3, 0.5) for t in range(5)]
        points = filter_run(samples, scene_id=2)
        self.assertEqual(len(points), 5)
        self.assertEqual([p.timestamp for p in points], [s[0] for s in samples])
        self.assertTrue(all(p.track_id == "a" and p.scene_id == 2 for p in points))

    def test_trajectory_json(self):
        points = filter_run([(0.0, "a", 0.3, 0.5), (0.5, "a", 0.31, 0.5)])
        payload = trajectory_to_json(points)
        self.assertEqual(len(payload), 2)
        self.assertEqual(set(payload[0]),
                         {"timestamp", "x", "y", "scale", "vx", "vy",
                          "track_id", "scene_id", "eased"})


class AnalyzeLevelTests(unittest.TestCase):
    def test_switch_stays_hard_cut(self):
        # Timeline SWITCH cuts must NOT be eased: A-run and B-run stay
        # discontinuous (editorial standard). Only acquisitions ease.
        # Both faces stay visible throughout; only the SPEAKING moves
        # (A talks 0-2s, B talks 2-6s), so the handover is a true switch.
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import (FakeDetector, motion_frames,
                                      talking_face, tone_audio)

        script = [talking_face(0.25) + talking_face(0.75) for _ in range(12)]
        frames = motion_frames(12, [(24, 64)] * 4 + [(104, 144)] * 8)
        stamps = [index * 0.5 for index in range(12)]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)))
        by_speaker: dict[str, list] = {}
        for instruction in plan.instructions:
            by_speaker.setdefault(instruction.speaker_id, []).append(instruction)
        left = min(plan.tracks, key=lambda t: (t.center_at(1.0) or (1, 1))[0])
        right = max(plan.tracks, key=lambda t: (t.center_at(1.0) or (1, 1))[0])
        self.assertIn(left.speaker_id, by_speaker)
        self.assertIn(right.speaker_id, by_speaker)
        left_max = max(i.center_x for i in by_speaker[left.speaker_id])
        right_min = min(i.center_x for i in by_speaker[right.speaker_id])
        self.assertLess(left_max, 0.45)
        self.assertGreater(right_min, 0.55)
        # No waypoint bridges the cut: nothing lives strictly between.
        for instruction in plan.instructions:
            self.assertTrue(instruction.center_x <= 0.45 or instruction.center_x >= 0.55)

    def test_trajectory_emitted_with_eased_flags(self):
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from gaeclip.framing_config import FramingConfig
        from test_autoframing import FakeDetector, talking_face, motion_frames, tone_audio

        script = [talking_face(0.3) for _ in range(4)] + [[] for _ in range(8)]
        frames = motion_frames(12, [(30, 70)] * 12)
        stamps = [index * 0.5 for index in range(12)]
        # Ease-behavior test: pin transition_style="ease" explicitly since
        # the production default is now hard cuts.
        ease_cfg = FramingConfig(transition_style="ease")
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)),
                                        config=ease_cfg)
        self.assertTrue(plan.trajectory)
        times = [p.timestamp for p in plan.trajectory]
        self.assertTrue(all(b > a for a, b in zip(times, times[1:])))
        self.assertTrue(any(p.eased for p in plan.trajectory))
        from gaeclip.smoothing import trajectory_to_json

        payload = trajectory_to_json(plan.trajectory)
        self.assertEqual(len(payload), len(plan.trajectory))


class BuildKeyframesHardCutTests(unittest.TestCase):
    def _points(self):
        return [(0.0, 0.3, 0.5, "a", 0), (0.5, 0.3, 0.5, "a", 0),
                (1.0, 0.7, 0.5, "b", 0), (1.5, 0.7, 0.5, "b", 0)]

    def test_hard_cut_boundary_pair(self):
        keyframes, stats = build_keyframes(
            self._points(), [(0.0, 2.0, 0.3167, 1.0)], (0.3167, 1.0),
            transition_ms=400, transition_steps=8, hard_cut=True)
        self.assertEqual(stats["transitions"], 1)
        self.assertEqual(stats["switches"][0]["transition_ms"], 0)
        # Duplicate-time boundary pair, old side then new side.
        cuts = [k for k in keyframes if k[0] == 1.0]
        self.assertEqual(len(cuts), 2)
        self.assertAlmostEqual(cuts[0][1], 0.3)
        self.assertAlmostEqual(cuts[1][1], 0.7)
        # No eased substeps strictly inside the window.
        self.assertFalse(any(1.0 < k[0] < 1.4 for k in keyframes))

    def test_ease_path_unchanged_by_default(self):
        keyframes, stats = build_keyframes(
            self._points(), [(0.0, 2.0, 0.3167, 1.0)], (0.3167, 1.0),
            transition_ms=400, transition_steps=8, hard_cut=False)
        self.assertEqual(stats["transitions"], 1)
        self.assertEqual(stats["switches"][0]["transition_ms"], 400)
        mids = [k for k in keyframes if 1.0 < k[0] < 1.4]
        self.assertTrue(len(mids) > 0)


if __name__ == "__main__":
    unittest.main()
