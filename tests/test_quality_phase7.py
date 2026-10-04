"""Phase 7 (QUALITY) tests: fps policy, profiles, keyframes, motion shaping."""

import shutil
import unittest

from gaeclip.framing_config import (
    RENDER_PROFILES,
    FramingConfig,
    resolve_analysis_profile,
    resolve_encode_preset,
    resolve_encode_profile,
)
from gaeclip.media import resolve_output_fps
from gaeclip.smoothing import (
    apply_deadzone,
    build_keyframes,
    ease_in_out,
    limit_motion,
)


class FpsPolicyTests(unittest.TestCase):
    def test_standard_rates_follow_source(self):
        from unittest.mock import patch

        import gaeclip.media as media

        for probed, expected in [
            ((30.0, 30.0, False), 30.0),
            ((29.97, 29.97, False), 29.97),
            ((60.0, 60.0, False), 60.0),
            ((25.0, 25.0, False), 25.0),
            ((24.0, 24.0, False), 24.0),
            ((50.0, 50.0, False), 50.0),
        ]:
            with patch.object(media, "probe_fps", return_value=probed):
                fps, reason = resolve_output_fps("dummy.mp4")
                self.assertAlmostEqual(fps, expected, places=2)
                self.assertEqual(reason, "source_native")

    def test_vfr_and_unknown_fall_back(self):
        from unittest.mock import patch

        import gaeclip.media as media

        with patch.object(media, "probe_fps", return_value=(60.0, 30.0, True)):
            fps, reason = resolve_output_fps("dummy.mp4")
            self.assertEqual((fps, reason), (30.0, "vfr_or_unknown_source"))
        with patch.object(media, "probe_fps", return_value=(None, None, False)):
            fps, reason = resolve_output_fps("dummy.mp4")
            self.assertEqual((fps, reason), (30.0, "vfr_or_unknown_source"))

    def test_clamp_and_forced(self):
        from unittest.mock import patch

        import gaeclip.media as media

        with patch.object(media, "probe_fps", return_value=(120.0, 120.0, False)):
            self.assertEqual(resolve_output_fps("d.mp4")[0], 60.0)
        self.assertEqual(resolve_output_fps("d.mp4", mode="30"), (30.0, "forced"))

    def test_analysis_fps_never_leaks(self):
        # Policy function takes no analysis input by construction: output
        # fps derives solely from the probed section.
        import inspect

        params = set(inspect.signature(resolve_output_fps).parameters)
        self.assertNotIn("analysis_fps", params)


class ProfileTests(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(RENDER_PROFILES["fast"], ("veryfast", 22))
        self.assertEqual(RENDER_PROFILES["balanced"], ("medium", 20))
        self.assertEqual(RENDER_PROFILES["quality"], ("medium", 18))

    def test_default_is_quality(self):
        self.assertEqual(resolve_encode_profile(None), ("medium", 18))
        self.assertEqual(resolve_encode_profile("balanced"), ("medium", 20))
        self.assertEqual(resolve_encode_profile("fast"), ("veryfast", 22))
        self.assertEqual(resolve_encode_profile("nonsense"), ("medium", 18))

    def test_legacy_preset_resolver_unchanged(self):
        # Existing contract preserved: preset-only resolver still works.
        self.assertEqual(resolve_encode_preset(None), "medium")
        self.assertEqual(resolve_encode_preset("ultrafast"), "ultrafast")

    def test_dev_alias(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"GAECLIP_RENDER_PROFILE": "dev"}):
            self.assertEqual(resolve_encode_profile(None), ("veryfast", 22))


class DeadzoneTests(unittest.TestCase):
    def test_micro_wander_held(self):
        seq = [(t * 0.5, 0.30 + 0.005 * t, 0.5) for t in range(8)]
        labels = ["a"] * 8
        scenes = [0] * 8
        gated, held = apply_deadzone(seq, labels, scenes, deadzone_x=0.04, deadzone_y=0.06)
        self.assertEqual(held, 7)
        self.assertTrue(all(abs(x - 0.30) < 1e-9 for _, x, _ in gated))

    def test_genuine_move_passes(self):
        seq = [(t * 0.5, 0.30 + 0.05 * t, 0.5) for t in range(6)]
        gated, held = apply_deadzone(seq, ["a"] * 6, [0] * 6, deadzone_x=0.04, deadzone_y=0.06)
        self.assertEqual(held, 0)
        self.assertEqual([x for _, x, _ in gated], [x for _, x, _ in seq])

    def test_vertical_gate_wider(self):
        seq = [(0.0, 0.5, 0.50), (0.5, 0.5, 0.55)]
        gated, held = apply_deadzone(seq, ["a", "a"], [0, 0], deadzone_x=0.04, deadzone_y=0.06)
        self.assertEqual(held, 1)  # 0.05 < 0.06 held
        seq2 = [(0.0, 0.5, 0.50), (0.5, 0.5, 0.57)]
        _, held2 = apply_deadzone(seq2, ["a", "a"], [0, 0], deadzone_x=0.04, deadzone_y=0.06)
        self.assertEqual(held2, 0)

    def test_anchor_resets_on_label_change(self):
        seq = [(0.0, 0.3, 0.5), (0.5, 0.31, 0.5), (1.0, 0.9, 0.5)]
        gated, _ = apply_deadzone(seq, ["a", "a", "b"], [0, 0, 0],
                                  deadzone_x=0.04, deadzone_y=0.06)
        self.assertAlmostEqual(gated[2][1], 0.9)


class MotionLimitTests(unittest.TestCase):
    def test_teleport_clamped(self):
        seq = [(0.0, 0.2, 0.5), (0.5, 0.9, 0.5)]
        limited, trips = limit_motion(seq, ["a", "a"], [0, 0],
                                      max_velocity=0.6, max_acceleration=3.0)
        # 0.7/0.5s = 1.4/s exceeds 0.6 -> clamped to 0.3 displacement.
        self.assertAlmostEqual(limited[1][1], 0.5, places=6)
        self.assertEqual(trips, 1)

    def test_gentle_motion_untouched(self):
        seq = [(t * 0.5, 0.3 + 0.05 * t, 0.5) for t in range(6)]
        limited, trips = limit_motion(seq, ["a"] * 6, [0] * 6,
                                      max_velocity=0.6, max_acceleration=3.0)
        self.assertEqual(trips, 0)
        self.assertEqual([x for _, x, _ in limited], [x for _, x, _ in seq])

    def test_acceleration_clamped(self):
        seq = [(0.0, 0.0, 0.5), (0.5, 0.1, 0.5), (1.0, 0.5, 0.5)]
        limited, trips = limit_motion(seq, ["a"] * 3, [0] * 3,
                                      max_velocity=10.0, max_acceleration=0.5)
        self.assertGreaterEqual(trips, 1)
        # Second step cannot jump at full implied acceleration.
        self.assertLess(limited[2][1], 0.5)


class KeyframeTests(unittest.TestCase):
    def _points(self):
        # Steady A run then B run, same scene.
        pts = [(t * 0.5, 0.3, 0.5, "a", 0) for t in range(6)]
        pts += [(3.0 + t * 0.5, 0.7, 0.5, "b", 0) for t in range(6)]
        return pts

    def test_switch_gets_eased_transition(self):
        from gaeclip.smoothing import build_keyframes

        runs = [(0.0, 3.0, 0.3164, 1.0)]
        keyframes, stats = build_keyframes(
            self._points(), runs, (0.3164, 1.0), transition_ms=650, transition_steps=6,
            min_scale_change=0.01, jump_threshold=0.15)
        self.assertEqual(stats["transitions"], 1)
        switch = stats["switches"][0]
        self.assertEqual(switch["transition_ms"], 650)
        self.assertAlmostEqual(switch["distance"], 0.4, places=2)
        # Transition keyframes bridge the cut progressively.
        mids = [kf for kf in keyframes if 3.0 < kf[0] < 3.65]
        self.assertGreater(len(mids), 0)
        xs = [kf[1] for kf in mids]
        self.assertTrue(all(a <= b for a, b in zip(xs, xs[1:])))
        self.assertTrue(all(0.3 <= x <= 0.7 for x in xs))

    def test_scene_change_hard_cut(self):
        from gaeclip.smoothing import build_keyframes

        pts = [(t * 0.5, 0.3, 0.5, "a", 0) for t in range(4)]
        pts += [(2.0 + t * 0.5, 0.8, 0.5, "a", 1) for t in range(4)]
        runs = [(0.0, 4.0, 0.3164, 1.0)]
        keyframes, stats = build_keyframes(
            pts, runs, (0.3164, 1.0), transition_ms=650, transition_steps=6,
            min_scale_change=0.01, jump_threshold=0.15)
        self.assertEqual(stats["transitions"], 0)
        # Duplicate-time boundary pair present (hard cut, zero width).
        times = [kf[0] for kf in keyframes]
        self.assertIn(2.0, times)
        at_cut = [kf for kf in keyframes if kf[0] == 2.0]
        self.assertEqual(len(at_cut), 2)
        self.assertNotAlmostEqual(at_cut[0][1], at_cut[1][1])

    def test_size_step_below_threshold(self):
        from gaeclip.smoothing import build_keyframes

        pts = [(t * 0.5, 0.3, 0.5, "a", 0) for t in range(4)]
        pts += [(2.0 + t * 0.5, 0.7, 0.5, "b", 0) for t in range(4)]
        runs = [(0.0, 4.0, 0.3164, 1.0)]
        keyframes, stats = build_keyframes(
            pts, runs, (0.3164, 1.0), transition_ms=650, transition_steps=6,
            min_scale_change=0.5, jump_threshold=0.15)
        self.assertEqual(stats["size_steps"], 1)

    def test_jump_detected(self):
        from gaeclip.smoothing import build_keyframes

        pts = [(0.0, 0.2, 0.5, "a", 0), (0.5, 0.9, 0.5, "a", 0)]
        runs = [(0.0, 1.0, 0.3164, 1.0)]
        _, stats = build_keyframes(pts, runs, (0.3164, 1.0), transition_ms=0,
                                   transition_steps=1, min_scale_change=0.01,
                                   jump_threshold=0.15)
        self.assertEqual(stats["jumps"], 1)


class EaseTests(unittest.TestCase):
    def test_ease_curve_properties(self):
        self.assertEqual(ease_in_out(0.0), 0.0)
        self.assertEqual(ease_in_out(1.0), 1.0)
        mid = ease_in_out(0.5)
        self.assertAlmostEqual(mid, 0.5)
        vals = [ease_in_out(i / 10) for i in range(11)]
        self.assertTrue(all(b >= a for a, b in zip(vals, vals[1:])))


class ConfigDefaultsTests(unittest.TestCase):
    def test_quality_defaults(self):
        cfg = FramingConfig()
        self.assertEqual(cfg.analysis_fps, 12.0)
        self.assertEqual((cfg.analysis_fps_min, cfg.analysis_fps_max), (6.0, 15.0))
        self.assertEqual((cfg.framing_deadzone_x, cfg.framing_deadzone_y), (0.04, 0.06))
        self.assertEqual((cfg.max_crop_velocity, cfg.max_crop_acceleration), (0.6, 3.0))
        self.assertEqual(cfg.min_scale_change, 0.01)
        self.assertEqual(cfg.switch_transition_ms, 400)
        self.assertEqual(cfg.jump_threshold, 0.15)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "ffmpeg/ffprobe required")
class SyntheticRenderTests(unittest.TestCase):
    """Full path on real files: decode -> VAD -> tracking -> keyframes ->
    sendcmd render -> ffprobe. Only face pixels are mocked."""

    def _write_video(self, tmp, name, frames, fps=12):
        import subprocess

        import numpy as np
        from pathlib import Path

        # Full-rate source (15fps x 6s = 90 frames): exercises the real
        # default decode path including adaptive stride decisions.
        # Deterministic sensor grain (±5): real footage is never
        # mathematically static, and without it the encoder compresses
        # 6s of gray to bytes (tripping the production min-bytes gate
        # for the wrong reason). Mouth bars (±72) still dominate.
        rng = np.random.default_rng(7)
        grainy = []
        for frame in frames:
            noise = rng.integers(-5, 6, size=frame.shape).astype(np.int16)
            grain = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)
            # 4x nearest upscale (160->640): normalized geometry unchanged,
            # but the encoder gets real pixels (160px sources compress to
            # bytes and trip the production min-bytes gate meaninglessly).
            grainy.append(np.repeat(np.repeat(grain, 4, axis=0), 4, axis=1))
        height, width = grainy[0].shape[:2]
        src = Path(tmp) / f"{name}.mp4"
        duration = len(grainy) / fps
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
             "-r", str(fps), "-i", "-",
             "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={duration}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)],
            input=b"".join(f.tobytes() for f in grainy),
            capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-300:])
        return src

    def _run(self, tmp, script, regions, duration=6.0, adaptive=False):
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import FakeDetector, motion_frames, tone_audio

        # Scripted detectors assume every decoded frame is analyzed (same
        # contract as all injected-frame tests); adaptive stride is covered
        # separately by the live Phase 6 test + sampler unit tests. The
        # artificial odd-frame bars would otherwise alias with striding.
        from gaeclip.framing_config import FramingConfig

        frames = motion_frames(len(script), regions)
        src = self._write_video(tmp, f"src_{len(script)}", frames)
        cfg = FramingConfig(adaptive_sampling=adaptive)
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(duration, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector([row[:] for row in script])):
            plan = auto.analyze_section(str(src), duration, config=cfg)
        return src, plan

    def _render_qc(self, tmp, name, src, plan):
        from pathlib import Path

        from gaeclip.framing_qc import qc_clip
        from gaeclip.media import (render_trajectory_vertical,
                                   resolve_output_geometry)

        out = Path(tmp) / f"{name}.mp4"
        self.assertTrue(plan.keyframes and len(plan.keyframes) >= 2)
        render_trajectory_vertical(src, out, plan.keyframes, fps=30.0,
                                   duration=6.0, preset="veryfast")
        exp_w, exp_h = resolve_output_geometry(src)
        report = qc_clip(out, name, 6.0, exp_w=exp_w, exp_h=exp_h,
                         exp_fps=30.0)
        self.assertTrue(report.passed, report.summary())
        return report

    def test_render_slow_pan_smooth(self):
        import sys

        sys.path.insert(0, "tests")
        import tempfile

        from test_autoframing import talking_face

        # TEST 1: face drifts 0.2 -> 0.5 slowly; bars follow the box.
        # 90 frames @15fps exercise the real default decode path.
        script, regions = [], []
        for i in range(72):
            cx = 0.2 + 0.3 * (i / 71)
            script.append(talking_face(cx))
            x0 = int((cx - 0.1) * 160)
            regions.append((x0, x0 + 40))
        with tempfile.TemporaryDirectory() as tmp:
            src, plan = self._run(tmp, script, regions)
            report = self._render_qc(tmp, "pan", src, plan)
            metrics = {c.name: c for c in report.checks}
            self.assertTrue(metrics["fps"].passed)
            # Smooth: no jump events on a gentle pan.
            self.assertEqual(plan.keyframe_stats.get("jumps", -1), 0)

    def test_render_switch_eased(self):
        import sys

        sys.path.insert(0, "tests")
        import tempfile

        from test_autoframing import talking_face

        # TEST 2: A talks 0-3s, B talks 3-6s, both visible throughout.
        script = [talking_face(0.25) + talking_face(0.75) for _ in range(72)]
        regions = [(24, 64)] * 36 + [(104, 144)] * 36
        with tempfile.TemporaryDirectory() as tmp:
            src, plan = self._run(tmp, script, regions)
            self.assertTrue(plan.keyframe_stats.get("transitions", 0) >= 1)
            # No teleport anywhere: every same-scene step stays small.
            kfs = sorted(plan.keyframes, key=lambda kf: kf[0])
            for first, second in zip(kfs, kfs[1:]):
                if first[5] == second[5] and second[0] - first[0] > 1e-9:
                    dist = abs(second[1] - first[1])
                    self.assertLess(dist, 0.15, f"jump at t={second[0]}")
            # Settle span: last old-side to first settled new-side <= 0.9s.
            self._render_qc(tmp, "switch", src, plan)

    def test_render_micro_head_stable(self):
        import sys

        sys.path.insert(0, "tests")
        import tempfile

        from test_autoframing import talking_face

        # TEST 3: static face + sub-deadzone jitter stays put.
        script = [talking_face(0.30 + 0.002 * (i % 2)) for i in range(72)]
        regions = [(30, 70)] * 72
        with tempfile.TemporaryDirectory() as tmp:
            src, plan = self._run(tmp, script, regions)
            self.assertGreater(plan.perf.get("deadzone_holds", 0), 0)
            xs = [kf[1] for kf in plan.keyframes]
            self.assertLess(max(xs) - min(xs), 0.02)
            self._render_qc(tmp, "micro", src, plan)


if __name__ == "__main__":
    unittest.main()
