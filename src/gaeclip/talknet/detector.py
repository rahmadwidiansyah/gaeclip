"""TalkNetDetector: escalation-scoped active-speaker scoring (§6).

One detector per analysis section (cheap; the MODEL is the shared lazy
singleton). Per window: crops per track -> bounded batches -> one forward
per batch -> per-face speaking score. Results cached by
(track_epoch, track_id, window bucket) with TTL + bounded eviction, so a
revisited window never recomputes and RAM never grows.

Status model: READY | NOT_INSTALLED | MODEL_MISSING | INIT_ERROR |
DISABLED (surfaced via the status endpoint, never raised into jobs).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .config import TalkNetConfig
from .device import InferenceDevice, resolve_device, vram_allows_cuda
from .model import estimate_model_bytes, get_model, resolve_model_path
from .types import TalkNetResult, SpeakerScore, confidence_level

logger = logging.getLogger(__name__)


class TalkNetDetector:
    def __init__(self, config: TalkNetConfig | None = None):
        self.config = config or TalkNetConfig.from_env()
        self.device_info: InferenceDevice | None = None
        self.status = "DISABLED" if not self.config.enabled else "UNKNOWN"
        self.status_detail = ""
        self._cache: dict[tuple, tuple[float, SpeakerScore]] = {}
        self._infer_lock = threading.Lock()
        self.trigger_log: list[dict] = []
        self.metrics = {
            "inference_count": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "trigger_count": 0,
            "fallback_count": 0,
            "cpu_count": 0,
            "gpu_count": 0,
            "inference_ms_total": 0.0,
            "faces_scored": 0,
        }

    # -- lifecycle ------------------------------------------------------
    def ensure_ready(self) -> bool:
        """Lazy init on first escalation (never at app startup)."""
        if not self.config.enabled:
            self.status, self.status_detail = "DISABLED", "disabled_by_config"
            return False
        if self.status == "READY" and self.device_info is not None:
            return True
        try:
            import torch  # noqa: F401
        except ImportError:
            self.status, self.status_detail = "NOT_INSTALLED", "torch_missing"
            logger.info("[TALKNET_FALLBACK] reason=torch_not_installed")
            return False
        path = resolve_model_path(self.config.model_path)
        if path is None:
            self.status, self.status_detail = (
                "MODEL_MISSING",
                "set TALKNET_MODEL_PATH or place models/TalkNet-ASD/pretrain_TalkSet.model")
            logger.info("[TALKNET_FALLBACK] reason=model_missing")
            return False
        info = resolve_device(self.config.device)
        ok, why = vram_allows_cuda(
            info, estimate_model_bytes(path), self.config.gpu_memory_fraction)
        if not ok:
            logger.warning("[TALKNET_DEVICE] device=cpu reason=%s", why)
            info = resolve_device("cpu")
        try:
            get_model(path, info.device, self.config.cpu_threads)
        except Exception as exc:
            logger.warning("[TALKNET_FALLBACK] reason=model_init_failed: %s", exc)
            if info.backend != "cpu":
                try:
                    get_model(path, "cpu", self.config.cpu_threads)
                    info = resolve_device("cpu")
                except Exception as exc2:
                    self.status, self.status_detail = (
                        "INIT_ERROR", f"cpu_fallback_failed: {exc2}")
                    return False
            else:
                self.status, self.status_detail = (
                    "INIT_ERROR", f"init_failed: {exc}")
                return False
        self.device_info = info
        self.status, self.status_detail = "READY", ""
        if info.backend == "cpu":
            self.metrics["cpu_count"] += 1
        else:
            self.metrics["gpu_count"] += 1
        return True

    def warmup(self) -> None:
        if self.ensure_ready():
            from .model import get_model as _get

            _get(resolve_model_path(self.config.model_path),
                 self.device_info.device,
                 self.config.cpu_threads).warmup()

    def close(self) -> None:
        """End of analysis section: drop the window cache (tensors were
        transient all along). The shared model stays loaded for reuse."""
        self._cache.clear()

    # -- scoring ----------------------------------------------------------
    def fused_score(self, obs) -> float:
        """Blend TalkNet with the heuristic verdict (§13).

        heuristic_part reuses the observation's own fusion weights
        (visual+audio only); continuity/quality/onset stay structural
        (inside active_score + timeline latch) so nothing double-counts.
        TalkNet absent -> active_score exactly (zero behavior change).
        """
        from .fusion import fuse_scores

        talknet_value = getattr(obs, "talknet_score", None)
        if talknet_value is None:
            return float(getattr(obs, "active_score", 0.0))
        weights = getattr(obs, "score_weights", None)
        if weights is not None and len(weights) == 3:
            visual_w, audio_w, _cont = weights
        else:
            visual_w, audio_w = 0.55, 0.35
        denom = visual_w + audio_w
        try:
            heuristic_part = (
                (visual_w * float(obs.lip_score)
                 + audio_w * float(obs.audio_score)) / denom
                if denom > 0 else float(obs.active_score))
        except (TypeError, ValueError, AttributeError):
            heuristic_part = float(getattr(obs, "active_score", 0.0))
        return fuse_scores(
            talknet_value, heuristic_part,
            getattr(obs, "continuity_score", None),
            getattr(obs, "quality_score", None), None,
            weight_talknet=self.config.weight_talknet,
            weight_heuristic=self.config.weight_heuristic,
            weight_continuity=self.config.weight_continuity,
            weight_face_quality=self.config.weight_face_quality,
            weight_onset=self.config.weight_onset)

    def _bucket(self, t0: float, t1: float) -> tuple[int, int]:
        return (int(round(t0 * 1000)), int(round(t1 * 1000)))

    def score_faces(self, video_path: str, tracks: list[Any],
                    t0: float, t1: float,
                    audio_mono: Any = None, sample_rate: int = 16000,
                    src_w: int = 0, src_h: int = 0,
                    reason: str = "") -> TalkNetResult:
        """Score tracks over [t0, t1]; heuristic fallback on ANY failure."""
        result = TalkNetResult(device="cpu", escalation_reason=reason)
        result.faces_requested = len(tracks)
        if not self.ensure_ready():
            self.metrics["fallback_count"] += 1
            return result
        assert self.device_info is not None
        result.device = self.device_info.device
        epoch_of = {id(t): getattr(t, "epoch", 0) for t in tracks}
        bucket = self._bucket(t0, t1)
        # Cache lookup first (temporal dedup §12).
        pending: list[Any] = []
        for track in tracks:
            tid = getattr(track, "speaker_id", str(track))
            key = (epoch_of[id(track)], tid, bucket[0], bucket[1])
            hit = self._cache.get(key)
            if hit is not None and (time.monotonic() - hit[0]) * 1000.0 <= self.config.cache_ttl_ms:
                result.scores.append(hit[1])
                result.cache_hits += 1
                self.metrics["cache_hits"] += 1
            else:
                pending.append(track)
                result.cache_misses += 1
                self.metrics["cache_misses"] += 1
        self._evict_cache()
        if pending:
            self._score_uncached(video_path, pending, t0, t1, audio_mono,
                                 sample_rate, src_w, src_h, epoch_of,
                                 bucket, result)
            self._evict_cache()  # bound AFTER inserts (not just before)
        result.inference_ms = round(result.inference_ms, 1)
        result.preprocess_ms = round(result.preprocess_ms, 1)
        self._relabel_confidence(result)
        return result

    def _relabel_confidence(self, result: TalkNetResult) -> None:
        """Peer-relative confidence: a lone strong face is HIGH, two close
        faces are AMBIGUOUS (margin decides, never the absolute score)."""
        from .types import confidence_level

        scored = sorted(result.scores, key=lambda s: -s.score)
        relabeled: list[SpeakerScore] = []
        for rank, entry in enumerate(scored):
            peer = scored[0].score if rank > 0 else (
                scored[1].score if len(scored) > 1 else 0.0)
            margin = entry.score - peer
            relabeled.append(SpeakerScore(
                track_id=entry.track_id, track_epoch=entry.track_epoch,
                timestamp=entry.timestamp, window_start=entry.window_start,
                window_end=entry.window_end, score=entry.score,
                confidence=confidence_level(entry.score, margin),
                provider=entry.provider, device=entry.device))
        result.scores[:] = relabeled

    def _evict_cache(self) -> None:
        if len(self._cache) <= self.config.cache_max_entries:
            return
        now = time.monotonic()
        ttl_s = self.config.cache_ttl_ms / 1000.0
        stale = [k for k, (at, _) in self._cache.items() if now - at > ttl_s]
        for key in stale:
            del self._cache[key]
        while len(self._cache) > self.config.cache_max_entries:
            self._cache.pop(next(iter(self._cache)))

    def _score_uncached(self, video_path, tracks, t0, t1, audio_mono,
                        sample_rate, src_w, src_h, epoch_of, bucket,
                        result: TalkNetResult) -> None:
        import numpy as np

        from .audio import mfcc_for_window
        from .preprocessing import (boxes_at_grid, crop_face_sequence,
                                    decode_window_frames)

        pre_started = time.perf_counter()
        fps = self.config.frame_fps
        n_frames = max(4, int(round((t1 - t0) * fps)))
        stamps = [round(t0 + i / fps, 3) for i in range(n_frames)]
        decode_w = 480
        decode_h = max(2, int(round(src_h * decode_w / max(1, src_w) / 2)) * 2) \
            if src_w and src_h else 270
        frames = decode_window_frames(video_path, t0, t1, fps=fps,
                                      width=decode_w, height=decode_h)
        if not frames:
            self.metrics["fallback_count"] += 1
            return
        # Align decoded frames to the 25fps grid (nearest, bounded).
        grid = [min(frames, key=lambda f: abs(f[0] - s))[1] for s in stamps]
        # Slice section audio (16k mono expected; resample-free).
        audio_seg = None
        if audio_mono is not None and sample_rate == 16000:
            try:
                import numpy as _np

                arr = _np.asarray(audio_mono).ravel()
                audio_seg = arr[int(t0 * 16000):int(t1 * 16000)]
            except Exception:
                audio_seg = None
        pre_ms = (time.perf_counter() - pre_started) * 1000.0
        result.preprocess_ms = pre_ms
        # Build per-face batches grouped by frame count.
        batch_items: list[tuple[Any, Any, Any]] = []  # (track, faces, audio)
        for track in tracks:
            tid = getattr(track, "speaker_id", str(track))
            boxes = boxes_at_grid(track, stamps)
            # Map normalized boxes to DECODED frame pixels.
            px_boxes = []
            for box in boxes:
                if box is None:
                    px_boxes.append(None)
                else:
                    px_boxes.append(box)  # normalized; cropper scales
            seq, coverage = crop_face_sequence(
                [(s, f) for s, f in zip(stamps, grid)], px_boxes,
                size=self.config.face_size, padding=self.config.face_padding,
                min_coverage=self.config.min_coverage)
            if seq is None or audio_seg is None:
                logger.debug("[TALKNET_RESULT] track=%s insufficient_data "
                             "coverage=%.2f", tid, coverage)
                continue
            feats = mfcc_for_window(audio_seg, seq.shape[0], fps=float(fps))
            batch_items.append((track, seq, feats))
        if not batch_items:
            self.metrics["fallback_count"] += 1
            return
        # Bounded batches (§22).
        import torch

        path = resolve_model_path(self.config.model_path)
        assert path is not None and self.device_info is not None
        model = get_model(path, self.device_info.device, self.config.cpu_threads)
        infer_ms = 0.0
        scored = 0
        with self._infer_lock:
            for index in range(0, len(batch_items), self.config.max_batch_size):
                chunk = batch_items[index:index + self.config.max_batch_size]
                result.batch_size = max(result.batch_size, len(chunk))
                visual = torch.stack(
                    [torch.from_numpy(item[1]) for item in chunk])
                audio_b = torch.stack(
                    [torch.from_numpy(item[2]) for item in chunk])
                started = time.perf_counter()
                try:
                    with torch.inference_mode():
                        probs = model.infer(visual, audio_b)
                except RuntimeError as exc:
                    if "out of memory" in str(exc).lower():
                        logger.warning("[TALKNET_FALLBACK] reason=gpu_oom")
                    else:
                        logger.warning("[TALKNET_FALLBACK] reason=infer_failed: %s", exc)
                    self.metrics["fallback_count"] += 1
                    return
                infer_ms += (time.perf_counter() - started) * 1000.0
                # Center-weighted mean: middle of the window decides.
                weights = np.linspace(0.5, 1.0, probs.shape[1])
                weights = weights / weights.sum()
                for (track, _seq, _feats), prob_row in zip(
                        chunk, probs.tolist()):
                    tid = getattr(track, "speaker_id", str(track))
                    score = float(sum(p * w for p, w in zip(prob_row, weights)))
                    score = max(0.0, min(1.0, score))
                    margin = 0.0  # set by fusion against peers
                    entry = SpeakerScore(
                        track_id=tid, track_epoch=epoch_of[id(track)],
                        timestamp=round((t0 + t1) / 2, 3),
                        window_start=t0, window_end=t1, score=score,
                        confidence=confidence_level(score, margin),
                        provider="talknet", device=self.device_info.device)
                    key = (epoch_of[id(track)], tid, bucket[0], bucket[1])
                    self._cache[key] = (time.monotonic(), entry)
                    result.scores.append(entry)
                    scored += 1
        result.inference_ms += infer_ms
        result.faces_scored = scored
        self.metrics["inference_count"] += 1
        self.metrics["inference_ms_total"] += infer_ms
        self.metrics["faces_scored"] += scored
        logger.debug("[TALKNET_RESULT] window=%.2f-%.2f faces=%d scored=%d "
                     "infer_ms=%.0f device=%s",
                     t0, t1, len(tracks), scored, infer_ms,
                     self.device_info.device)
