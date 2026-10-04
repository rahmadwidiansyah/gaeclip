"""Face-track temporal crop extraction for TalkNet windows (§7-8).

Authoritative source: existing BlazeFace boxes + IoU tracks (NO second
detector). For a (track, time window): decode source frames at 25 fps,
crop each frame around the track's box (configurable padding, clamped),
grayscale + 112x112. Missing boxes (occlusion/gap/cut) yield
INSUFFICIENT_DATA when coverage drops below min_coverage — scores are
never fabricated.

Timestamps always come from the analysis pipeline (source time base);
no CFR conversion is introduced for TalkNet.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


def _numpy():
    try:
        import numpy as np
    except ImportError as exc:
        from ..errors import ExternalCommandError

        raise ExternalCommandError(
            "numpy is not installed; TalkNet preprocessing needs it "
            "(heuristic fallback remains available)") from exc
    return np


def decode_window_frames(video_path: str, t0: float, t1: float,
                         fps: int = 25, width: int = 480,
                         height: int = 270,
                         ) -> list[tuple[float, Any]]:
    """Decode [t0, t1] at fixed fps (bounded: TalkNet windows are <=1s).

    Caller passes the DECODED frame height (source aspect scaled to
    `width`; e.g. round(src_h * width / src_w / 2) * 2) so no probing or
    second ffprobe call is needed. Returns [(timestamp, BGR frame)].
    Empty list on any ffmpeg failure (caller treats as INSUFFICIENT_DATA,
    never crashes the job).
    """
    duration = max(0.05, t1 - t0)
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-ss", f"{max(0.0, t0):.3f}", "-to", f"{t1:.3f}",
           "-i", video_path,
           "-vf", f"fps={fps},scale={width}:{height}",
           "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("[TALKNET_FALLBACK] reason=decode_failed: %s", exc)
        return []
    if proc.returncode != 0:
        logger.warning("[TALKNET_FALLBACK] reason=decode_failed rc=%s",
                       proc.returncode)
        return []
    raw = proc.stdout
    step = width * height * 3
    np = _numpy()
    frames: list[tuple[float, Any]] = []
    for index in range(len(raw) // step):
        chunk = raw[index * step:(index + 1) * step]
        frame = np.frombuffer(chunk, dtype=np.uint8).reshape(height, width, 3)
        frames.append((round(t0 + index / fps, 3), frame.copy()))
    if not frames:
        logger.warning("[TALKNET_FALLBACK] reason=decode_empty")
    return frames


def crop_face_sequence(frames: list[tuple[float, Any]],
                       boxes: list[tuple[float, Any] | None],
                       size: int = 112,
                       padding: float = 0.25,
                       min_coverage: float = 0.60,
                       ) -> tuple[Any | None, float]:
    """Crop/resize/grayscale a face track over decoded frames.

    Returns (sequence (T,size,size) float32, coverage). None when coverage
    < min_coverage (INSUFFICIENT_DATA). Padding is a fraction of face size;
    crops never include unrelated people beyond the padded box, and all
    coordinates are clamped inside the frame.
    """
    try:
        import cv2
    except ImportError:
        return None, 0.0
    np = _numpy()
    if not frames or len(frames) != len(boxes):
        return None, 0.0
    crops = []
    hits = 0
    for (_stamp, frame), box in zip(frames, boxes):
        if box is None:
            continue
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = box
        face_w, face_h = max(1e-6, x2 - x1), max(1e-6, y2 - y1)
        pad_w, pad_h = face_w * padding, face_h * padding
        px1 = max(0, int((x1 - pad_w) * width))
        py1 = max(0, int((y1 - pad_h) * height))
        px2 = min(width, int((x2 + pad_w) * width + 0.5))
        py2 = min(height, int((y2 + pad_h) * height + 0.5))
        if px2 <= px1 or py2 <= py1:
            continue
        crop = frame[py1:py2, px1:px2]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        crops.append(cv2.resize(gray, (size, size),
                               interpolation=cv2.INTER_LINEAR))
        hits += 1
    coverage = hits / max(1, len(frames))
    if coverage < min_coverage or not crops:
        return None, coverage
    return np.stack(crops).astype(np.float32), coverage


def boxes_at_grid(track: Any, stamps: list[float],
                  max_gap: float = 0.5) -> list[Any | None]:
    """Stepwise box lookup (same semantics as Track.box_at), but a box
    older than ``max_gap`` is treated as missing: stale faces must not
    inflate coverage with ancient positions (occlusion honesty)."""
    boxes = getattr(track, "boxes", []) or []
    out: list[Any | None] = []
    for stamp in stamps:
        best = None
        for time, box in boxes:
            if time <= stamp + 1e-6:
                best = (time, box)
            else:
                break
        if best is not None and stamp - best[0] <= max_gap + 1e-9:
            out.append(best[1])
        else:
            out.append(None)
    return out
