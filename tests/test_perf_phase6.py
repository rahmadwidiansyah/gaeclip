"""Phase 6 tests: adaptive sampling, memory guard, perf modes."""

import os
import shutil
import unittest
from unittest.mock import patch

from gaeclip.framing_config import FramingConfig, resolve_analysis_profile
from gaeclip.framing_stream import AdaptiveSampler, rss_mb, thumb_diff


class SamplerTests(unittest.TestCase):
    def test_still_sequence_drops_to_stride_two(self):
        sampler = AdaptiveSampler(still_threshold=0.02, still_windows=4)
        decisions = [sampler.update(0.0) for _ in range(8)]
        # 4th still window engages stride 2 (phase 3 is odd -> skip).
        self.assertEqual(decisions, [True, True, True, False, True, False, True, False])
        self.assertEqual((sampler.analyzed, sampler.skipped), (5, 3))

    def test_motion_resumes_immediately(self):
        sampler = AdaptiveSampler(still_threshold=0.02, still_windows=2)
        sampler.update(0.0)
        sampler.update(0.0)
        sampler.update(0.0)  # still now (3 >= 2) but phase decides
        self.assertTrue(sampler.update(0.5))  # motion: analyzed at once
        self.assertEqual(sampler.stride, 1)

    def test_max_stride_one_never_skips(self):
        sampler = AdaptiveSampler(max_stride=1)
        self.assertTrue(all(sampler.update(0.0) for _ in range(10)))
        self.assertEqual(sampler.skipped, 0)

    def test_deterministic(self):
        scores = [0.0, 0.0, 0.0, 0.0, 0.0, 0.09, 0.0, 0.0, 0.0, 0.0]
        first = AdaptiveSampler()
        expected = [first.update(v) for v in scores]
        for _ in range(5):
            sampler = AdaptiveSampler()
            self.assertEqual([sampler.update(v) for v in scores], expected)

    def test_reset(self):
        sampler = AdaptiveSampler(still_windows=1)
        sampler.update(0.0)
        sampler.update(0.0)
        sampler.reset()
        self.assertEqual((sampler.stride, sampler.phase, sampler.still_count), (1, 0, 0))


class ThumbDiffTests(unittest.TestCase):
    def test_identical_is_zero(self):
        import numpy as np

        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        self.assertEqual(thumb_diff(frame, frame), 0.0)

    def test_black_white_is_one(self):
        import numpy as np

        black = np.zeros((64, 64, 3), dtype=np.uint8)
        white = np.full((64, 64, 3), 255, dtype=np.uint8)
        self.assertAlmostEqual(thumb_diff(black, white), 1.0, places=2)

    def test_none_and_mismatch_safe(self):
        import numpy as np

        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        self.assertEqual(thumb_diff(None, frame), 0.0)
        self.assertEqual(thumb_diff(frame, np.zeros((32, 32, 3), dtype=np.uint8)), 0.0)


class ConfigProfileTests(unittest.TestCase):
    def test_defaults(self):
        cfg = FramingConfig()
        self.assertTrue(cfg.adaptive_sampling)
        # QUALITY phase: denser analysis band (was 4.0/8.0).
        self.assertEqual((cfg.analysis_fps_min, cfg.analysis_fps_max), (6.0, 15.0))
        self.assertEqual(cfg.max_analysis_memory_mb, 4000.0)
        self.assertEqual(cfg.perf_mode, "quality")

    def test_resolve_quality(self):
        self.assertEqual(resolve_analysis_profile(None, None, FramingConfig()),
                         (15.0, 640, True))

    def test_resolve_dev(self):
        with patch.dict(os.environ, {"GAECLIP_FRAMING_PERF_MODE": "dev"}):
            self.assertEqual(resolve_analysis_profile(None, None, FramingConfig()),
                             (4.0, 480, False))

    def test_explicit_wins(self):
        self.assertEqual(resolve_analysis_profile(3.0, 320, FramingConfig()),
                         (3.0, 320, False))

    def test_rss_returns_float(self):
        self.assertIsInstance(rss_mb(), float)
        self.assertGreaterEqual(rss_mb(), -1.0)


class AnalyzeWiringTests(unittest.TestCase):
    def test_memory_guard_drops_fps(self):
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import FakeDetector, motion_frames, tone_audio

        from gaeclip.framing_config import FramingConfig

        script = [[((0.15, 0.2, 0.35, 0.7), 0.9)] for _ in range(6)]
        frames = motion_frames(6, [None] * 6)
        stamps = [index * 0.5 for index in range(6)]
        cfg = FramingConfig(max_analysis_memory_mb=0.001)  # always over budget
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(3.0, False)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=3.0,
                                        frame_source=list(zip(stamps, frames)),
                                        config=cfg)
        self.assertTrue(plan.perf["memory_guard"])
        self.assertEqual(plan.perf["analysis_fps"], cfg.analysis_fps_min)

    def test_injected_frames_bypass_sampler(self):
        # Determinism contract: injected harnesses analyze every frame.
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import FakeDetector, motion_frames, tone_audio

        script = [[((0.15, 0.2, 0.35, 0.7), 0.9)] for _ in range(6)]
        frames = motion_frames(6, [None] * 6)
        stamps = [index * 0.5 for index in range(6)]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(3.0, False)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=3.0,
                                        frame_source=list(zip(stamps, frames)))
        self.assertEqual(plan.perf["frames_analyzed"], 6)
        self.assertEqual(plan.perf["frames_skipped"], 0)

    def test_dev_perf_mode_profile(self):
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import FakeDetector, motion_frames, tone_audio

        script = [[((0.15, 0.2, 0.35, 0.7), 0.9)] for _ in range(6)]
        frames = motion_frames(6, [None] * 6)
        stamps = [index * 0.5 for index in range(6)]
        with patch.dict(os.environ, {"GAECLIP_FRAMING_PERF_MODE": "dev"}), \
            patch.object(auto, "extract_audio_mono",
                         return_value=tone_audio(3.0, False)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=3.0,
                                        frame_source=list(zip(stamps, frames)))
        self.assertEqual(plan.perf["analysis_fps"], 4.0)
        self.assertEqual(plan.perf["analysis_max_width"], 480)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class LiveAdaptiveTests(unittest.TestCase):
    def test_static_video_skips_detector_runs(self):
        import subprocess
        import tempfile
        from pathlib import Path

        import gaeclip.autoframing as auto

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "static.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-v", "error", "-y",
                 "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=6",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                 "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-shortest", str(src)],
                check=True, timeout=120,
            )
            plan = auto.analyze_section(str(src), 6.0)
        self.assertEqual(plan.perf["frames_decoded"], 90)  # 6s @ 15fps decode
        self.assertGreater(plan.perf["frames_skipped"], 0)
        self.assertEqual(plan.perf["frames_analyzed"] + plan.perf["frames_skipped"],
                         plan.perf["frames_decoded"])
        self.assertEqual(plan.mode, "static")


if __name__ == "__main__":
    unittest.main()
