"""Phase 1 tests: geometry, trajectory, guard, streaming, config.

Pure-geometry tests run without ffmpeg. Render/ffprobe tests are skipped
when ffmpeg/ffprobe binaries are unavailable.
"""

import inspect
import math
import shutil
import unittest

from gaeclip import framing_geometry as geo
from gaeclip.framing_config import FramingConfig, resolve_encode_preset


class GeometryTests(unittest.TestCase):
    def test_1_landscape_1920x1080_valid_crop(self):
        crop_w, crop_h = geo.portrait_window_size(1920, 1080)
        self.assertEqual((crop_w, crop_h), (608, 1080))
        self.assertTrue(geo.aspect_matches(crop_w, crop_h, 1080, 1920))
        window = geo.portrait_crop_for_center(0.5, 0.5, 1920, 1080)
        self.assertTrue(window.is_valid())
        ok, reason, _ = geo.validate_crop(window, 1920, 1080)
        self.assertTrue(ok, reason)

    def test_2_landscape_1280x720_valid_crop(self):
        from gaeclip.media import compute_vertical_ladder

        crop_w, crop_h = geo.portrait_window_size(1280, 720)
        self.assertTrue(geo.aspect_matches(crop_w, crop_h, 1080, 1920))
        window = geo.portrait_crop_for_center(0.2, 0.5, 1280, 720)
        self.assertTrue(window.is_valid())
        # Guard validates against the ladder rung the renderer will use.
        _, _, _, _, rung_w, rung_h = compute_vertical_ladder(1280, 720)
        ok, reason, _ = geo.validate_crop(window, 1280, 720,
                                          out_w=rung_w, out_h=rung_h)
        self.assertTrue(ok, reason)

    def test_3_portrait_720x1280_no_distortion(self):
        crop_w, crop_h = geo.portrait_window_size(720, 1280)
        # Full-frame use: nothing meaningful to cut on a 9:16 source.
        self.assertEqual((crop_w, crop_h), (720, 1280))
        self.assertTrue(geo.aspect_matches(crop_w, crop_h, 1080, 1920))

    def test_8_split_pane_no_stretch(self):
        pane = geo.split_pane_crop(0.3, 1920, 1080, out_w=1080, pane_h=960)
        self.assertTrue(pane.is_valid())
        self.assertTrue(geo.aspect_matches(pane.width * 1920, pane.height * 1080,
                                           1080, 960),
                        f"pane aspect distorted: {pane}")
        # Speaker stays inside its pane horizontally.
        self.assertLessEqual(abs(pane.center[0] - 0.3), pane.width / 2 + 1e-9)

    def test_8_render_pane_filter_no_stretch(self):
        from gaeclip.media import _portrait_crop_filter
        from gaeclip.visual import CropInstruction

        item = CropInstruction(start=0, end=1, speaker_id="a", center_x=0.3,
                               center_y=0.5, width=0.3, height=0.5, layout="split")
        filt = _portrait_crop_filter(item, 1920, 1080, 1080, 1920, pane_h=960)
        crop_part, scale_part = filt.split(",scale=")
        dims = crop_part[len("crop="):].split(":")
        crop_w, crop_h = int(dims[0]), int(dims[1])
        out_w, out_h = (int(v) for v in scale_part.split(":flags")[0].split(":")[:2])
        self.assertAlmostEqual(crop_w / crop_h, out_w / out_h, delta=0.02)


class TrajectoryTests(unittest.TestCase):
    def test_4_moving_face_keeps_waypoints(self):
        from gaeclip.autoframing import merge_trajectory

        samples = [(t * 0.5, "a", 0.2 + 0.1 * t, 0.5) for t in range(9)]
        merged = merge_trajectory(samples, max_position_delta=0.03,
                                  max_time_gap=0.75, sample_step=0.5)
        self.assertGreater(len(merged), 1,
                           "walker collapsed to a single position")
        self.assertLess(merged[0][3], merged[-1][3])
        # Coverage: waypoints must span the motion, not just the endpoint.
        self.assertLess(merged[0][3], 0.4)
        self.assertGreater(merged[-1][3], 0.8)

    def test_5_stable_face_merges(self):
        from gaeclip.autoframing import merge_trajectory

        samples = [(t * 0.5, "a", 0.5 + 0.001 * t, 0.5) for t in range(9)]
        merged = merge_trajectory(samples, max_position_delta=0.03,
                                  max_time_gap=0.75, sample_step=0.5)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][0], 0.0)
        self.assertAlmostEqual(merged[0][1], 4.5)

    def test_scene_boundary_forces_split(self):
        from gaeclip.autoframing import merge_trajectory

        samples = [(t * 0.5, "a", 0.5, 0.5) for t in range(8)]
        scenes = [0] * 4 + [1] * 4
        merged = merge_trajectory(samples, scenes)
        self.assertEqual(len(merged), 2)
        self.assertAlmostEqual(merged[0][1], merged[1][0])

    def test_time_gap_forces_split(self):
        from gaeclip.autoframing import merge_trajectory

        samples = [(0.0, "a", 0.5, 0.5), (0.5, "a", 0.5, 0.5),
                   (3.0, "a", 0.5, 0.5)]
        merged = merge_trajectory(samples, max_time_gap=0.75, sample_step=0.5)
        self.assertEqual(len(merged), 2)


class GroupClampTests(unittest.TestCase):
    def test_6_two_faces_kept_visible(self):
        window, report = geo.safe_group_crop(
            [(0.35, 0.2, 0.45, 0.7), (0.47, 0.2, 0.57, 0.7)],
            src_w=1920, src_h=1080)
        self.assertTrue(window.is_valid())
        # Source-aware aspect: normalized ratio means nothing on its own;
        # the PIXEL window must match the output aspect (1920x1080 source).
        self.assertTrue(geo.aspect_matches(window.width * 1920,
                                           window.height * 1080, 1080, 1920))
        for value in report["visibility"]:
            self.assertGreaterEqual(value, 0.85)
        self.assertEqual(report["reason"], "union-fit")

    def test_6b_wide_pair_balanced_optimum(self):
        # Faces 0.5 apart cannot share a correct 9:16 window (their span
        # exceeds the maximal window). The honest result balances both
        # sides equally and reports safest-max-window; the analyze layer
        # turns this case into split panes instead of amputating.
        window, report = geo.safe_group_crop(
            [(0.1, 0.2, 0.3, 0.7), (0.6, 0.2, 0.8, 0.7)],
            src_w=1920, src_h=1080)
        self.assertTrue(window.is_valid())
        # Source-aware aspect: normalized ratio means nothing on its own;
        # the PIXEL window must match the output aspect (1920x1080 source).
        self.assertTrue(geo.aspect_matches(window.width * 1920,
                                           window.height * 1080, 1080, 1920))
        self.assertEqual(report["reason"], "safest-max-window")
        vis = report["visibility"]
        self.assertAlmostEqual(vis[0], vis[1], places=6)
        self.assertLess(abs(window.center[0] - 0.45), 0.05)

    def test_7_edge_union_no_amputation(self):
        # Union touches the left edge but the pair still fits: no amputation.
        faces = [(0.05, 0.2, 0.15, 0.7), (0.25, 0.2, 0.35, 0.7)]
        window, report = geo.safe_group_crop(faces, src_w=1920, src_h=1080)
        self.assertTrue(window.is_valid())
        self.assertGreaterEqual(window.x, 0.0)
        for value in report["visibility"]:
            self.assertGreaterEqual(value, 0.85)

    def test_7b_extreme_spread_degrades_gracefully(self):
        # Opposite extreme edges: no valid 9:16 window can keep both.
        # Must still return a valid window + honest reason, never garbage.
        faces = [(0.0, 0.1, 0.2, 0.9), (0.8, 0.1, 1.0, 0.9)]
        window, report = geo.safe_group_crop(faces)
        self.assertTrue(window.is_valid())
        # Source-aware aspect: normalized ratio means nothing on its own;
        # the PIXEL window must match the output aspect (1920x1080 source).
        self.assertTrue(geo.aspect_matches(window.width * 1920,
                                           window.height * 1080, 1080, 1920))
        self.assertEqual(report["reason"], "safest-max-window")


class QualityGuardTests(unittest.TestCase):
    def test_10_tiny_crop_rejected_for_upscale(self):
        # Aspect-correct but tiny: 96px wide on a 1920 source -> 11.25x.
        window = geo.CropWindow(x=0.4, y=0.1, width=0.05, height=0.158)
        ok, reason, suggestion = geo.validate_crop(window, 1920, 1080,
                                                   max_upscale=2.0)
        self.assertFalse(ok)
        self.assertIn("upscale", reason)
        self.assertEqual(suggestion, "widen")

    def test_10_out_of_bounds_rejected(self):
        window = geo.CropWindow(x=-0.1, y=0.0, width=0.3, height=0.5)
        ok, _, suggestion = geo.validate_crop(window, 1920, 1080)
        self.assertFalse(ok)
        self.assertEqual(suggestion, "fallback")

    def test_10_zero_size_rejected(self):
        window = geo.CropWindow(x=0.5, y=0.5, width=0.0, height=0.4)
        ok, _, suggestion = geo.validate_crop(window, 1920, 1080)
        self.assertFalse(ok)
        self.assertEqual(suggestion, "fallback")

    def test_10_face_outside_rejected(self):
        window = geo.CropWindow(x=0.0, y=0.0, width=0.3164, height=1.0)
        face = (0.8, 0.3, 0.95, 0.7)  # far right, outside the left crop
        ok, reason, _ = geo.validate_crop(window, 1920, 1080, faces=[face])
        self.assertFalse(ok)
        self.assertIn("face", reason)

    def test_10_good_crop_accepted(self):
        window = geo.portrait_crop_for_center(0.5, 0.5, 1920, 1080)
        ok, reason, _ = geo.validate_crop(
            window, 1920, 1080, faces=[(0.4, 0.3, 0.6, 0.7)])
        self.assertTrue(ok, reason)

    def test_10b_guard_uses_matching_rung_dims(self):
        # Live-test regression: the analyze wiring once passed rung WIDTH
        # with configured full HEIGHT (810x1920), rejecting every valid
        # 720p instruction. Guard callers must pass the rung pair.
        from gaeclip.autoframing import guard_instruction
        from gaeclip.media import compute_vertical_ladder
        from gaeclip.visual import CropInstruction

        _, _, _, _, rung_w, rung_h = compute_vertical_ladder(1280, 720)
        instruction = CropInstruction(
            start=0, end=2, speaker_id="a", center_x=0.5, center_y=0.5,
            width=406 / 1280, height=1.0, layout="single")
        keep, _ = guard_instruction(instruction, [(0.4, 0.3, 0.6, 0.7)],
                                    1280, 720, out_w=rung_w, out_h=rung_h)
        self.assertTrue(keep)
        # The old mismatched pairing must reject (proves the test is live).
        keep_bad, _ = guard_instruction(instruction, [(0.4, 0.3, 0.6, 0.7)],
                                        1280, 720, out_w=rung_w, out_h=1920)
        self.assertFalse(keep_bad)


class StreamConfigTests(unittest.TestCase):
    def test_analysis_size_uniform_no_upscale(self):
        from gaeclip.framing_stream import analysis_size_for

        self.assertEqual(analysis_size_for(1920, 1080, 640), (640, 360))
        self.assertEqual(analysis_size_for(320, 240, 640), (320, 240))
        wide, high = analysis_size_for(1920, 800, 640)
        self.assertEqual(wide, 640)
        self.assertAlmostEqual(high / wide, 800 / 1920, places=2)

    def test_11_iterator_is_lazy_generator(self):
        from gaeclip import framing_stream

        self.assertTrue(inspect.isgeneratorfunction(
            framing_stream.iter_analysis_frames))

    def test_config_defaults_and_env_override(self):
        import os
        from unittest.mock import patch

        cfg = FramingConfig()
        self.assertEqual(cfg.analysis_max_width, 640)
        # QUALITY phase: denser analysis anchors (was 6.0).
        self.assertEqual(cfg.analysis_fps, 12.0)
        self.assertTrue(cfg.scene_cut_enabled)
        self.assertTrue(cfg.quality_guard_enabled)
        with patch.dict(os.environ, {"GAECLIP_FRAMING_ANALYSIS_FPS": "4",
                                     "GAECLIP_RENDER_PROFILE": "dev"}):
            cfg2 = FramingConfig.from_env()
            self.assertEqual(cfg2.analysis_fps, 4.0)
        self.assertEqual(resolve_encode_preset(None), "medium")
        with patch.dict(os.environ, {"GAECLIP_RENDER_PROFILE": "dev"}):
            self.assertEqual(resolve_encode_preset(None), "veryfast")
        self.assertEqual(resolve_encode_preset("ultrafast"), "ultrafast")

    def test_9_scene_cut_detects_hard_cut(self):
        import numpy as np

        from gaeclip.framing_stream import SceneCutDetector

        detector = SceneCutDetector(threshold=0.35, min_interval_ms=1000)
        black = np.zeros((160, 160, 3), dtype=np.uint8)
        white = np.full((160, 160, 3), 255, dtype=np.uint8)
        self.assertEqual(detector.update(black, 0.0), (False, 0.0))
        self.assertFalse(detector.update(black, 0.5)[0])  # identical: no cut
        is_cut, score = detector.update(white, 1.0)
        self.assertTrue(is_cut)
        self.assertGreater(score, 0.35)
        # Minimum interval suppresses immediate re-trigger.
        self.assertFalse(detector.update(black, 1.2)[0])

    def test_9_pan_like_drift_is_not_a_cut(self):
        import numpy as np

        from gaeclip.framing_stream import SceneCutDetector

        rng = np.random.default_rng(7)
        base = rng.integers(0, 255, size=(160, 160, 3)).astype(np.uint8)
        drifted = np.clip(base.astype(int) + 8, 0, 255).astype(np.uint8)
        detector = SceneCutDetector(threshold=0.35, min_interval_ms=1000)
        detector.update(base, 0.0)
        self.assertFalse(detector.update(drifted, 0.5)[0])

    def test_9_defaults_catch_mid_contrast_cuts(self):
        # Production defaults (0.25/500ms) must catch angle changes that
        # the old 0.35/1000ms floor missed, without firing on drift.
        import numpy as np

        from gaeclip.framing_config import FramingConfig
        from gaeclip.framing_stream import SceneCutDetector

        cfg = FramingConfig()
        self.assertEqual(cfg.scene_cut_threshold, 0.25)
        self.assertEqual(cfg.scene_cut_min_interval_ms, 500)
        detector = SceneCutDetector(
            threshold=cfg.scene_cut_threshold,
            min_interval_ms=cfg.scene_cut_min_interval_ms)
        dark = np.full((160, 160, 3), 60, dtype=np.uint8)
        bright = np.full((160, 160, 3), 200, dtype=np.uint8)
        detector.update(dark, 0.0)
        # ~0.55 contrast step: a real cut, caught even in quick succession.
        self.assertTrue(detector.update(bright, 0.7)[0])
        self.assertTrue(detector.update(dark, 1.4)[0])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "ffmpeg/ffprobe required")
class RenderSampleTests(unittest.TestCase):
    """TEST 12/13 + synthetic sample + ffprobe (Phase 1.16-1.18)."""

    @classmethod
    def setUpClass(cls):
        import subprocess
        import tempfile
        from pathlib import Path

        cls.tmp = Path(tempfile.mkdtemp(prefix="gaeclip-p1-"))
        cls.source = cls.tmp / "source.mp4"
        # 4s 640x360 30fps + stereo-turned-mono sine audio.
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=4",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             "-shortest", str(cls.source)],
            check=True, timeout=120,
        )

    def _probe(self, path):
        import json
        import subprocess

        out = subprocess.run(
            ["ffprobe", "-hide_banner", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, check=True, timeout=60,
        )
        return json.loads(out.stdout.decode())

    def test_12_dynamic_render_filter_order_and_geometry(self):
        import subprocess

        from gaeclip.media import compute_vertical_ladder, render_dynamic_vertical
        from gaeclip.visual import CropInstruction

        _, _, _, _, rung_w, rung_h = compute_vertical_ladder(640, 360)
        out = self.tmp / "dynamic.mp4"
        render_dynamic_vertical(
            self.source, out,
            [CropInstruction(start=0, end=2, speaker_id="a", center_x=0.3,
                             center_y=0.5, width=0.3, height=0.5, layout="single"),
             CropInstruction(start=2, end=4, speaker_id="a", center_x=0.7,
                             center_y=0.5, width=0.3, height=0.5, layout="single")],
            subtitles=None, preset="veryfast",
        )
        info = self._probe(out)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        self.assertEqual((int(video["width"]), int(video["height"])), (rung_w, rung_h))
        self.assertEqual(video["pix_fmt"], "yuv420p")
        self.assertEqual(video.get("sample_aspect_ratio", "1:1"), "1:1")
        self.assertEqual(video["codec_name"], "h264")
        self.assertEqual(audio["codec_name"], "aac")
        # Duration tight check (Phase 1 rule, not the legacy ±3s).
        actual = float(info["format"]["duration"])
        self.assertLessEqual(abs(actual - 4.0), max(0.5, 4.0 * 0.05))
        # A/V sync: stream durations must agree.
        self.assertLessEqual(
            abs(float(video.get("duration", actual)) - float(audio.get("duration", actual))), 0.5)

    def test_13_static_render_with_subtitle_burn_last(self):
        from gaeclip.media import compute_vertical_ladder, render_static_vertical
        from gaeclip.schemas import TranscriptLine
        from gaeclip.subtitles import write_ass

        out_w, out_h = None, None
        from gaeclip.media import resolve_output_geometry

        out_w, out_h = resolve_output_geometry(self.source)
        lines = [TranscriptLine(line_id=f"l{i + 1:06d}", start=float(i),
                                end=float(i + 1), text=f"kata baris {i}",
                                language="id", source="youtube_auto")
                 for i in range(4)]
        ass = self.tmp / "clip.ass"
        write_ass(lines, ass, offset=0, clip_duration=4, max_chars=20,
                  width=out_w, height=out_h)
        content = ass.read_text(encoding="utf-8")
        self.assertIn(f"PlayResX: {out_w}", content)
        self.assertIn(f"PlayResY: {out_h}", content)
        out = self.tmp / "static.mp4"
        render_static_vertical(self.source, out, 0, 4, subtitles=ass,
                               preset="veryfast")
        info = self._probe(out)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((int(video["width"]), int(video["height"])), (out_w, out_h))
        # Filter order is structural in media.py: crop,scale,setsar,fps,subtitles
        # (fps value is now dynamic = source rate; only the stage order is pinned).
        import inspect

        import gaeclip.media as media

        src = inspect.getsource(media.render_static_vertical)
        order = [src.index(token) for token in
                 ("crop=", "scale=", "setsar", "fps=", "_subtitle_filter")]
        self.assertEqual(order, sorted(order))


if __name__ == "__main__":
    unittest.main()
