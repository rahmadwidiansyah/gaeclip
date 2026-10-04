import math
import shutil
import unittest
from unittest.mock import patch

import numpy as np

from gaeclip.autoframing import (
    IoUTracker,
    decide_framing,
    dump_active_speaker_log,
    iou,
    lip_score_from_motion,
    make_group_crop,
    mouth_motion_ratio,
    smooth_centers,
    vad_energy_curve,
    vad_score_at,
)


def solid_frame(value=128, width=160, height=160):
    return np.full((height, width, 3), value, dtype=np.uint8)


class IoUTests(unittest.TestCase):
    def test_iou_overlap(self):
        self.assertAlmostEqual(iou((0, 0, 1, 1), (0, 0, 1, 1)), 1.0)
        self.assertAlmostEqual(iou((0, 0, 1, 1), (1, 1, 2, 2)), 0.0)
        self.assertGreater(iou((0, 0, 2, 2), (1, 1, 3, 3)), 0.1)

    def test_tracker_keeps_stable_ids(self):
        tracker = IoUTracker()
        for step in range(6):
            shift = step * 0.01
            tracker.update(step * 0.5, [
                ((0.10 + shift, 0.2, 0.30 + shift, 0.7), 0.9),
                ((0.60 - shift, 0.2, 0.80 - shift, 0.7), 0.9),
            ])
        self.assertEqual(len(tracker.tracks), 2)
        ids = sorted(track.speaker_id for track in tracker.tracks)
        self.assertEqual(ids, ["speaker-000", "speaker-001"])
        for track in tracker.tracks:
            self.assertEqual(len(track.boxes), 6)

    def test_tracker_spawns_new_id_for_new_face(self):
        tracker = IoUTracker()
        tracker.update(0.0, [((0.1, 0.2, 0.3, 0.7), 0.9)])
        tracker.update(0.5, [((0.1, 0.2, 0.3, 0.7), 0.9), ((0.6, 0.2, 0.8, 0.7), 0.9)])
        self.assertEqual(len(tracker.tracks), 2)

    def test_tracker_reacquires_after_brief_absence(self):
        tracker = IoUTracker()
        box = (0.1, 0.2, 0.3, 0.7)
        tracker.update(0.0, [(box, 0.9)])
        tracker.update(0.5, [(box, 0.9)])
        for step in range(2, 8):
            tracker.update(step * 0.5, [])
        self.assertEqual(tracker.tracks, [])
        tracker.update(4.0, [(box, 0.9)])
        self.assertEqual([track.speaker_id for track in tracker.tracks], ["speaker-000"])

    def test_solid_tracks_filters_flicker(self):
        tracker = IoUTracker()
        box = (0.1, 0.2, 0.3, 0.7)
        for step in range(6):
            tracker.update(step * 0.5, [(box, 0.9)])
        tracker.update(3.0, [(box, 0.9), ((0.6, 0.2, 0.8, 0.7), 0.4)])
        solid = tracker.solid_tracks()
        self.assertEqual(len(solid), 1)
        self.assertEqual(solid[0].speaker_id, "speaker-000")


class VadTests(unittest.TestCase):
    def test_silence_vs_tone(self):
        sr = 16000
        silence = np.zeros(sr, dtype=np.float32)
        tone = (0.5 * np.sin(2 * math.pi * 440 * np.arange(sr) / sr)).astype(np.float32)
        audio = np.concatenate([silence, tone])
        energy, active = vad_energy_curve(audio, sample_rate=sr, window_sec=0.1)
        self.assertEqual(len(energy), 20)
        self.assertAlmostEqual(vad_score_at(energy, active, 0.0, 1.0), 0.0)
        self.assertGreater(vad_score_at(energy, active, 1.0, 2.0), 0.8)

    def test_empty_audio(self):
        energy, active = vad_energy_curve(np.zeros(0, dtype=np.float32))
        self.assertEqual((energy, active), ([], []))
        self.assertEqual(vad_score_at([], [], 0, 1), 0.0)


class MotionScoreTests(unittest.TestCase):
    def test_identical_frames_score_zero(self):
        frame = solid_frame()
        self.assertAlmostEqual(mouth_motion_ratio(frame, frame, (0, 0.2, 1, 0.8)), 0.0, places=3)
        self.assertEqual(lip_score_from_motion(1.0), 0.0)

    def test_moving_mouth_scores_high(self):
        prev = solid_frame()
        curr = solid_frame()
        curr[88:120, 40:120] = 200
        ratio = mouth_motion_ratio(prev, curr, (0.0, 0.2, 1.0, 0.8))
        self.assertGreater(ratio, 2.0)
        self.assertGreater(lip_score_from_motion(ratio), 0.5)

    def test_mapping_bounds(self):
        self.assertEqual(lip_score_from_motion(1.2), 0.0)
        self.assertEqual(lip_score_from_motion(50.0), 1.0)


class SmoothingTests(unittest.TestCase):
    def test_no_extreme_jumps(self):
        samples = [(t * 0.5, "a", 0.2 if t < 4 else 0.8, 0.5) for t in range(10)]
        smoothed = smooth_centers(samples)
        self.assertEqual(len(smoothed), len(samples))
        for (_, _, x1, y1), (_, _, x2, y2) in zip(smoothed, smoothed[1:]):
            self.assertLessEqual(abs(x2 - x1), 0.12 + 1e-9)
            self.assertLessEqual(abs(y2 - y1), 0.12 + 1e-9)
        # Converges toward the new target instead of sticking at the old one.
        self.assertGreater(smoothed[-1][2], 0.5)

    def test_empty(self):
        self.assertEqual(smooth_centers([]), [])


class DecisionTests(unittest.TestCase):
    def test_no_faces_static(self):
        mode, reason = decide_framing([], [], 30.0)
        self.assertEqual(mode, "static")
        self.assertIn("no faces", reason)

    def test_group_crop_geometry(self):
        crop = make_group_crop([(0.1, 0.2, 0.3, 0.7), (0.6, 0.2, 0.8, 0.7)])
        # Source-aware aspect (Phase 2 fix): the PIXEL window on the
        # default 1920x1080 source must match 9:16. The old assertion
        # (normalized height == width*16/9) encoded square-pixel thinking
        # and is wrong for non-square sources; the renderer never consumed
        # these fields, only the center, which is unchanged behavior.
        self.assertAlmostEqual(
            (crop.width * 1920) / (crop.height * 1080), 9 / 16, places=2)
        self.assertLessEqual(crop.center_x + crop.width / 2, 1.0)
        self.assertGreaterEqual(crop.center_x - crop.width / 2, 0.0)
        self.assertEqual(crop.layout, "group")

    def test_debug_log_format(self):
        from gaeclip.autoframing import Track
        track = Track(speaker_id="speaker-000", boxes=[(0.0, (0.1, 0.2, 0.3, 0.7))])
        log = dump_active_speaker_log(0.0, [track], [], None, None, (0.2, 0.0, 0.56, 1.0))
        for key in ("[ACTIVE SPEAKER]", "timestamp:", "persons_detected: 1", "bbox:",
                    "tracking_id: speaker-000", "speaking_score:", "selected_speaker:",
                    "confidence:", "target_crop:", "x:", "y:", "width:", "height:"):
            self.assertIn(key, log)


class FakeDetector:
    """Scripted detections: list per frame index of (box, conf, talking)."""

    def __init__(self, script):
        self.script = script
        self.calls = 0

    def detect(self, frame):
        detections = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return detections

    def close(self):
        pass


def talking_face(x_center):
    return [((x_center - 0.1, 0.2, x_center + 0.1, 0.7), 0.9)]


def motion_frames(n_frames, bar_regions):
    """Alternate solid frames with a bright mouth bar inside given x-ranges.

    bar_regions: list per frame of (x0, x1) or None. Motion is attributed
    only to faces overlapping the bar, so silent faces stay silent.
    """
    frames = []
    for index in range(n_frames):
        frame = solid_frame()
        region = bar_regions[index] if index < len(bar_regions) else None
        if index % 2 == 1 and region is not None:
            frame[88:120, region[0]:region[1]] = 200
        frames.append(frame)
    return frames


def tone_audio(duration, active=True):
    sr = 16000
    if active:
        return (0.5 * np.sin(2 * math.pi * 220 * np.arange(int(sr * duration)) / sr)).astype(np.float32)
    return np.zeros(int(sr * duration), dtype=np.float32)


def run_analysis(detector_script, frames, duration=6.0, audio_active=True):
    """Run analyze_section with fully mocked I/O (no ffmpeg, no mediapipe).

    Frames are injected via the frame_source seam (streaming-compatible);
    audio and detector remain mocked as before.
    """
    import gaeclip.autoframing as auto

    stamps = [index * 0.5 for index in range(len(frames))]
    detector = FakeDetector(detector_script)
    with patch.object(auto, "extract_audio_mono", return_value=tone_audio(duration, audio_active)), \
        patch.object(auto, "BlazeFaceDetector", return_value=detector):
        return auto.analyze_section("dummy.mp4", duration=duration,
                                    frame_source=list(zip(stamps, frames)))


class EndToEndTests(unittest.TestCase):
    def test_a_single_speaker_followed(self):
        script = [talking_face(0.3) for _ in range(12)]
        frames = motion_frames(12, [(30, 70)] * 12)
        plan = run_analysis(script, frames)
        # Single face must be followed (never static, never a random pick).
        self.assertEqual(plan.mode, "speaker-follow")
        self.assertTrue(plan.instructions)
        for instruction in plan.instructions:
            self.assertEqual(instruction.layout, "single")
            self.assertLess(abs(instruction.center_x - 0.3), 0.2)

    def test_b_speaker_switch_followed(self):
        script = [talking_face(0.25) for _ in range(6)] + [talking_face(0.75) for _ in range(6)]
        frames = motion_frames(12, [(24, 64)] * 6 + [(104, 144)] * 6)
        plan = run_analysis(script, frames)
        self.assertEqual(plan.mode, "speaker-follow")
        centers = [item.center_x for item in plan.instructions]
        self.assertLess(min(centers), 0.45)
        self.assertGreater(max(centers), 0.55)

    def test_c_silent_face_not_chosen(self):
        script = [talking_face(0.25) + talking_face(0.75) for _ in range(12)]
        frames = motion_frames(12, [(24, 64)] * 12)  # only the left mouth moves
        first = run_analysis([row[:] for row in script], [row.copy() for row in frames])
        second = run_analysis([row[:] for row in script], [row.copy() for row in frames])
        # Deterministic: same input -> same tracks and same followed speaker.
        self.assertEqual(
            [item.speaker_id for item in first.tracks],
            [item.speaker_id for item in second.tracks],
        )
        left_id = min(first.tracks, key=lambda t: (t.center_at(1.0) or (1, 1))[0]).speaker_id
        followed = {item.speaker_id for item in first.instructions}
        self.assertIn(left_id, followed)

    def test_d_rapid_switch_no_jitter(self):
        script = [talking_face(0.25) + talking_face(0.75) for _ in range(12)]
        # Bar alternates sides every frame: scores flip fast, hysteresis must hold.
        frames = motion_frames(12, [(24, 64) if i % 2 == 0 else (104, 144) for i in range(12)])
        plan = run_analysis(script, frames)
        # Hysteresis must prevent rapid A->B->A oscillation.
        # With alternating scores the timeline should produce at most 2 segments
        # (stay on one speaker, or make exactly one stable switch — never oscillate).
        # We allow up to 2 instructions because a single held switch is valid;
        # 3+ means hysteresis failed and the tracker is jittering.
        self.assertLessEqual(len(plan.instructions), 2,
                             f"Expected ≤2 segments (no jitter), got {len(plan.instructions)}: "
                             f"{[(i.speaker_id, round(i.start,2), round(i.end,2)) for i in plan.instructions]}")

    def test_e_low_confidence_falls_back(self):
        script = [[((0.15, 0.2, 0.35, 0.7), 0.05),
                   ((0.65, 0.2, 0.85, 0.7), 0.05)] for _ in range(12)]
        frames = motion_frames(12, [None] * 12)
        plan = run_analysis(script, frames, audio_active=False)
        # Phase 4.2 (§52): no split-screen. Two silent, far-apart,
        # low-confidence faces cannot form a group window without an
        # empty-center shot, so the fallback is a single stable face —
        # never a confident speech claim, never split panes.
        self.assertEqual(plan.mode, "speaker-follow")
        self.assertTrue(plan.instructions)
        for instruction in plan.instructions:
            self.assertEqual(instruction.layout, "single")
        self.assertTrue(plan.reason)

    def test_f_brief_lip_blip_no_switch(self):
        # Scenario C: A talks throughout; B's lips move for a single 0.5s
        # window only (< MIN_SPEAKER_HOLD 600ms of sustained evidence).
        script = [talking_face(0.25) + talking_face(0.75) for _ in range(12)]
        frames = motion_frames(
            12, [(24, 64)] * 12,  # A (left) always moves
        )
        # B (right) moves on exactly one odd frame (helper draws bars on
        # odd frames only): inject a bar there.
        extra = motion_frames(12, [None] * 5 + [(104, 144)] + [None] * 6)
        frames = [a if i != 5 else b for i, (a, b) in enumerate(zip(frames, extra))]
        plan = run_analysis(script, frames)
        speakers = {item.speaker_id for item in plan.instructions}
        left_id = min(plan.tracks, key=lambda t: (t.center_at(1.0) or (1, 1))[0]).speaker_id
        self.assertIn(left_id, speakers)
        self.assertLessEqual(len(plan.instructions), 2)

    def test_g_occlusion_hold(self):
        # Scenario D: A talks, vanishes for one 0.5s window, returns.
        # Grace must hold A: no jump to center, no new identity storm.
        script = ([talking_face(0.3)] * 4 + [[]] + [talking_face(0.3)] * 7)
        frames = motion_frames(12, [(30, 70)] * 12)
        plan = run_analysis(script, frames)
        self.assertEqual(plan.mode, "speaker-follow")
        speakers = {item.speaker_id for item in plan.instructions}
        self.assertEqual(len(speakers), 1)
        for instruction in plan.instructions:
            self.assertLess(abs(instruction.center_x - 0.3), 0.2)

    def test_h_prolonged_absence_falls_back_bounded(self):
        # Scenario E/G (Phase 2): A talks 2s then disappears for the rest.
        # Grace holds A briefly, then NO_TARGET -> static center fallback.
        # Ghost-hold must be BOUNDED (no indefinite freeze on empty scene)
        # and must not jump aggressively (no snap at disappearance).
        # Phase 3: the static onset eases in from the frozen position.
        # (Pinned to ease style: production default is now hard cuts.)
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from gaeclip.framing_config import FramingConfig

        ease_cfg = FramingConfig(transition_style="ease")
        script = [talking_face(0.3) for _ in range(4)] + [[] for _ in range(8)]
        frames = motion_frames(12, [(30, 70)] * 12)
        stamps = [index * 0.5 for index in range(len(frames))]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)),
                                        config=ease_cfg)
        by_speaker: dict[str, list] = {}
        for instruction in plan.instructions:
            by_speaker.setdefault(instruction.speaker_id, []).append(instruction)
        # A is held (no immediate jump), then a bounded static fallback.
        self.assertIn("speaker-000", by_speaker)
        for instruction in by_speaker["speaker-000"]:
            self.assertLess(abs(instruction.center_x - 0.3), 0.25)
        self.assertIn("static", by_speaker)
        statics = sorted(by_speaker["static"], key=lambda i: i.start)
        static_start = statics[0].start
        self.assertGreaterEqual(static_start, 2.0)  # held through grace, not instant
        # Eased onset: ramp from the frozen position up to center, then flat.
        centers = [i.center_x for i in statics]
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(centers, centers[1:])),
                        f"static onset must ease monotonically: {centers}")
        self.assertAlmostEqual(statics[-1].center_x, 0.5, delta=0.01)
        self.assertTrue(any(0.3 < c < 0.5 for c in centers),
                        "expected intermediate eased waypoints")
        # No third identity appears out of nowhere.
        self.assertEqual(set(by_speaker), {"speaker-000", "static"})

    def test_h_cut_mode_static_fallback_has_no_pan(self):
        # Same scenario under production cut default: the fallback still
        # lands on center, but with zero intermediate pan frames.
        script = [talking_face(0.3) for _ in range(4)] + [[] for _ in range(8)]
        frames = motion_frames(12, [(30, 70)] * 12)
        plan = run_analysis(script, frames)
        statics = sorted(
            (i for i in plan.instructions if i.speaker_id == "static"),
            key=lambda i: i.start)
        self.assertTrue(statics)
        self.assertAlmostEqual(statics[-1].center_x, 0.5, delta=0.01)
        self.assertFalse(any(p.eased for p in plan.trajectory))
        self.assertTrue(all(s["transition_ms"] == 0
                            for s in plan.keyframe_stats.get("switches", [])))


class LadderTests(unittest.TestCase):
    def test_full_hd_source(self):
        from gaeclip.media import compute_vertical_ladder
        crop_x, crop_y, crop_w, crop_h, out_w, out_h = compute_vertical_ladder(1920, 1080)
        self.assertEqual((crop_w, crop_h), (608, 1080))
        self.assertEqual((out_w, out_h), (1080, 1920))

    def test_small_source_no_excessive_upscale(self):
        from gaeclip.media import compute_vertical_ladder
        crop_x, crop_y, crop_w, crop_h, out_w, out_h = compute_vertical_ladder(640, 360)
        # Crop must not exceed source bounds, output must be exactly 9:16,
        # and all dimensions must be even.
        self.assertLessEqual(crop_w, 640)
        self.assertLessEqual(crop_h, 360)
        self.assertEqual(crop_w % 2, 0, "crop_w must be even")
        self.assertEqual(crop_h % 2, 0, "crop_h must be even")
        self.assertEqual(out_w % 2, 0, "out_w must be even")
        self.assertEqual(out_h % 2, 0, "out_h must be even")
        self.assertEqual(out_w * 16, out_h * 9, "output must be exactly 9:16")
        # No upscale beyond 2×.
        self.assertLessEqual(out_w / crop_w, 2.0 + 1e-9)

    def test_4k_source_caps_at_1080x1920(self):
        from gaeclip.media import compute_vertical_ladder
        _, _, _, _, out_w, out_h = compute_vertical_ladder(3840, 2160)
        self.assertEqual((out_w, out_h), (1080, 1920))

    def test_portrait_source_unchanged(self):
        from gaeclip.media import compute_vertical_ladder
        crop_x, crop_y, crop_w, crop_h, out_w, out_h = compute_vertical_ladder(1080, 1920)
        self.assertEqual((crop_w, crop_h, out_w, out_h), (1080, 1920, 1080, 1920))

    def test_crop_is_centered_and_even(self):
        from gaeclip.media import compute_vertical_ladder
        for src in ((1920, 1080), (1280, 720), (640, 360), (1080, 1920)):
            crop_x, crop_y, crop_w, crop_h, out_w, out_h = compute_vertical_ladder(*src)
            self.assertEqual(crop_x, (src[0] - crop_w) // 2)
            self.assertEqual(crop_y, (src[1] - crop_h) // 2)
            for value in (crop_w, crop_h, out_w, out_h):
                self.assertEqual(value % 2, 0, f"odd dimension {value} for {src}")
            self.assertAlmostEqual(out_w / out_h, 9 / 16, places=2)


class RenderFilterTests(unittest.TestCase):
    def test_static_crop_before_scale_high_quality(self):
        import gaeclip.media as media

        seen = {}

        def fake_run(args, timeout=600):
            seen["args"] = args

            class Done:
                stdout = ""
            return Done()

        with patch.object(media, "run_command", fake_run):
            with patch.object(media, "verify_clip", return_value={}):
                import tempfile
                from pathlib import Path
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "clip.mp4"
                    # Missing source: probe fails -> falls back to given size.
                    media.render_static_vertical(
                        Path(tmp) / "missing.mp4", out,
                        start=0, duration=2, width=1080, height=1920,
                    )
        args = seen["args"]
        vf = args[args.index("-vf") + 1]
        self.assertLess(vf.index("crop="), vf.index("scale="))
        self.assertIn("flags=lanczos", vf)
        self.assertIn("20", args[args.index("-crf") + 1])
        self.assertIn("medium", args[args.index("-preset") + 1])
        self.assertIn("128k", args[args.index("-b:a") + 1])


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class RenderExecutionTests(unittest.TestCase):
    """Real ffmpeg renders. Skipped where ffmpeg is unavailable
    (e.g. Windows CI before the third-party binaries step)."""
    @staticmethod
    def make_source(path, size="1280x720", duration=3):
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
             f"testsrc=size={size}:rate=30:duration={duration}",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
             "-vf", "noise=alls=25:allf=t",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-shortest", str(path)],
            check=True, timeout=120,
        )

    def test_f_static_hd_output_dims(self):
        import tempfile
        from pathlib import Path
        from gaeclip.media import probe, render_static_vertical
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "clip.mp4"
            self.make_source(src, "1920x1080")
            render_static_vertical(src, out, start=0, duration=2)
            info = probe(out)
            self.assertEqual((info.get("width"), info.get("height")), ("1080", "1920"))

    def test_f_small_source_stays_honest(self):
        import tempfile
        from pathlib import Path
        from gaeclip.media import probe, render_static_vertical
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "clip.mp4"
            self.make_source(src, "640x360", duration=8)
            render_static_vertical(src, out, start=0, duration=8)
            info = probe(out)
            w, h = int(info.get("width", 0)), int(info.get("height", 0))
            # Output must be exactly 9:16, all even, and no excessive upscale.
            self.assertEqual(w * 16, h * 9, f"output {w}x{h} is not 9:16")
            self.assertEqual(w % 2, 0, "output width must be even")
            self.assertEqual(h % 2, 0, "output height must be even")
            self.assertLessEqual(w, 640 * 2, "should not upscale more than 2x")

    def test_dynamic_render_executes(self):
        import tempfile
        from pathlib import Path
        from gaeclip.media import probe, render_dynamic_vertical
        from gaeclip.visual import CropInstruction
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "clip.mp4"
            self.make_source(src, "1280x720", duration=8)
            render_dynamic_vertical(
                src, out,
                [CropInstruction(start=0, end=2, speaker_id="speaker-000",
                                 center_x=0.3, center_y=0.5, width=9 / 16, height=1.0),
                 CropInstruction(start=2, end=8, speaker_id="speaker-001",
                                 center_x=0.7, center_y=0.5, width=9 / 16, height=1.0)],
            )
            info = probe(out)
            w, h = int(info.get("width", 0)), int(info.get("height", 0))
            # Output must be exactly 9:16 with correct geometry (no stretching).
            self.assertEqual(w * 16, h * 9, f"output {w}x{h} is not 9:16")
            self.assertEqual(w % 2, 0, "output width must be even")
            self.assertEqual(h % 2, 0, "output height must be even")

    def test_debug_overlay_executes(self):
        import tempfile
        from pathlib import Path
        from gaeclip.media import render_debug_overlay
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "debug.mp4"
            self.make_source(src, "640x360", duration=2)
            render_debug_overlay(
                src, out,
                faces=[(0.2, 0.2, 0.4, 0.7, True), (0.6, 0.2, 0.8, 0.7, False)],
                crops=[(0.2, 0.0, 0.56, 1.0)],
                width=404, height=720,
            )
            self.assertTrue(out.is_file())
            self.assertGreater(out.stat().st_size, 10_000)


if __name__ == "__main__":
    unittest.main()
