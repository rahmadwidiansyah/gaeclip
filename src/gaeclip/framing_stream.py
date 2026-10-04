"""Streaming analysis input for dynamic framing (Phase 1).

Replaces "decode everything, then analyze" with a bounded-memory pipeline::

    source frame (any size)
      -> ffmpeg fps + uniform scale (analysis size, aspect preserved)
      -> generator yields ONE analysis frame at a time
      -> caller processes + releases (only 1-2 frames alive)

Coordinates stay normalized (0-1), so analysis resolution is transparent
to detection/tracking: no remap needed as long as resize is uniform
(which ``scale=W:-2`` guarantees).

Scene-cut detection is intentionally dependency-free (numpy only): a
normalized grayscale frame difference with a minimum interval. Camera pans
and small lighting changes score far below a real cut; the threshold and
interval are configurable via ``framing_config``.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Iterator

logger = logging.getLogger(__name__)


@dataclass
class AnalysisFrame:
    """One analysis sample. ``frame`` is HxWx3 uint8 (analysis size)."""

    timestamp: float
    frame: object  # numpy array; typed loosely to avoid hard numpy import
    index: int
    source_width: int
    source_height: int
    analysis_width: int
    analysis_height: int


def analysis_size_for(source_width: int, source_height: int,
                      max_width: int) -> tuple[int, int]:
    """Uniform analysis size: capped at ``max_width``, aspect preserved,
    even dimensions, never upscaled (small sources keep native size)."""
    if source_width <= 0 or source_height <= 0:
        raise ValueError("invalid source dimensions")
    if source_width <= max_width:
        width = source_width if source_width % 2 == 0 else max(2, source_width - 1)
        height = source_height if source_height % 2 == 0 else max(2, source_height - 1)
        return width, height
    width = max(2, (max_width // 2) * 2)
    height = max(2, int(round(source_height * (width / source_width))) // 2 * 2)
    return width, height


def _numpy():
    try:
        import numpy as np
    except ImportError as exc:
        from .errors import ExternalCommandError
        raise ExternalCommandError("numpy is required for auto framing") from exc
    return np


def iter_analysis_frames(video_path: str, fps: float = 6.0,
                         max_width: int = 640) -> Iterator[AnalysisFrame]:
    """Yield ``AnalysisFrame`` samples via a streaming ffmpeg pipe.

    Bounded memory: at most one frame is yielded at a time; the caller must
    not accumulate frames. On decode failure with zero frames an
    ``ExternalCommandError`` is raised (same contract as ``extract_frames``).
    """
    from .autoframing import probe_dimensions

    np = _numpy()
    src_w, src_h = probe_dimensions(video_path)
    analysis_w, analysis_h = analysis_size_for(src_w, src_h, max_width)
    filters = [f"fps={fps}"]
    if (analysis_w, analysis_h) != (src_w, src_h):
        filters.append(f"scale={analysis_w}:{analysis_h}")
    cmd = [
        "ffmpeg", "-hide_banner", "-v", "error", "-i", video_path,
        "-vf", ",".join(filters),
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    frame_bytes = analysis_w * analysis_h * 3
    index = 0
    produced = 0
    try:
        while True:
            raw = proc.stdout.read(frame_bytes)
            if not raw:
                break
            if len(raw) < frame_bytes:
                # Trailing partial frame: drop, do not misalign the stream.
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(analysis_h, analysis_w, 3)
            yield AnalysisFrame(
                timestamp=index / fps,
                frame=frame,
                index=index,
                source_width=src_w,
                source_height=src_h,
                analysis_width=analysis_w,
                analysis_height=analysis_h,
            )
            produced += 1
            index += 1
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    if produced == 0:
        from .errors import ExternalCommandError
        raise ExternalCommandError(f"No decodable frames in {video_path}")


def rss_mb() -> float:
    """Process peak RSS in MiB. -1 when unavailable.

    Cross-platform: ``resource`` on Unix, ``psutil`` when installed,
    Win32 ``GetProcessMemoryInfo`` fallback (the stdlib ``resource``
    module does not exist on Windows, where this previously always
    returned -1 and silently disabled the memory guard).
    """
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        pass
    try:
        import os

        import psutil

        return psutil.Process(os.getpid()).memory_info().rss / 1048576.0
    except Exception:
        pass
    try:
        import ctypes
        import sys

        if sys.platform == "win32":
            return _win32_peak_rss_mb(ctypes)
    except Exception:
        pass
    return -1.0


def _win32_peak_rss_mb(ctypes) -> float:
    """Peak working set via psapi (no third-party deps).

    argtypes/restype are mandatory here: without them ctypes passes the
    64-bit process HANDLE truncated to 32-bit int, GetProcessMemoryInfo
    fails, and the caller falls back to -1.
    """
    from ctypes import wintypes

    class _Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    get_process = ctypes.windll.kernel32.GetCurrentProcess
    get_process.restype = wintypes.HANDLE
    get_process.argtypes = []
    get_mem_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_mem_info.restype = wintypes.BOOL
    get_mem_info.argtypes = [wintypes.HANDLE,
                             ctypes.POINTER(_Counters), wintypes.DWORD]
    counters = _Counters()
    counters.cb = ctypes.sizeof(_Counters)
    if not get_mem_info(get_process(), ctypes.byref(counters), counters.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return counters.PeakWorkingSetSize / 1048576.0


class AdaptiveSampler:
    """Motion-gated detector stride (no resampling, no timestamp rewrite).

    The decoder always runs at full fps; this gate only decides which
    decoded frames pay for detection. Sustained stillness
    (``still_windows`` straight windows below ``still_threshold``) drops
    to stride 2; any motion returns to stride 1 on the very same frame,
    so speech onsets are never skipped. Timestamps always come from the
    stream, so VFR/PTS validity is untouched. Deterministic (no randomness,
    no wall-clock): same scores in, same decisions out.
    """

    def __init__(self, still_threshold: float = 0.02, still_windows: int = 4,
                 max_stride: int = 2):
        self.still_threshold = still_threshold
        self.still_windows = max(1, still_windows)
        self.max_stride = 2 if max_stride >= 2 else 1
        self.still_count = 0
        self.stride = 1
        self.phase = 0
        self.analyzed = 0
        self.skipped = 0

    def reset(self) -> None:
        self.still_count = 0
        self.stride = 1
        self.phase = 0

    def update(self, motion_score: float) -> bool:
        """Feed this frame's normalized diff score; True = analyze it."""
        if motion_score >= self.still_threshold:
            self.still_count = 0
            self.stride = 1
        else:
            self.still_count += 1
            if self.still_count >= self.still_windows:
                self.stride = self.max_stride
        take = (self.phase % self.stride == 0)
        self.phase += 1
        if take:
            self.analyzed += 1
        else:
            self.skipped += 1
        return take


def grayscale_thumb(frame, thumb_width: int = 96):
    """Luminosity thumbnail (2D float32) for cheap frame comparison."""
    np = _numpy()
    height, width = frame.shape[:2]
    step = max(1, width // max(8, thumb_width))
    small = frame[::step, ::step].astype(np.float32)
    return (0.299 * small[:, :, 0]
            + 0.587 * small[:, :, 1]
            + 0.114 * small[:, :, 2])


def thumb_diff(previous, current, thumb_width: int = 96) -> float:
    """Normalized mean-abs grayscale difference in 0..1 (stateless)."""
    np = _numpy()
    if previous is None or current is None:
        return 0.0
    try:
        prev = grayscale_thumb(previous, thumb_width)
        cur = grayscale_thumb(current, thumb_width)
        if prev.shape != cur.shape:
            return 0.0
        return float(np.abs(cur - prev).mean() / 255.0)
    except Exception:
        return 0.0


class SceneCutDetector:
    """Lightweight cut detector on analysis frames (numpy only).

    Score = mean absolute grayscale difference vs previous frame,
    normalized to 0..1. A hard cut scores ~0.3-0.8; pans and lighting
    drift score an order of magnitude lower. ``min_interval_ms`` prevents
    spam on flickering sources.
    """

    def __init__(self, threshold: float = 0.35,
                 min_interval_ms: int = 1000,
                 thumb_width: int = 96):
        self.threshold = threshold
        self.min_interval_ms = min_interval_ms
        self.thumb_width = max(8, thumb_width)
        self._previous: object = None
        self._last_cut_time: float = float("-inf")

    def reset(self) -> None:
        self._previous = None
        self._last_cut_time = float("-inf")

    def update(self, frame, timestamp: float) -> tuple[bool, float]:
        """Return (is_cut, score). First frame never counts as a cut."""
        np = _numpy()
        thumb = grayscale_thumb(frame, self.thumb_width)
        previous = self._previous
        self._previous = thumb
        if previous is None:
            return False, 0.0
        try:
            diff = float(np.abs(thumb - previous).mean() / 255.0)
        except Exception:
            return False, 0.0
        if diff < self.threshold:
            return False, diff
        if (timestamp - self._last_cut_time) * 1000.0 < self.min_interval_ms:
            return False, diff
        self._last_cut_time = timestamp
        logger.info("[SCENE_CUT] time=%.2f score=%.2f", timestamp, diff)
        return True, diff
