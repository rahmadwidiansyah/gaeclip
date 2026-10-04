"""Phase 5 tests: VAD methods, mask post-processing, wiring."""

import unittest

from gaeclip.autoframing import smooth_vad_mask, vad_energy_curve, vad_score_at
from gaeclip.framing_config import FramingConfig


def synth_audio(levels, sr=16000, window_sec=0.1):
    """Deterministic PCM from per-window RMS levels (no randomness)."""
    import numpy as np

    win = int(sr * window_sec)
    out = np.zeros(len(levels) * win, dtype=np.float32)
    for i, level in enumerate(levels):
        out[i * win:(i + 1) * win] = level
    return out


class MixedLevelTests(unittest.TestCase):
    def setUp(self):
        # Silence floor, quiet speech, loud burst, quiet speech again.
        self.levels = [0.001] * 10 + [0.05] * 10 + [0.9] * 5 + [0.05] * 10 + [0.001] * 10
        self.audio = synth_audio(self.levels)

    def test_peak_method_misses_quiet_speech(self):
        _, active = vad_energy_curve(self.audio, method="peak")
        # Documents the known limitation: peak*0.15 = 0.135 > quiet 0.05.
        self.assertEqual(sum(active[10:20]), 0.0)
        self.assertEqual(sum(active[20:25]), 5.0)

    def test_noise_floor_method_catches_quiet_speech(self):
        _, active = vad_energy_curve(self.audio, method="noise_floor")
        self.assertEqual(sum(active[10:20]), 10.0)  # quiet speech found
        self.assertEqual(sum(active[20:25]), 5.0)   # loud burst found
        self.assertEqual(sum(active[0:10]), 0.0)    # silence stays silent
        self.assertEqual(sum(active[35:45]), 0.0)

    def test_peak_legacy_behavior_preserved(self):
        # Constant tone: both methods agree (no behavior drift on easy input).
        audio = synth_audio([0.5] * 20)
        _, peak_active = vad_energy_curve(audio, method="peak")
        _, floor_active = vad_energy_curve(audio, method="noise_floor")
        self.assertEqual(peak_active, floor_active)
        self.assertEqual(sum(peak_active), 20.0)

    def test_silence_short_circuits_both(self):
        audio = synth_audio([0.0001] * 10)
        for method in ("peak", "noise_floor"):
            _, active = vad_energy_curve(audio, method=method)
            self.assertEqual(sum(active), 0.0)

    def test_empty_audio(self):
        import numpy as np

        for method in ("peak", "noise_floor"):
            energy, active = vad_energy_curve(np.zeros(0, dtype=np.float32), method=method)
            self.assertEqual((energy, active), ([], []))


class MaskSmoothingTests(unittest.TestCase):
    def test_hangover_bridges_short_gap(self):
        mask = [1.0] * 5 + [0.0] + [1.0] * 5
        out = smooth_vad_mask(mask, window_sec=0.1, hangover_ms=200, min_speech_ms=150)
        self.assertEqual(out, [1.0] * 11)

    def test_min_speech_kills_blip(self):
        mask = [0.0] * 5 + [1.0] + [0.0] * 5
        out = smooth_vad_mask(mask, window_sec=0.1, hangover_ms=200, min_speech_ms=150)
        self.assertEqual(out, [0.0] * 11)

    def test_sustained_speech_survives(self):
        mask = [1.0] * 10
        out = smooth_vad_mask(mask, window_sec=0.1, hangover_ms=200, min_speech_ms=150)
        self.assertEqual(out, mask)

    def test_idempotent_on_clean_masks(self):
        self.assertEqual(smooth_vad_mask([0.0] * 8, min_speech_ms=150), [0.0] * 8)
        self.assertEqual(smooth_vad_mask([], min_speech_ms=150), [])

    def test_hangover_bounded(self):
        # 5-window silence after 2 speech windows: only 2 bridged (200ms).
        mask = [1.0] * 2 + [0.0] * 5
        out = smooth_vad_mask(mask, window_sec=0.1, hangover_ms=200, min_speech_ms=150)
        self.assertEqual(out, [1.0] * 4 + [0.0] * 3)

    def test_deterministic(self):
        mask = [1.0, 1.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 1.0]
        first = smooth_vad_mask(mask)
        for _ in range(5):
            self.assertEqual(smooth_vad_mask(mask), first)


class WiringTests(unittest.TestCase):
    def test_config_defaults_preserve_legacy(self):
        cfg = FramingConfig()
        self.assertEqual(cfg.vad_method, "peak")
        self.assertEqual(cfg.vad_hangover_ms, 200)
        self.assertEqual(cfg.vad_min_speech_ms, 150)

    def test_noise_floor_end_to_end_single_speaker(self):
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import FakeDetector, motion_frames, talking_face

        from gaeclip.framing_config import FramingConfig

        script = [talking_face(0.3) for _ in range(12)]
        frames = motion_frames(12, [(30, 70)] * 12)
        stamps = [index * 0.5 for index in range(12)]
        # Quiet tone: peak method would struggle, noise_floor must hold it.
        quiet = synth_audio([0.05] * 60)
        cfg = FramingConfig(vad_method="noise_floor")
        with patch.object(auto, "extract_audio_mono", return_value=quiet), \
            patch.object(auto, "BlazeFaceDetector", return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)),
                                        config=cfg)
        self.assertEqual(plan.mode, "speaker-follow")
        self.assertTrue(plan.instructions)

    def test_vad_score_uses_smoothed_mask(self):
        # Smoothed mask feeds vad_score_at identically (same indexing).
        mask = [1.0] * 5 + [0.0] + [1.0] * 4
        smoothed = smooth_vad_mask(mask)
        self.assertAlmostEqual(vad_score_at([], smoothed, 0.0, 1.0), 1.0)


if __name__ == "__main__":
    unittest.main()
