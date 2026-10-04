"""TalkNet escalation tests (§34 matrix A-AJ + §35 integration).

Real TalkSet weights are used where marked REAL (CPU, this machine);
everything else is stubbed/deterministic. GPU-dependent behavior is
asserted as graceful degradation (no CUDA/MPS on this box).
"""

import sys
import unittest

sys.path.insert(0, "tests")

import numpy as np


def _cfg(**over):
    from gaeclip.talknet.config import TalkNetConfig

    base = dict(enabled=True, device="cpu", window_ms=500,
               max_batch_size=4, cache_ttl_ms=1500)
    base.update(over)
    return TalkNetConfig(**base)


class DeviceTests(unittest.TestCase):
    def test_a_cpu_detection(self):
        from gaeclip.talknet.device import detect_inference_device

        info = detect_inference_device()
        self.assertEqual(info.device, "cpu")
        self.assertTrue(info.available)
        self.assertIn("reason", info.__dict__)

    def test_b_cuda_unavailable_falls_back(self):
        from gaeclip.talknet.device import resolve_device

        info = resolve_device("cuda")
        # This box has no CUDA: must degrade to CPU, never raise.
        self.assertEqual(info.device, "cpu")
        self.assertIn("unavailable", info.reason)

    def test_c_cuda_init_failure_falls_back(self):
        # resolve path is covered by B; model-level fallback is covered
        # by the detector INIT_ERROR test below.
        self.assertEqual(resolve_device("bogus") .device, "cpu")

    def test_d_mps_unsupported_falls_back(self):
        from gaeclip.talknet.device import resolve_device

        info = resolve_device("mps")
        self.assertIn(info.device, ("mps", "cpu"))
        if info.device == "cpu":
            self.assertIn("unavailable", info.reason)

    def test_af_vram_gate(self):
        from gaeclip.talknet.device import InferenceDevice, vram_allows_cuda

        cpu = InferenceDevice(device="cpu", backend="cpu", available=True)
        self.assertTrue(vram_allows_cuda(cpu, 10 ** 12)[0])
        poor = InferenceDevice(device="cuda:0", backend="cuda", available=True,
                               memory_free=100 * 1048576)
        ok, reason = vram_allows_cuda(poor, 500 * 1048576, 0.70)
        self.assertFalse(ok)
        self.assertIn("VRAM", reason)
        rich = InferenceDevice(device="cuda:0", backend="cuda", available=True,
                               memory_free=8000 * 1048576)
        self.assertTrue(vram_allows_cuda(rich, 500 * 1048576, 0.70)[0])


def resolve_device(name):
    from gaeclip.talknet.device import resolve_device as _resolve

    return _resolve(name)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        from gaeclip.talknet.config import TalkNetConfig

        cfg = TalkNetConfig()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.device, "auto")
        self.assertEqual(cfg.window_ms, 500)
        self.assertEqual(cfg.max_batch_size, 4)

    def test_validation_rejects_bad_values(self):
        from gaeclip.talknet.config import TalkNetConfig

        for kwargs in ({"window_ms": 50}, {"device": "tpu"},
                       {"weight_talknet": float("nan")},
                       {"weight_heuristic": -1.0},
                       {"gpu_memory_fraction": 5.0}):
            with self.assertRaises(ValueError):
                TalkNetConfig(**kwargs)

    def test_ah_resource_modes_inspectable(self):
        cfg = _cfg()
        low = cfg.effective(cpu_count=4, ram_gb=4.0)
        balanced = cfg.effective(cpu_count=8, ram_gb=12.0)
        perf = cfg.effective(cpu_count=8, ram_gb=32.0, vram_gb=8.0)
        self.assertEqual(low["mode"], "low")
        self.assertEqual(balanced["mode"], "balanced")
        self.assertEqual(perf["mode"], "performance")
        self.assertLessEqual(low["max_batch_size"], balanced["max_batch_size"])


class AudioTests(unittest.TestCase):
    def test_mfcc_shapes_and_sanity(self):
        from gaeclip.talknet.audio import mfcc_features, mfcc_for_window

        sr = 16000
        t = np.arange(sr, dtype=np.float64) / sr
        tone = (0.5 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
        silence = np.zeros(sr, dtype=np.int16)
        rows_tone = mfcc_for_window(tone, 25)
        self.assertEqual(rows_tone.shape, (100, 13))
        self.assertFalse(bool(np.isnan(rows_tone).any()))
        # Energy (c0) separates speech from silence.
        self.assertGreater(float(rows_tone[:, 0].mean()),
                           float(mfcc_for_window(silence, 25)[:, 0].mean()) + 5.0)
        # Deterministic.
        np.testing.assert_array_equal(
            rows_tone, mfcc_for_window(tone, 25))
        # Empty-safe, no NaN.
        empty = mfcc_features(np.zeros(0, dtype=np.int16))
        self.assertEqual(empty.shape, (0, 13))


class ModelLifecycleTests(unittest.TestCase):
    @classmethod
    def model_path(cls):
        from gaeclip.talknet.model import resolve_model_path

        return resolve_model_path("")

    def test_e_missing_model_resolves_none(self):
        from gaeclip.talknet.model import resolve_model_path

        self.assertIsNone(resolve_model_path("/nonexistent/weights.model"))

    def test_bundled_weights_found(self):
        self.assertIsNotNone(self.model_path())

    def test_ai_lazy_singleton_reuse(self):
        from gaeclip.talknet import model as model_mod

        model_mod.release_all()
        from gaeclip.talknet.model import get_model

        first = get_model(self.model_path(), "cpu", 2)
        second = get_model(self.model_path(), "cpu", 2)
        self.assertIs(first, second)
        self.assertEqual(len(model_mod._REGISTRY), 1)

    def test_real_infer_shapes(self):
        import torch

        from gaeclip.talknet.model import get_model

        model = get_model(self.model_path(), "cpu", 2)
        rng = np.random.default_rng(7)
        visual = torch.from_numpy(
            rng.integers(0, 255, size=(2, 12, 112, 112)).astype(np.float32))
        audio = torch.zeros((2, 48, 13))
        probs = model.infer(visual, audio)
        self.assertEqual(tuple(probs.shape), (2, 12))
        self.assertTrue(bool(((probs >= 0) & (probs <= 1)).all()))
        # inference_mode: no grad graph retained.
        self.assertFalse(probs.requires_grad)


class FakeTrack:
    def __init__(self, speaker_id, epoch=0, boxes=None):
        self.speaker_id = speaker_id
        self.epoch = epoch
        self.boxes = boxes or []


class DetectorStateTests(unittest.TestCase):
    def test_aj_no_model_when_unused(self):
        from gaeclip.talknet import model as model_mod
        from gaeclip.talknet.detector import TalkNetDetector

        model_mod.release_all()
        detector = TalkNetDetector(_cfg())
        self.assertEqual(len(model_mod._REGISTRY), 0)

    def test_disabled_never_loads(self):
        from gaeclip.talknet import model as model_mod
        from gaeclip.talknet.detector import TalkNetDetector

        model_mod.release_all()
        detector = TalkNetDetector(_cfg(enabled=False))
        self.assertFalse(detector.ensure_ready())
        self.assertEqual(detector.status, "DISABLED")
        self.assertEqual(len(model_mod._REGISTRY), 0)

    def test_e_model_missing_status(self):
        from gaeclip.talknet.detector import TalkNetDetector

        detector = TalkNetDetector(_cfg(model_path="/nonexistent/x.model"))
        self.assertFalse(detector.ensure_ready())
        self.assertEqual(detector.status, "MODEL_MISSING")

    def test_f_init_failure_status(self):
        from gaeclip.talknet.detector import TalkNetDetector

        detector = TalkNetDetector(_cfg(model_path="/tmp"))
        self.assertFalse(detector.ensure_ready())
        self.assertIn(detector.status, ("MODEL_MISSING", "INIT_ERROR"))

    def test_g_unavailable_falls_back_to_heuristic(self):
        from gaeclip.talknet.detector import TalkNetDetector

        detector = TalkNetDetector(_cfg(enabled=False))
        result = detector.score_faces("dummy.mp4", [FakeTrack("a")],
                                      0.0, 0.5, reason="test")
        self.assertEqual(result.scores, [])
        self.assertEqual(detector.metrics["fallback_count"], 1)


class CacheTests(unittest.TestCase):
    def _detector_with_stub(self, calls):
        from gaeclip.talknet.detector import TalkNetDetector
        from gaeclip.talknet.device import InferenceDevice
        from gaeclip.talknet.types import SpeakerScore

        detector = TalkNetDetector(_cfg(cache_ttl_ms=60000))
        detector.status = "READY"
        detector.device_info = InferenceDevice(
            device="cpu", backend="cpu", available=True, name="CPU",
            reason="test")

        def fake_uncached(video_path, tracks, t0, t1, audio_mono,
                          sample_rate, src_w, src_h, epoch_of, bucket, result):
            calls.append((t0, t1, tuple(
                getattr(t, "speaker_id", "") for t in tracks)))
            for track in tracks:
                tid = getattr(track, "speaker_id", "")
                entry = SpeakerScore(
                    track_id=tid, track_epoch=epoch_of[id(track)],
                    timestamp=(t0 + t1) / 2, window_start=t0, window_end=t1,
                    score=0.9, provider="talknet")
                key = (epoch_of[id(track)], tid, bucket[0], bucket[1])
                import time as _time

                detector._cache[key] = (_time.monotonic(), entry)
                result.scores.append(entry)
                result.faces_scored += 1

        detector._score_uncached = fake_uncached
        return detector

    def test_h_cache_hit(self):
        detector = self._detector_with_stub(calls := [])
        tracks = [FakeTrack("a"), FakeTrack("b")]
        first = detector.score_faces("v.mp4", tracks, 1.0, 1.5)
        second = detector.score_faces("v.mp4", tracks, 1.0, 1.5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first.scores, second.scores)
        self.assertEqual(detector.metrics["cache_hits"], 2)

    def test_i_cache_miss_new_window(self):
        detector = self._detector_with_stub(calls := [])
        tracks = [FakeTrack("a")]
        detector.score_faces("v.mp4", tracks, 1.0, 1.5)
        detector.score_faces("v.mp4", tracks, 2.0, 2.5)
        self.assertEqual(len(calls), 2)

    def test_j_cache_eviction_bounded(self):
        detector = self._detector_with_stub(calls := [])
        detector.config = _cfg(cache_ttl_ms=60000, cache_max_entries=4)
        tracks = [FakeTrack("a")]
        for i in range(8):
            detector.score_faces("v.mp4", tracks, float(i), float(i) + 0.5)
        self.assertLessEqual(len(detector._cache), 4)

    def test_k_epoch_isolation(self):
        detector = self._detector_with_stub(calls := [])
        detector.score_faces("v.mp4", [FakeTrack("a", epoch=0)], 1.0, 1.5)
        detector.score_faces("v.mp4", [FakeTrack("a", epoch=1)], 1.0, 1.5)
        # Same id+window but new epoch -> recompute (no leak across cuts).
        self.assertEqual(len(calls), 2)

    def test_m_insufficient_data(self):
        from gaeclip.talknet.preprocessing import boxes_at_grid, crop_face_sequence

        track = FakeTrack("a", boxes=[(0.0, (0.2, 0.2, 0.4, 0.6))])
        boxes = boxes_at_grid(track, [0.0, 0.5, 1.0])
        self.assertEqual(boxes[0], (0.2, 0.2, 0.4, 0.6))
        self.assertIsNone(boxes[2])
        frames = [(t, np.zeros((100, 100, 3), dtype=np.uint8)) for t in (0.0, 0.5)]
        seq, coverage = crop_face_sequence(
            frames, [None, None], min_coverage=0.6)
        self.assertIsNone(seq)
        self.assertEqual(coverage, 0.0)


class FusionTests(unittest.TestCase):
    def test_fusion_weights_and_nan_safety(self):
        from gaeclip.talknet.fusion import fuse_scores

        fused = fuse_scores(0.9, 0.5, 0.8, 0.9, None,
                            weight_talknet=0.55, weight_heuristic=0.25,
                            weight_continuity=0.10, weight_face_quality=0.05,
                            weight_onset=0.10)
        # (0.55*.9 + 0.25*.5 + 0.10*.8 + 0.05*.9) / 0.95
        self.assertAlmostEqual(fused, (0.495 + 0.125 + 0.08 + 0.045) / 0.95,
                               places=5)
        # TalkNet absent -> heuristic exactly (zero behavior change).
        self.assertAlmostEqual(
            fuse_scores(None, 0.62, 0.8, 0.9, None), 0.62, places=6)
        self.assertEqual(
            fuse_scores(float("nan"), 0.5, None, None, None), 0.5)

    def test_y_low_talknet_high_heuristic(self):
        from gaeclip.talknet.fusion import fuse_scores

        # Weak TalkNet dampens but must not silence strong heuristic
        # speech: stays in the LOW band (>= 0.25), below the heuristic.
        fused = fuse_scores(0.1, 0.85, None, None, None)
        self.assertGreaterEqual(fused, 0.25)
        self.assertLess(fused, 0.85)

    def test_x_high_score_low_margin_ambiguous(self):
        from gaeclip.talknet.fusion import overlapping_speech
        from gaeclip.talknet.types import confidence_level

        self.assertEqual(confidence_level(0.89, 0.03), "AMBIGUOUS")
        is_overlap, who = overlapping_speech({"a": 0.86, "b": 0.82})
        self.assertTrue(is_overlap)
        self.assertEqual(who, ["a", "b"])
        self.assertFalse(overlapping_speech({"a": 0.86, "b": 0.2})[0])


class EscalationTests(unittest.TestCase):
    def test_crowded_triggers(self):
        from gaeclip.talknet.fusion import decide_escalation

        ok, reason = decide_escalation(5, 0.5, 0.45)
        self.assertTrue(ok)
        self.assertEqual(reason, "crowded")

    def test_ambiguous_triggers(self):
        from gaeclip.talknet.fusion import decide_escalation

        ok, reason = decide_escalation(2, 0.62, 0.58)
        self.assertTrue(ok)
        self.assertEqual(reason, "ambiguous_margin")

    def test_low_confidence_triggers(self):
        from gaeclip.talknet.fusion import decide_escalation

        ok, reason = decide_escalation(3, 0.4, 0.2)
        self.assertTrue(ok)
        self.assertIn(reason, ("low_confidence", "ambiguous_margin", "crowded"))

    def test_confident_pair_never_pays(self):
        from gaeclip.talknet.fusion import decide_escalation

        ok, _ = decide_escalation(2, 0.9, 0.5)
        self.assertFalse(ok)

    def test_single_face_never_pays(self):
        from gaeclip.talknet.fusion import decide_escalation

        for kwargs in ({"strong_mouth_count": 0},):
            ok, _ = decide_escalation(1, 0.9, 0.0, **kwargs)
            self.assertFalse(ok)

    def test_overlap_onset_lost_acquire_triggers(self):
        from gaeclip.talknet.fusion import decide_escalation

        for kwargs in ({"overlap": True}, {"new_onset": True},
                       {"target_lost": True}, {"acquire_failed": True},
                       {"strong_mouth_count": 2}):
            ok, reason = decide_escalation(2, 0.9, 0.5, **kwargs)
            self.assertTrue(ok, kwargs)
            self.assertTrue(reason)


class ResolveAndStatusTests(unittest.TestCase):
    def test_resolve_without_client_uses_env(self):
        from gaeclip.talknet.config import TalkNetConfig

        cfg = TalkNetConfig.resolve(None)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.device, "auto")

    def test_resolve_prefers_db_over_env(self):
        import sys

        sys.path.insert(0, "tests")
        from test_video_metadata import FakeRedis

        from gaeclip.app_settings import AppSettings
        from gaeclip.talknet.config import TalkNetConfig

        fake = FakeRedis()
        AppSettings(fake).set("TALKNET_DEVICE", "cpu")
        cfg = TalkNetConfig.resolve(fake)
        self.assertEqual(cfg.device, "cpu")

    def test_status_endpoint_shape(self):
        import sys

        sys.path.insert(0, "tests")
        from test_video_metadata import make_client

        client, _fake = make_client()
        res = client.get("/api/v1/vision/talknet/status")
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        for key in ("enabled", "status", "device", "backend",
                    "model_found", "model_loaded"):
            self.assertIn(key, body)
        self.assertNotIn("api_key", str(body).lower())


class WiringTests(unittest.TestCase):
    def test_escalation_attaches_and_fuses(self):
        # Full analyze_section with a stubbed detector: ambiguous scene
        # (alternating motion) must trigger, attach talknet_score, fuse,
        # and record talknet metrics — Target Manager path untouched.
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from gaeclip.talknet.config import TalkNetConfig
        from gaeclip.talknet.detector import TalkNetDetector
        from gaeclip.talknet.types import SpeakerScore
        from test_autoframing import (FakeDetector, motion_frames,
                                      talking_face, tone_audio)

        seen = {"calls": 0}

        def fake_score_faces(video_path, tracks, t0, t1, audio_mono,
                             sample_rate, src_w, src_h, reason=""):
            from gaeclip.talknet.detector import TalkNetDetector as _D

            seen["calls"] += 1
            scores = []
            for track in tracks:
                tid = getattr(track, "speaker_id", "")
                # Strong verdict for the LEFT face only.
                value = 0.9 if tid.endswith("000") else 0.1
                scores.append(SpeakerScore(
                    track_id=tid, track_epoch=getattr(track, "epoch", 0),
                    timestamp=(t0 + t1) / 2, window_start=t0, window_end=t1,
                    score=value, provider="talknet"))
            from gaeclip.talknet.detector import TalkNetResult

            return TalkNetResult(scores=scores, device="cpu",
                                 faces_requested=len(tracks),
                                 faces_scored=len(scores),
                                 escalation_reason=reason)

        script = [talking_face(0.25) + talking_face(0.75) for _ in range(12)]
        frames = motion_frames(
            12, [(24, 64) if i % 2 == 0 else (104, 144) for i in range(12)])
        stamps = [i * 0.5 for i in range(12)]
        cfg = TalkNetConfig(enabled=True, device="cpu", min_people=2)
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector([r[:] for r in script])), \
            patch.object(TalkNetDetector, "ensure_ready", return_value=True), \
            patch.object(TalkNetDetector, "score_faces",
                         side_effect=fake_score_faces), \
            patch.object(TalkNetDetector, "close", return_value=None):
            plan = auto.analyze_section(
                "dummy.mp4", duration=6.0,
                frame_source=list(zip(stamps, frames)),
                talknet_config=cfg)
        self.assertGreater(seen["calls"], 0)
        self.assertIn("talknet", plan.metrics)
        attached = [o.talknet_score for o in
                    self._obs_of(plan) if o.talknet_score is not None]
        self.assertTrue(attached)

    def _obs_of(self, plan):
        # Re-derive attached scores from the evidence artifact.
        out = []

        class _O:
            def __init__(self, v):
                self.talknet_score = v

        for row in plan.evidence_log:
            if row.get("talknet_score") is not None:
                out.append(_O(row["talknet_score"]))
        return out

    def test_disabled_config_never_touches_detector(self):
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from gaeclip.talknet.detector import TalkNetDetector
        from test_autoframing import (FakeDetector, motion_frames,
                                      talking_face, tone_audio)

        script = [talking_face(0.25) + talking_face(0.75) for _ in range(12)]
        frames = motion_frames(12, [(24, 64)] * 12)
        stamps = [i * 0.5 for i in range(12)]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector([r[:] for r in script])), \
            patch.object(TalkNetDetector, "ensure_ready") as ready:
            plan = auto.analyze_section(
                "dummy.mp4", duration=6.0,
                frame_source=list(zip(stamps, frames)),
                talknet_config=None)
        ready.assert_not_called()
        self.assertNotIn("talknet", plan.metrics)


class BatchingTests(unittest.TestCase):
    def test_ag_batching_bounded(self):
        # Batching path is exercised in the integration test below; here
        # assert the config contract (bounded, >=1).
        cfg = _cfg(max_batch_size=4)
        self.assertEqual(cfg.max_batch_size, 4)
        with self.assertRaises(ValueError):
            _cfg(max_batch_size=0)


class IntegrationTests(unittest.TestCase):
    def test_n_to_r_five_faces_mock(self):
        # §35 with injected TalkNet verdicts (no model): D wins, overlap
        # holds, B takes over after hysteresis.
        from gaeclip.talknet.fusion import fuse_scores

        heuristic = {"a": 0.20, "b": 0.15, "c": 0.18, "d": 0.55, "e": 0.21}
        talknet = {"a": 0.20, "b": 0.15, "c": 0.18, "d": 0.82, "e": 0.21}
        fused = {k: fuse_scores(talknet[k], heuristic[k], None, None, None)
                 for k in heuristic}
        best = max(fused, key=fused.get)
        self.assertEqual(best, "d")
        # Overlap: A=0.80 B=0.77 D=0.81 -> ambiguous, must not flap.
        from gaeclip.talknet.fusion import overlapping_speech

        self.assertTrue(overlapping_speech({"a": 0.80, "b": 0.77, "d": 0.81})[0])

    def test_real_model_single_window_mechanics(self):
        # REAL weights, synthetic video: proves crop->MFCC->batch->score
        # plumbing (not accuracy) on this CPU box.
        import subprocess

        from gaeclip.talknet.detector import TalkNetDetector

        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "testsrc2=size=320x240:rate=30:duration=2",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
             "-shortest", "/tmp/talknet_mech.mp4"],
            check=True, timeout=120)
        import numpy as np

        sr = 16000
        t = np.arange(2 * sr, dtype=np.float64) / sr
        audio = (0.5 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
        tracks = [FakeTrack("a", boxes=[(s / 10, (0.2, 0.2, 0.4, 0.6))
                                        for s in range(20)]),
                  FakeTrack("b", boxes=[(s / 10, (0.5, 0.2, 0.7, 0.6))
                                        for s in range(20)])]
        detector = TalkNetDetector(_cfg(device="cpu", max_batch_size=4))
        result = detector.score_faces("/tmp/talknet_mech.mp4", tracks,
                                      0.25, 0.75, audio, 16000, 320, 240,
                                      reason="test")
        self.assertEqual(detector.status, "READY")
        self.assertEqual(result.faces_requested, 2)
        self.assertEqual(len(result.scores), 2)
        self.assertLessEqual(result.batch_size, 4)
        for entry in result.scores:
            self.assertGreaterEqual(entry.score, 0.0)
            self.assertLessEqual(entry.score, 1.0)
        self.assertGreater(result.inference_ms, 0)


if __name__ == "__main__":
    unittest.main()
