"""Phase 4 tests: QC checks, debug artifacts, log-only pipeline hook."""

import json
import shutil
import unittest
from pathlib import Path

from gaeclip.framing_qc import (
    QCCheck,
    QCReport,
    check_av_sync,
    check_duration,
    check_output_geometry,
    dump_debug_artifacts,
    instruction_metrics,
    max_upscale_of,
    qc_clip,
    trajectory_metrics,
)
from gaeclip.visual import CropInstruction


def fake_info(width=1080, height=1920, vcodec="h264", acodec="aac",
              sar="1:1", dar="9:16", pix_fmt="yuv420p", duration="4.0"):
    return {
        "streams": [
            {"codec_type": "video", "codec_name": vcodec, "width": width,
             "height": height, "pix_fmt": pix_fmt,
             "sample_aspect_ratio": sar, "display_aspect_ratio": dar,
             "duration": duration},
            {"codec_type": "audio", "codec_name": acodec,
             "sample_rate": "44100", "duration": duration},
        ],
        "format": {"duration": duration},
    }


def instruction(start, end, speaker="a", cx=0.3, cy=0.5):
    return CropInstruction(start=start, end=end, speaker_id=speaker,
                           center_x=cx, center_y=cy,
                           width=0.3164, height=1.0, layout="single")


class GeometryCheckTests(unittest.TestCase):
    def test_valid_output_passes(self):
        checks = {c.name: c for c in check_output_geometry(fake_info(), 1080, 1920)}
        self.assertTrue(all(c.passed for c in checks.values()))

    def test_wrong_resolution_fails(self):
        checks = {c.name: c for c in check_output_geometry(fake_info(width=720), 1080, 1920)}
        self.assertFalse(checks["resolution"].passed)

    def test_anamorphic_sar_fails(self):
        checks = {c.name: c for c in check_output_geometry(fake_info(sar="4:3"), 1080, 1920)}
        self.assertFalse(checks["sar"].passed)

    def test_missing_audio_fails(self):
        info = fake_info()
        info["streams"] = [info["streams"][0]]
        checks = {c.name: c for c in check_output_geometry(info, 1080, 1920)}
        self.assertFalse(checks["audio_stream"].passed)


class DurationCheckTests(unittest.TestCase):
    def test_tight_tolerance(self):
        self.assertTrue(check_duration(4.1, 4.0).passed)
        self.assertFalse(check_duration(5.0, 4.0).passed)  # legacy ±3s would pass
        self.assertTrue(check_duration(100.0, 104.0).passed)  # 5% of 104

    def test_av_sync(self):
        self.assertTrue(check_av_sync(4.0, 4.2).passed)
        self.assertFalse(check_av_sync(4.0, 5.0).passed)
        self.assertTrue(check_av_sync(None, 4.0).passed)  # unknown: no false alarm


class TrajectoryMetricTests(unittest.TestCase):
    def test_instruction_metrics(self):
        items = [instruction(0.0, 2.0, "a"), instruction(2.0, 4.0, "b"),
                 instruction(4.0, 6.0, "static")]
        metrics = instruction_metrics(items, 6.0)
        self.assertEqual(metrics["instructions"], 3)
        self.assertEqual(metrics["switches"], 1)  # static transitions excluded
        self.assertEqual(metrics["static_segments"], 1)
        self.assertAlmostEqual(metrics["coverage"], 6.0)
        self.assertEqual(metrics["max_gap_s"], 0.0)

    def test_gap_overlap_detected(self):
        items = [instruction(0.0, 1.0, "a"), instruction(1.5, 3.0, "a"),
                 instruction(2.8, 4.0, "a")]
        metrics = instruction_metrics(items, 4.0)
        self.assertAlmostEqual(metrics["max_gap_s"], 0.5)
        self.assertAlmostEqual(metrics["max_overlap_s"], 0.2)

    def test_empty_instructions(self):
        metrics = instruction_metrics([], 4.0)
        self.assertEqual(metrics["instructions"], 0)
        self.assertEqual(metrics["coverage"], 0.0)

    def test_trajectory_points(self):
        from gaeclip.smoothing import TrajectoryPoint

        points = [TrajectoryPoint(timestamp=i * 0.5, x=0.3, y=0.5, scale=1.0,
                                  vx=0.01, vy=0.0, track_id="a") for i in range(4)]
        points[2].eased = True
        metrics = trajectory_metrics(points)
        self.assertEqual(metrics["trajectory_points"], 4)
        self.assertEqual(metrics["eased_points"], 1)
        self.assertGreater(metrics["mean_crop_velocity"], 0.0)

    def test_max_upscale(self):
        items = [instruction(0.0, 2.0)]
        self.assertAlmostEqual(max_upscale_of(items, 1920, 1080), 1080 / 608, places=2)
        self.assertIsNone(max_upscale_of([], 1920, 1080))


class ReportTests(unittest.TestCase):
    def test_summary_format(self):
        report = QCReport(clip_id="c1")
        report.checks.append(QCCheck("probe", True, ""))
        text = report.summary()
        for token in ("QC SUMMARY", "clip: c1", "result: PASS", "[ok] probe"):
            self.assertIn(token, text)

    def test_qc_clip_never_raises(self):
        report = qc_clip(Path("/nonexistent/x.mp4"), "c1", 4.0,
                         instructions=[instruction(0.0, 4.0)])
        self.assertFalse(report.passed)
        self.assertTrue(any(c.name == "probe" for c in report.checks))

    def test_dump_debug_artifacts(self):
        import tempfile
        from pathlib import Path

        from gaeclip.autoframing import FramingPlan
        from gaeclip.smoothing import TrajectoryPoint

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clip.mp4"
            out.write_bytes(b"\x00")
            plan = FramingPlan(
                instructions=[instruction(0.0, 4.0)], mode="speaker-follow",
                reason="test", scenes=[2.0],
                trajectory=[TrajectoryPoint(timestamp=0.5, x=0.3, y=0.5,
                                            scale=1.0, track_id="a", eased=True)],
            )
            written = dump_debug_artifacts(out, plan)
            self.assertEqual(len(written), 1)
            payload = json.loads(Path(written[0]).read_text())
            self.assertEqual(payload["mode"], "speaker-follow")
            self.assertEqual(payload["scenes"], [2.0])
            self.assertTrue(payload["trajectory"][0]["eased"])
            self.assertEqual(len(payload["instructions"]), 1)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "ffmpeg/ffprobe required")
class LiveQCTests(unittest.TestCase):
    def test_qc_clip_on_real_render(self):
        import subprocess
        import tempfile
        from pathlib import Path

        from gaeclip.media import render_static_vertical

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "src.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-v", "error", "-y",
                 "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=6",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                 "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-shortest", str(src)],
                check=True, timeout=120,
            )
            out = tmp_path / "clip.mp4"
            render_static_vertical(src, out, 0, 6, preset="veryfast")
            from gaeclip.media import resolve_output_geometry

            exp_w, exp_h = resolve_output_geometry(src)
            report = qc_clip(out, "live", 6.0, exp_w=exp_w, exp_h=exp_h,
                             instructions=[instruction(0.0, 6.0)])
            self.assertTrue(report.passed, report.summary())


class PipelineHookTests(unittest.TestCase):
    def test_hook_never_fails_job(self):
        from gaeclip.pipeline import _log_framing_qc

        # Missing file, no plan, debug on: must swallow everything.
        _log_framing_qc(Path("/nonexistent/x.mp4"),
                        "c1", "job1", 4.0, [], None, True)
        _log_framing_qc(Path("/nonexistent/x.mp4"),
                        "c1", "job1", 4.0, [], None, False)


if __name__ == "__main__":
    unittest.main()
