"""Automatic speaker-aware framing for clip sections.

End-to-end flow (all CPU-friendly, no GPU required)::

    section video
      -> streaming samples (ffmpeg pipe, adaptive ≤8 fps, 640px analysis,
         bounded RAM; injected seam for tests)
      -> face detect (BlazeFace, bundled model) + scene-cut reset
      -> IoU tracking (stable speaker IDs across frames)
      -> audio energy VAD (ffmpeg PCM pipe, numpy only)
      -> per-track speaking score (lip openness gated by voice activity)
      -> speaker timeline with hold/margin/cooldown (visual.py)
      -> explicit target manager (presence/grace/fallback, target_manager.py)
      -> Kalman crop trajectory per run (smoothing.py; single primary filter)
      -> adaptive merge + eased acquisitions (switches stay hard cuts)
      -> portrait/group crop instructions (+ quality guard) -> render_dynamic_vertical

Fallback chain (each step logged, never fails the job):
  1. speaking segment  -> follow active speaker (single crop)
  2. faces but no confident speech -> single face: follow largest track;
     multiple faces: group crop covering all faces
  3. no faces / analysis error -> static center crop (caller fallback)
"""

from __future__ import annotations

import logging
import math
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SAMPLE_FPS = 2.0
MAX_FACES = 4
AUDIO_SAMPLE_RATE = 16000
VAD_WINDOW_SEC = 0.1
MIN_ACTIVE_SCORE = 0.25
SWITCH_HYSTERESIS_SEC = 2.0
# NOTE (Phase 1): tunables below are importable legacy aliases. New code
# reads FramingConfig (framing_config.py); defaults here are unchanged so
# existing callers/tests keep working.
# Framing smoothness — reduced from 0.4/0.12 to avoid jitter
SMOOTHING_ALPHA = 0.20   # EMA alpha for crop-center smoothing (lower = smoother)
MAX_STEP = 0.06          # max crop-center move per 0.5s sample (relative units)
MIN_FRAMING_MOVEMENT = 0.012  # min relative movement to update crop center (suppresses micro-jitter)
HEADROOM_FACTOR = 0.18   # fraction of face height to add as headroom above face center
IOU_MATCH_THRESHOLD = 0.3
MAX_MISSED_SAMPLES = 4
SAFE_MARGIN = 0.06

# --- Temporal state machine parameters ---
MIN_SPEAKER_HOLD_MS = 600         # ms a candidate must speak before becoming current
MIN_SPEAKER_SWITCH_INTERVAL_MS = 1500  # ms cooldown after a switch
TRACK_LOST_GRACE_MS = 400         # ms to keep current speaker when lost from detection
NO_SPEAKER_FALLBACK_MS = 1800     # ms of no confident speech before fallback
FORCE_SWITCH_SCORE_MARGIN = 0.40  # score diff to allow early switch override
SPEAKER_ACTIVATION_THRESHOLD = 0.28  # min score for initial activation
SPEAKER_SWITCH_THRESHOLD = 0.33   # min score for switching to new speaker
SCORE_SMOOTHING_ALPHA = 0.35      # EMA alpha for score smoothing (per speaker)

BLAZE_MODEL_CANDIDATES = (
    "/app/models/blaze_face_short_range.tflite",
    "models/blaze_face_short_range.tflite",
)


def resolve_blaze_model(explicit: str | None = None) -> str:
    """Find the BlazeFace .tflite without hardcoding one location."""
    from pathlib import Path

    candidates = ([explicit] if explicit else []) + list(BLAZE_MODEL_CANDIDATES)
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    from .errors import ExternalCommandError
    raise ExternalCommandError(
        "BlazeFace model not found; expected one of: " + ", ".join(candidates)
    )


# --------------------------------------------------------------------------
# Frame / audio extraction (ffmpeg pipes, numpy only — no cv2 needed)
# --------------------------------------------------------------------------

def _numpy():
    try:
        import numpy as np
    except ImportError as exc:
        from .errors import ExternalCommandError
        raise ExternalCommandError("numpy is required for auto framing") from exc
    return np


def probe_dimensions(path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream via ffprobe."""
    from .media import probe as _probe

    info = _probe(__import__("pathlib").Path(path))
    width = int(float(info.get("width", 0) or 0))
    height = int(float(info.get("height", 0) or 0))
    if width <= 0 or height <= 0:
        from .errors import ExternalCommandError
        raise ExternalCommandError(f"Cannot probe video dimensions: {path}")
    return width, height


def extract_frames(path: str, fps: float = SAMPLE_FPS) -> tuple[list[float], list]:
    """Decode sampled RGB frames. Returns (timestamps, frames as HxWx3 arrays)."""
    np = _numpy()
    width, height = probe_dimensions(path)
    cmd = [
        "ffmpeg", "-hide_banner", "-v", "error", "-i", path,
        "-vf", f"fps={fps}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0:
        from .errors import ExternalCommandError
        raise ExternalCommandError(f"Frame extraction failed for {path}")
    frame_bytes = width * height * 3
    raw = proc.stdout
    count = len(raw) // frame_bytes
    timestamps = [i / fps for i in range(count)]
    frames = [
        np.frombuffer(raw[i * frame_bytes:(i + 1) * frame_bytes], dtype=np.uint8).reshape(height, width, 3)
        for i in range(count)
    ]
    return timestamps, frames


def extract_audio_mono(path: str, sample_rate: int = AUDIO_SAMPLE_RATE):
    """Decode section audio to mono float32 PCM in [-1, 1]."""
    np = _numpy()
    cmd = [
        "ffmpeg", "-hide_banner", "-v", "error", "-i", path,
        "-ac", "1", "-ar", str(sample_rate),
        "-f", "f32le", "-acodec", "pcm_f32le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0 or not proc.stdout:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def vad_energy_curve(audio, sample_rate: int = AUDIO_SAMPLE_RATE, window_sec: float = 0.1,
                     method: str = "peak", noise_percentile: float = 30.0,
                     noise_ratio: float = 4.0):
    """Per-window RMS energy + adaptive voice-activity mask.

    ``method="peak"`` (legacy default): threshold = max(peak*0.15, 1e-4).
    ``method="noise_floor"``: threshold = noise_ratio * P30(energy), where
    the low percentile estimates the noise floor. A loud burst no longer
    masks quiet speech because the threshold tracks the floor, not the peak.
    Both methods short-circuit on near-digital silence.
    """
    np = _numpy()
    if len(audio) == 0:
        return [], []
    win = max(1, int(sample_rate * window_sec))
    n = len(audio) // win
    if n == 0:
        return [0.0], [0.0]
    frames = audio[: n * win].reshape(n, win)
    energy = [float(np.sqrt((frame ** 2).mean())) for frame in frames]
    peak = max(energy)
    if peak < 5e-4:
        # Near-digital silence overall: nothing is speech.
        return energy, [0.0] * len(energy)
    if method == "noise_floor":
        order = sorted(energy)
        rank = min(max(noise_percentile, 0.0), 100.0) / 100.0 * (len(order) - 1)
        lo, frac = int(rank), rank - int(rank)
        floor = order[lo] * (1.0 - frac) + order[min(lo + 1, len(order) - 1)] * frac
        threshold = max(floor * max(1.0, noise_ratio), 1e-4)
        # Guard against degenerate calibration: on clips with (almost) no
        # silence the percentile equals the signal itself, which would mute
        # everything. Never demand more than 90% of the peak.
        threshold = min(threshold, peak * 0.9)
    else:
        threshold = max(peak * 0.15, 1e-4)
    active = [1.0 if value >= threshold else 0.0 for value in energy]
    return energy, active


def smooth_vad_mask(active: list[float], window_sec: float = 0.1,
                    hangover_ms: int = 200, min_speech_ms: int = 150) -> list[float]:
    """Debounce a binary VAD mask (pure, no audio needed).

    Pass 1 drops runs shorter than min-speech as flicker blips; pass 2
    extends surviving runs forward (hangover bridges sub-200ms gaps from
    stop consonants / frame jitter, and never amplifies a lone blip into
    a keepable run). Idempotent on clean masks (all-on/all-off pass
    through unchanged).
    """
    if not active:
        return []
    # Millisecond-to-window conversion with ceil minus epsilon: float
    # division like 150/1000/0.1 yields 1.4999..., which round() would
    # wrongly collapse to 1.
    hangover = max(0, int(math.ceil(hangover_ms / 1000.0 / window_sec - 1e-9)))
    min_len = max(1, int(math.ceil(min_speech_ms / 1000.0 / window_sec - 1e-9)))
    n = len(active)
    # Pass 1: minimum speech duration.
    kept = [0.0] * n
    i = 0
    while i < n:
        if active[i] < 1.0:
            i += 1
            continue
        j = i
        while j < n and active[j] >= 1.0:
            j += 1
        if j - i >= min_len:
            for k in range(i, j):
                kept[k] = 1.0
        i = j
    # Pass 2: hangover extension of surviving runs only.
    out = list(kept)
    last_speech: int | None = None
    for i in range(n):
        if kept[i] >= 1.0:
            last_speech = i
        elif last_speech is not None and i - last_speech <= hangover:
            out[i] = 1.0
    return out


def vad_score_at(energy: list[float], active: list[float], start: float, end: float,
                 window_sec: float = 0.1) -> float:
    """Fraction of voice-active windows inside [start, end]."""
    if not active or end <= start:
        return 0.0
    lo = max(0, int(start / window_sec))
    hi = min(len(active), int(math.ceil(end / window_sec)))
    window = active[lo:hi]
    if not window:
        return 0.0
    return sum(window) / len(window)


def get_audio_speaking_score(energy: list[float], active: list[float],
                             start: float, end: float,
                             window_sec: float = 0.1) -> float:
    """Audio speaking evidence for a window (Phase 5 seam).

    Current implementation is energy-VAD alignment only: it answers
    "is there voice here", never "which face speaks". A diarization
    backend can replace this function without touching the target
    manager or the observation builders.
    """
    return vad_score_at(energy, active, start, end, window_sec=window_sec)


# --------------------------------------------------------------------------
# IoU tracking (stable speaker IDs across frames)
# --------------------------------------------------------------------------

def iou(box_a: tuple[float, float, float, float], box_b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    inter_x1, inter_y1 = max(ax1, bx1), max(ay1, by1)
    inter_x2, inter_y2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, inter_x2 - inter_x1) * max(0.0, inter_y2 - inter_y1)
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


@dataclass
class Track:
    speaker_id: str
    boxes: list[tuple[float, tuple[float, float, float, float]]] = field(default_factory=list)
    missed: int = 0
    confidence_sum: float = 0.0
    confidence_count: int = 0
    epoch: int = 0  # scene epoch: reacquire never glues IDs across a cut

    @property
    def confidence(self) -> float:
        return self.confidence_sum / max(1, self.confidence_count)

    @property
    def age_samples(self) -> int:
        """Number of detections ever attached (track age, Phase 1 metadata)."""
        return len(self.boxes)

    @property
    def last_seen(self) -> float | None:
        """Timestamp of the most recent detection, if any."""
        return self.boxes[-1][0] if self.boxes else None

    @property
    def born_at(self) -> float | None:
        """Timestamp of the first detection, if any."""
        return self.boxes[0][0] if self.boxes else None

    @property
    def lost_samples(self) -> int:
        """Consecutive samples without a matching detection."""
        return self.missed

    def box_at(self, timestamp: float) -> tuple[float, float, float, float] | None:
        """Nearest box at or before timestamp (sampled tracks are stepwise)."""
        best = None
        for time, box in self.boxes:
            if time <= timestamp + 1e-6:
                best = box
            else:
                break
        return best

    def center_at(self, timestamp: float) -> tuple[float, float] | None:
        box = self.box_at(timestamp)
        if box is None:
            return None
        x1, y1, x2, y2 = box
        return ((x1 + x2) / 2, (y1 + y2) / 2)

    def area_at(self, timestamp: float) -> float:
        box = self.box_at(timestamp)
        if box is None:
            return 0.0
        x1, y1, x2, y2 = box
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)


class IoUTracker:
    """Greedy IoU tracker: stable `speaker-NNN` IDs for face detections."""

    def __init__(self, iou_threshold: float = IOU_MATCH_THRESHOLD,
                 max_missed: int = MAX_MISSED_SAMPLES,
                 reacquire_missed: int = 12,
                 reacquire_iou: float = 0.5):
        self.iou_threshold = iou_threshold
        self.max_missed = max_missed
        self.reacquire_missed = reacquire_missed
        self.reacquire_iou = reacquire_iou
        self.tracks: list[Track] = []
        self.history: list[Track] = []
        self._next_id = 0
        self.epoch = 0

    def reset(self) -> None:
        """Scene-cut reset: drop active tracks, bump the identity epoch.

        History is kept for debugging, but reacquire() will never attach a
        new-scene detection to a pre-cut identity.
        """
        self.tracks = []
        self.epoch += 1

    def solid_tracks(self, min_boxes: int = 3) -> list[Track]:
        """Tracks with enough evidence; flicker detections are excluded."""
        return [track for track in self.history if len(track.boxes) >= min_boxes]

    def update(self, timestamp: float,
               detections: list[tuple[tuple[float, float, float, float], float]]) -> None:
        unmatched_tracks = list(self.tracks)
        for box, confidence in sorted(detections, key=lambda item: -item[1]):
            best, best_iou = None, self.iou_threshold
            for track in unmatched_tracks:
                last = track.boxes[-1][1] if track.boxes else None
                if last is None:
                    continue
                score = iou(last, box)
                if score >= best_iou:
                    best, best_iou = track, score
            if best is None:
                # Reacquire recently lost tracks (brief absence/turn-away)
                # instead of minting a new speaker identity. Same-epoch only:
                # a scene cut bumps the epoch so post-cut faces always get
                # fresh identities instead of gluing to a pre-cut track.
                for track in self.history:
                    if track.epoch != self.epoch:
                        continue
                    if track in unmatched_tracks or track.missed > self.reacquire_missed:
                        continue
                    last = track.boxes[-1][1] if track.boxes else None
                    if last is None:
                        continue
                    if iou(last, box) >= self.reacquire_iou:
                        best = track
                        break
                if best is None:
                    # Phase 4.2 lightweight association (§18): crossing /
                    # overlapping faces can drop IoU below the gate while
                    # still being the same person (nearby center, similar
                    # size). Conservative bounds so strangers never merge;
                    # nearest wins so crossings resolve deterministically.
                    nearest, nearest_dist = None, 0.12
                    for track in self.history:
                        if track.epoch != self.epoch:
                            continue
                        if (track in unmatched_tracks
                                or track.missed > self.reacquire_missed):
                            continue
                        last = track.boxes[-1][1] if track.boxes else None
                        if last is None or iou(last, box) < 0.2:
                            continue
                        unpredicted = track.center_at(timestamp)
                        cx = (box[0] + box[2]) / 2
                        cy = (box[1] + box[3]) / 2
                        if unpredicted is None:
                            continue
                        dist = ((unpredicted[0] - cx) ** 2
                                + (unpredicted[1] - cy) ** 2) ** 0.5
                        last_area = max(1e-9, (last[2] - last[0])
                                        * (last[3] - last[1]))
                        new_area = max(1e-9, (box[2] - box[0])
                                       * (box[3] - box[1]))
                        ratio = max(last_area, new_area) / min(
                            last_area, new_area)
                        if dist < nearest_dist and ratio < 1.5:
                            nearest, nearest_dist = track, dist
                    if nearest is not None:
                        best = nearest
            if best is None:
                track = Track(speaker_id=f"speaker-{self._next_id:03d}", epoch=self.epoch)
                self._next_id += 1
                self.tracks.append(track)
                self.history.append(track)
            else:
                track = best
                if track in unmatched_tracks:
                    unmatched_tracks.remove(track)
                elif track not in self.tracks:
                    self.tracks.append(track)
                track.missed = 0
            track.boxes.append((timestamp, box))
            track.confidence_sum += confidence
            track.confidence_count += 1
        for track in unmatched_tracks:
            track.missed += 1
        self.tracks = [track for track in self.tracks if track.missed <= self.max_missed]

    def active_at(self, timestamp: float) -> list[Track]:
        return [track for track in self.tracks if track.box_at(timestamp) is not None]


# --------------------------------------------------------------------------
# Face detection (MediaPipe tasks API, bundled .tflite) + mouth-motion voice
# cue (numpy only). Talking moves the mouth band more than the rest of the
# face; the lower/upper band motion ratio is the speaking signal, gated by
# audio voice activity. No GPU, no torch, fully offline after pip install.
# --------------------------------------------------------------------------

def mouth_motion_ratio(previous, current, box: tuple[float, float, float, float]) -> float:
    """Lower-face motion over upper-face motion for one face box.

    Both frames are HxWx3 uint8 arrays; box is normalized (x1, y1, x2, y2).
    Returns >= 0; ~1.0 for rigid head motion, higher while talking.
    """
    np = _numpy()
    height, width = previous.shape[:2]
    x1 = max(0, int(box[0] * width))
    y1 = max(0, int(box[1] * height))
    x2 = min(width, int(math.ceil(box[2] * width)))
    y2 = min(height, int(math.ceil(box[3] * height)))
    if x2 - x1 < 4 or y2 - y1 < 6:
        return 0.0
    face_prev = previous[y1:y2, x1:x2].astype(np.float32)
    face_curr = current[y1:y2, x1:x2].astype(np.float32)
    diff = np.abs(face_curr - face_prev).mean(axis=2)
    rows = diff.shape[0]
    upper = diff[: max(1, int(rows * 0.35))]
    lower = diff[min(rows - 1, int(rows * 0.55)):]
    upper_motion = float(upper.mean()) + 1e-6
    lower_motion = float(lower.mean())
    return lower_motion / upper_motion


def mouth_head_motion(previous, current,
                      box: tuple[float, float, float, float]
                      ) -> tuple[float, float]:
    """(mouth_ratio, head_motion) for one face box in a single pass.

    ``mouth_ratio`` is the same lower/upper band ratio as
    :func:`mouth_motion_ratio` (head turn ~= 1.0, talking higher).
    ``head_motion`` is the raw mean abs-diff of the UPPER band (0-255):
    pure head-turn context. Head motion never adds speech evidence — it
    only damps ambiguous mouth readings downstream. Small faces return
    (0.0, 0.0): no evidence invented.
    """
    np = _numpy()
    height, width = previous.shape[:2]
    x1 = max(0, int(box[0] * width))
    y1 = max(0, int(box[1] * height))
    x2 = min(width, int(math.ceil(box[2] * width)))
    y2 = min(height, int(math.ceil(box[3] * height)))
    if x2 - x1 < 4 or y2 - y1 < 6:
        return 0.0, 0.0
    face_prev = previous[y1:y2, x1:x2].astype(np.float32)
    face_curr = current[y1:y2, x1:x2].astype(np.float32)
    diff = np.abs(face_curr - face_prev).mean(axis=2)
    rows = diff.shape[0]
    upper = diff[: max(1, int(rows * 0.35))]
    lower = diff[min(rows - 1, int(rows * 0.55)):]
    upper_motion = float(upper.mean())
    lower_motion = float(lower.mean())
    return lower_motion / (upper_motion + 1e-6), upper_motion


def head_motion_score(now: float, baseline: float) -> float:
    """Normalized head-motion excess vs the track's own baseline (0-1).

    Relative (not absolute): 0 when still, ~1 when moving twice the usual.
    Exposure/compression invariant because it compares against itself.
    """
    if baseline <= 1e-9:
        return 0.0 if now <= 1e-9 else 1.0
    return max(0.0, min(1.0, (now - baseline) / (baseline + 1e-6)))


def _prioritize_detections(detections: list, tracks: list,
                           max_faces: int) -> list:
    """Cap detections to ``max_faces`` WITHOUT dropping the active target.

    Priority: (1) boxes overlapping an already-tracked face (the current
    target survives crowded frames), (2) detector confidence, (3) input
    order (stable sort: never invents order). Returns at most max_faces.
    """
    if len(detections) <= max_faces:
        return list(detections)
    scored = []
    for index, (box, confidence) in enumerate(detections):
        kept = False
        for track in tracks:
            boxes = track.boxes
            if not boxes:
                continue
            if iou(boxes[-1][1], box) >= 0.3:
                kept = True
                break
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            conf = 0.0
        scored.append((1 if kept else 0, conf, -index, box, confidence))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [(box, conf) for _, _, _, box, conf in scored[:max_faces]]


def lip_score_from_motion(ratio: float) -> float:
    """Map mouth-motion ratio to a 0-1 speaking signal."""
    return max(0.0, min(1.0, (ratio - 1.2) / 1.5))


class BlazeFaceDetector:
    """Thin wrapper reusing the tasks-API MediaPipeFaceDetector adapter."""

    def __init__(self, model_path: str | None = None, max_faces: int = MAX_FACES,
                 min_confidence: float = 0.4):
        from .vision_adapters import MediaPipeFaceDetector

        self._detector = MediaPipeFaceDetector(
            model_path=resolve_blaze_model(model_path),
            min_detection_confidence=min_confidence)
        self._max_faces = max_faces

    def detect(self, frame, timestamp: float = 0.0):
        """Return [(bbox_xyxy_normalized, confidence)] sorted by confidence."""
        observations = self._detector.detect(frame, timestamp)
        boxes = [
            ((item.face.x, item.face.y,
              item.face.x + item.face.width, item.face.y + item.face.height),
             item.confidence)
            for item in observations
        ]
        boxes.sort(key=lambda item: -item[1])
        return boxes[: self._max_faces]

    def close(self) -> None:
        try:
            self._detector._detector.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Section analysis -> observations -> timeline -> instructions
# --------------------------------------------------------------------------

@dataclass
class FramingPlan:
    """Everything render needs, plus debuggability."""

    instructions: list = field(default_factory=list)
    mode: str = "static"  # speaker-follow | two_speaker | group | static
    reason: str = ""
    tracks: list[Track] = field(default_factory=list)
    debug_log: str = ""
    scenes: list = field(default_factory=list)  # scene-cut timestamps (s)
    perf: dict = field(default_factory=dict)    # [FRAMING_PERF] counters
    trajectory: list = field(default_factory=list)  # TrajectoryPoint (debug)
    keyframes: list = field(default_factory=list)  # (t,cx,cy,w,h,scene) render
    keyframe_stats: dict = field(default_factory=dict)
    evidence_log: list = field(default_factory=list)  # per-window evidence rows
    # Phase 4.2 decision metrics (latency/false-switch/miss/composition).
    metrics: dict = field(default_factory=dict)


def _observations_from_tracks(
    tracks: list[Track],
    energy: list[float],
    active: list[float],
    lip_by_track: dict[str, list[tuple[float, float]]],
    start: float,
    end: float,
    step: float = 0.5,
    weights=None,
    spatial_enabled: bool = False,
    vad_window_sec: float = 0.1,
    face_quality_weight: float = 0.0,
    evidence_out: list | None = None,
    head_by_track: dict[str, list[tuple[float, float]]] | None = None,
    av_window_ms: int = 1200,
    av_min_samples: int = 3,
    av_neutral_floor: float = 0.25,
    mouth_windows: tuple[int, int, int] = (150, 500, 1000),
    mouth_weights: tuple[float, float, float] = (0.3, 0.5, 0.2),
    head_damp_strength: float = 0.3,
) -> list:
    """Build FaceObservation records per track per window.

    Phase 4.2 evidence (all normalized 0-1 BEFORE fusion):
    - ``lip_score``: multi-window fused mouth motion (SHORT reacts,
      MEDIUM decides, LONG vetoes blips), head-damped.
    - ``audio_score``: global VAD fraction SCALED by this face's
      audio-visual correlation — a loud room no longer hands every
      face the same 0.9 (spec §3). VAD=0 still gates everything to 0.
    - ``av_score``/``head_score``: associated per-face evidence.
    Continuity, quality, spatial: unchanged. [SPEAKER_SCORE] lines carry
    every component; ``evidence_out`` rows feed the debug artifact
    (bounded: one row per track per window).
    """
    from .speaker_score import (associated_audio_score, av_correlation,
                                continuity_from_boxes, damp_by_head_motion,
                                face_quality_score, fuse_mouth_motion,
                                head_motion_score, spatial_prior_for,
                                window_median)
    from .visual import BoundingBox, FaceObservation

    if weights is None:
        score_weights = None
    elif isinstance(weights, tuple):
        score_weights = weights
    else:
        score_weights = (weights.visual, weights.audio, weights.continuity)

    head_by_track = head_by_track or {}
    vad_series = [(index * vad_window_sec, value)
                  for index, value in enumerate(active)]
    # Per-track head baseline: median of the whole (bounded, in-RAM)
    # series — relative excess is exposure/compression invariant.
    head_baseline: dict[str, float] = {}
    for speaker, series in head_by_track.items():
        values = sorted(value for _, value in series)
        head_baseline[speaker] = (
            values[len(values) // 2] if values else 0.0)

    observations = []
    time = start
    while time < end:
        window_end = min(end, time + step)
        for track in tracks:
            # Skip tracks with no detection inside this window: box_at()
            # returns stale boxes, which would fabricate tied observations
            # for speakers that already left the frame.
            if not any(time <= stamp < window_end for stamp, _ in track.boxes):
                continue
            box = track.box_at(time + (window_end - time) / 2)
            if box is None:
                continue
            full_lip = [(stamp, score)
                        for stamp, score in lip_by_track.get(track.speaker_id, [])
                        if stamp <= window_end + 1e-9]
            short_ms, med_ms, long_ms = mouth_windows
            mouth_short = window_median(full_lip, window_end, short_ms)
            mouth_med = window_median(full_lip, window_end, med_ms)
            mouth_long = window_median(full_lip, window_end, long_ms)
            mouth_fused = fuse_mouth_motion(
                mouth_short, mouth_med, mouth_long, *mouth_weights)
            full_head = [(stamp, value)
                         for stamp, value in head_by_track.get(track.speaker_id, [])
                         if stamp <= window_end + 1e-9]
            head_now = window_median(full_head, window_end, med_ms)
            head_now = head_now if head_now is not None else 0.0
            excess = head_motion_score(
                head_now, head_baseline.get(track.speaker_id, 0.0))
            mouth_final, head_score = damp_by_head_motion(
                mouth_fused, excess, head_damp_strength)
            vad_frac = get_audio_speaking_score(energy, active, time, window_end,
                                                window_sec=vad_window_sec)
            av_corr = av_correlation(full_lip, vad_series,
                                     window_ms=av_window_ms,
                                     min_samples=av_min_samples)
            audio = associated_audio_score(vad_frac, av_corr, av_neutral_floor)
            # No-mouth cap (§22): a flat mouth with neutral-or-worse
            # correlation must not ride loud audio into a confident claim.
            # Silent faces stay below activation; the single-face case
            # still follows via decide_framing (presence, not speech).
            if mouth_fused < 0.1 and av_corr <= 0.55:
                audio = min(audio, 0.45)
            continuity = continuity_from_boxes(
                [stamp for stamp, _ in track.boxes],
                (time + window_end) / 2, expected_step=step)
            x1, y1, x2, y2 = box
            spatial = spatial_prior_for((x1 + x2) / 2, (y1 + y2) / 2,
                                        enabled=spatial_enabled)
            area = max(0.0, (x2 - x1) * (y2 - y1))
            edge_clip = max(0.0, -min(x1, y1)) + max(0.0, max(x2, y2) - 1.0)
            quality = face_quality_score(area, edge_clip, track.confidence)
            observation = FaceObservation(
                timestamp=round((time + window_end) / 2, 3),
                speaker_id=track.speaker_id,
                face=BoundingBox(
                    x=max(0.0, min(0.999, x1)), y=max(0.0, min(0.999, y1)),
                    width=max(0.01, min(1.0 - max(0.0, x1), x2 - x1)),
                    height=max(0.01, min(1.0 - max(0.0, y1), y2 - y1)),
                ),
                lip_score=round(mouth_final, 3),
                audio_score=round(audio, 3),
                continuity_score=round(continuity, 3),
                spatial_prior=round(spatial, 3),
                score_weights=score_weights,
                confidence=round(track.confidence, 3),
                quality_score=round(quality, 3),
                quality_weight=face_quality_weight,
                av_score=round(av_corr, 3),
                head_score=round(head_score, 3),
            )
            logger.debug(
                "[SPEAKER_SCORE] time=%.2f track=%s visual=%.2f audio=%.2f "
                "continuity=%.2f face_q=%.2f av=%.2f head=%.2f final=%.2f",
                observation.timestamp, track.speaker_id,
                observation.lip_score, observation.audio_score,
                observation.continuity_score, observation.confidence,
                observation.av_score, observation.head_score,
                observation.active_score,
            )
            # Phase 4 §37 canonical tag (same numbers, spec-required name).
            logger.debug(
                "[SPEAKER_EVIDENCE] track=%s visual=%.2f audio=%.2f "
                "continuity=%.2f quality=%.2f av=%.2f head=%.2f time=%.2f",
                track.speaker_id,
                observation.lip_score, observation.audio_score,
                observation.continuity_score, observation.quality_score,
                observation.av_score, observation.head_score,
                observation.timestamp,
            )
            if evidence_out is not None:
                evidence_out.append({
                    "timestamp": observation.timestamp,
                    "track_id": track.speaker_id,
                    "bbox": [round(x1, 4), round(y1, 4),
                             round(x2, 4), round(y2, 4)],
                    "mouth_score": round(mouth_final, 3),
                    "mouth_short": (round(mouth_short, 3)
                                    if mouth_short is not None else None),
                    "mouth_med": (round(mouth_med, 3)
                                  if mouth_med is not None else None),
                    "mouth_long": (round(mouth_long, 3)
                                   if mouth_long is not None else None),
                    "vad": round(vad_frac, 3),
                    "av_correlation": round(av_corr, 3),
                    "head_motion": round(head_score, 3),
                    "continuity": round(continuity, 3),
                    "face_quality": round(quality, 3),
                    "total_score": round(observation.active_score, 3),
                })
            observations.append(observation)
        time = round(time + step, 6)
    return observations


def smooth_centers(samples: list[tuple[float, str, float, float]],
                   alpha: float = SMOOTHING_ALPHA,
                   max_step: float = MAX_STEP,
                   min_movement: float = MIN_FRAMING_MOVEMENT,
                   ) -> list[tuple[float, str, float, float]]:
    """EMA smoothing with per-step clamp and micro-jitter suppression.

    - ``alpha``: EMA weight for new target (lower = smoother but more lag).
    - ``max_step``: hard cap on per-sample movement (prevents teleport jumps
      *within* a single speaker run).
    - ``min_movement``: ignore movements smaller than this (suppresses pixel
      noise from detection jitter without introducing perceptible lag).

    When the speaker changes, the position resets directly to the new
    speaker's first sample. This matches the behaviour of a real camera
    editor: cut to the new speaker, then smooth track their movement.
    max_step and min_movement apply only within the same-speaker run.
    """
    if not samples:
        return []
    smoothed = [samples[0]]
    prev_x, prev_y = samples[0][2], samples[0][3]
    prev_speaker = samples[0][1]
    for time, speaker, cx, cy in samples[1:]:
        if speaker != prev_speaker:
            # Speaker changed: cut directly to the new target.
            smoothed.append((time, speaker, cx, cy))
            prev_x, prev_y = cx, cy
            prev_speaker = speaker
            continue
        dx, dy = cx - prev_x, cy - prev_y
        # Suppress micro-movements within same-speaker run.
        if abs(dx) < min_movement and abs(dy) < min_movement:
            smoothed.append((time, speaker, prev_x, prev_y))
            continue
        nx = prev_x + alpha * dx
        ny = prev_y + alpha * dy
        nx = prev_x + max(-max_step, min(max_step, nx - prev_x))
        ny = prev_y + max(-max_step, min(max_step, ny - prev_y))
        smoothed.append((time, speaker, nx, ny))
        prev_x, prev_y = nx, ny
    return smoothed


def make_group_crop(faces: list[tuple[float, float, float, float]],
                    safe_margin: float = SAFE_MARGIN,
                    src_w: int = 1920, src_h: int = 1080):
    """One 9:16 window covering all given faces (fallback, no random pick).

    Phase 1: computed via framing_geometry.safe_group_crop so post-clamp
    faces are re-validated and the window widens instead of amputating.
    Same return type as before (CropInstruction, layout="group").
    """
    from .framing_geometry import safe_group_crop
    from .visual import CropInstruction

    window, report = safe_group_crop(
        faces, margin=safe_margin, out_w=1080, out_h=1920,
        src_w=src_w, src_h=src_h,
    )
    if any(value < 1.0 for value in report["visibility"]):
        logger.warning("[GROUP_CROP] reason=%s visibility=%s",
                       report["reason"],
                       [round(value, 2) for value in report["visibility"]])
    center_x, center_y = window.center
    return CropInstruction(
        start=0, end=1, speaker_id="group",
        center_x=center_x, center_y=center_y,
        width=window.width, height=window.height, layout="group",
    )


def decide_framing(tracks: list[Track], timeline: list,
                   duration: float,
                   speech_evidence: dict[str, float] | None = None,
                   overlap_fraction: float = 0.0,
                   group_min_active: int = 2,
                   group_min_confidence: float = 0.5,
                   group_activation_threshold: float = 0.5,
                   overlap_ambiguity_margin: float = 0.12) -> tuple[str, str]:
    """Pick follow/group/static with an explicit logged reason.

    GROUP fires only on simultaneous high evidence: at least
    ``group_min_active`` tracks each peaking >= ``group_min_confidence``,
    with mean-peak x overlap-fraction >= ``group_activation_threshold``.
    Sequential speakers (overlap ~0) stay SINGLE even when everyone is
    loud at some point. Without evidence (legacy callers), the old
    count-based rule applies. This keeps two adjacent faces from forcing
    GROUP when only one of them speaks.
    """
    if timeline:
        # Contested sustained overlap (both loud, thin margin, together
        # most of the time) is a GROUP moment, not a coin flip: following
        # either face alone would jitter or amputate the other.
        if speech_evidence is not None and overlap_fraction >= 0.5:
            ranked_peaks = sorted(speech_evidence.items(),
                                  key=lambda kv: (-kv[1], kv[0]))
            if (len(ranked_peaks) >= 2
                    and ranked_peaks[0][1] >= group_min_confidence
                    and ranked_peaks[1][1] >= group_min_confidence
                    and (ranked_peaks[0][1] - ranked_peaks[1][1])
                    < overlap_ambiguity_margin):
                return ("group",
                        f"contested overlap ({ranked_peaks[0][0]} vs "
                        f"{ranked_peaks[1][0]}, overlap {overlap_fraction:.2f})")
        speakers = sorted({segment.speaker_id for segment in timeline})
        return ("speaker-follow",
                f"active speaker timeline with {len(timeline)} segment(s): "
                f"{', '.join(speakers)}")
    if not tracks:
        return ("static", "no faces detected in section")
    if len(tracks) == 1:
        return ("speaker-follow",
                f"single face tracked ({tracks[0].speaker_id}), no confident speech")
    if speech_evidence is not None:
        scored = sorted(
            ((t.speaker_id, speech_evidence.get(t.speaker_id, 0.0)) for t in tracks),
            key=lambda kv: (-kv[1], kv[0]),
        )
        # Silence everywhere: no speech to attribute, keep the legacy
        # count rule (quiet group stays group). Gating engages only when
        # real speech evidence exists.
        if not scored or scored[0][1] < group_min_confidence:
            if len(tracks) >= 2:
                return ("group",
                        f"{len(tracks)} faces tracked without confident speech; group framing")
            leader = scored[0][0] if scored else tracks[0].speaker_id
            return ("speaker-follow",
                    f"single active speaker ({leader}), others quiet")
        active = [(tid, score) for tid, score in scored
                  if score >= group_min_confidence]
        if len(active) >= group_min_active:
            mean_peak = sum(s for _, s in active) / len(active)
            group_score = mean_peak * max(0.0, min(1.0, overlap_fraction))
            if group_score >= group_activation_threshold:
                return ("group",
                        f"{len(active)} simultaneously active speakers "
                        f"(group score {group_score:.2f})")
        leader = scored[0][0] if scored else tracks[0].speaker_id
        return ("speaker-follow",
                f"single active speaker ({leader}), others quiet")
    return ("group",
            f"{len(tracks)} faces tracked without confident speech; group framing")


# ---------------------------------------------------------------------------
# Phase 4.1: two-speaker co-framing (pair gating + composition anchor).
# A correct SINGLE always beats a bad pair shot: evidence, temporal and
# geometric gates must ALL pass; soft score only ranks survivors.
# ---------------------------------------------------------------------------

def _pair_coactivity(observations: list, id_a: str, id_b: str,
                     threshold: float = 0.4,
                     proximity_ms: int = 1000,
                     score_fn=None) -> dict:
    """Co-activity of a candidate pair from per-window observations.

    Returns active_ms per speaker, simultaneous overlap_ms, the longest
    bridged co-active run (gaps <= proximity_ms don't break a run —
    this is the exit-hold), and whether the pair ever converses
    (overlap or rapid alternation within proximity_ms).
    """
    step_ms = None
    score_of = (score_fn if score_fn is not None
                else (lambda o: o.active_score))
    times_a = sorted(o.timestamp for o in observations
                     if o.speaker_id == id_a and score_of(o) >= threshold)
    times_b = sorted(o.timestamp for o in observations
                     if o.speaker_id == id_b and score_of(o) >= threshold)
    if observations:
        stamps = sorted({o.timestamp for o in observations})
        if len(stamps) >= 2:
            step_ms = (stamps[-1] - stamps[0]) / (len(stamps) - 1) * 1000.0
    step_ms = step_ms or 100.0
    set_b = set(times_b)
    overlap_ms = sum(step_ms for t in times_a if t in set_b)
    # Proximity: closest approach between any A-active and B-active window.
    min_gap_ms = float("inf")
    for t in times_a:
        for u in times_b:
            gap = abs(t - u) * 1000.0
            if gap < min_gap_ms:
                min_gap_ms = gap
    # Longest bridged run over the union of active windows.
    union = sorted(set(times_a) | set(times_b))
    best_run_ms = 0.0
    run_start = None
    prev = None
    for t in union:
        if prev is None or (t - prev) * 1000.0 <= proximity_ms + 1e-9:
            run_start = t if run_start is None else run_start
        else:
            best_run_ms = max(best_run_ms, (prev - run_start) * 1000.0 + step_ms)
            run_start = t
        prev = t
    if run_start is not None and prev is not None:
        best_run_ms = max(best_run_ms, (prev - run_start) * 1000.0 + step_ms)
    converses = bool(times_a and times_b) and (
        overlap_ms > 0 or (min_gap_ms <= proximity_ms))
    return {
        "active_ms_a": len(times_a) * step_ms,
        "active_ms_b": len(times_b) * step_ms,
        "overlap_ms": overlap_ms,
        "min_gap_ms": None if min_gap_ms == float("inf") else round(min_gap_ms, 1),
        "bridged_run_ms": round(best_run_ms, 1),
        "converses": converses,
    }


def _pair_anchor_for(box_a, box_b,
                     headroom_factor: float = 0.18,
                     crop_height: float = 1.0) -> tuple[float, float]:
    """Per-sample composition anchor for a live pair: union-box center-x
    with the same face-top headroom rule as single anchors."""
    from .framing_geometry import compute_target_anchor

    union = (min(box_a[0], box_b[0]), min(box_a[1], box_b[1]),
             max(box_a[2], box_b[2]), max(box_a[3], box_b[3]))
    return compute_target_anchor(union, headroom_factor=headroom_factor,
                                 crop_height=crop_height)


def decide_two_speaker(tracks, observations,
                       evidence_peaks: dict,
                       src_w: int, src_h: int,
                       cfg, score_fn=None) -> tuple:
    """Section-level TWO_SPEAKER gate. Returns
    (accept, pair_ids|None, composition_result|None, qc dict).

    Gates in order: enabled → exactly-two loud → temporal proximity →
    geometric feasibility → score ≥ enter. Every rejection carries a
    specific §38 reason; accept needs score ≥ enter threshold.
    """
    from .framing_geometry import compute_two_speaker_composition

    qc = {"attempt": 0, "accept": 0, "rejects": {}}

    def _reject(reason: str, extra: dict | None = None):
        qc["rejects"][reason] = qc["rejects"].get(reason, 0) + 1
        logger.info("[TWO_SPEAKER_REJECT] reason=%s%s", reason,
                    f" {extra}" if extra else "")
        return False, None, None, qc

    if not cfg.two_speaker_enabled:
        return _reject("disabled")
    if not tracks or not observations:
        return _reject("no_temporal_overlap")
    loud = sorted(
        ((tid, score) for tid, score in evidence_peaks.items()
         if score >= cfg.group_min_confidence),
        key=lambda kv: (-kv[1], kv[0]),
    )
    if len(loud) != 2:
        # §30: 0/1 loud → single business; 3+ loud → GROUP business.
        return _reject("ambiguous_speech" if len(loud) > 2 else "weak_speaker_b",
                       {"loud": [tid for tid, _ in loud]})
    qc["attempt"] = 1
    (id_a, peak_a), (id_b, peak_b) = loud
    logger.info(
        "[TWO_SPEAKER_CANDIDATE] a=%s b=%s speaker_a=%.2f speaker_b=%.2f",
        id_a, id_b, peak_a, peak_b)
    coact = _pair_coactivity(
        observations, id_a, id_b, threshold=0.4,
        proximity_ms=cfg.two_speaker_temporal_overlap_ms,
        score_fn=score_fn)
    logger.info(
        "[TWO_SPEAKER_CANDIDATE] temporal overlap_ms=%.0f bridged_run_ms=%.0f "
        "min_gap_ms=%s converses=%s",
        coact["overlap_ms"], coact["bridged_run_ms"],
        coact["min_gap_ms"], coact["converses"])
    if not coact["converses"]:
        return _reject("no_temporal_overlap")
    if coact["bridged_run_ms"] < cfg.two_speaker_min_duration_ms:
        return _reject("no_temporal_overlap",
                       {"bridged_run_ms": coact["bridged_run_ms"]})
    track_a = next((t for t in tracks if t.speaker_id == id_a), None)
    track_b = next((t for t in tracks if t.speaker_id == id_b), None)
    if track_a is None or track_b is None:
        return _reject("speaker_invisible")
    mid = max((o.timestamp for o in observations)) / 2 if observations else 0.0
    box_a = track_a.box_at(mid)
    box_b = track_b.box_at(mid)
    if box_a is None or box_b is None:
        return _reject("speaker_invisible")
    result = compute_two_speaker_composition(
        box_a, box_b, src_w, src_h,
        out_w=cfg.output_width, out_h=cfg.output_height,
        max_distance=cfg.two_speaker_max_distance,
        min_face_width=cfg.min_two_speaker_face_width,
        min_face_height=cfg.min_two_speaker_face_height,
        min_edge_margin=cfg.min_two_speaker_edge_margin,
        max_upscale=cfg.two_speaker_max_upscale,
        max_crop_width=cfg.two_speaker_max_crop_width,
        max_empty_space=cfg.two_speaker_max_empty_space,
        max_cutoff=cfg.two_speaker_max_cutoff,
        max_gap_ratio=cfg.two_speaker_max_gap_ratio,
        visibility_threshold=cfg.two_speaker_speaker_visibility_threshold,
        min_score=cfg.two_speaker_min_score,
        movement_penalty=cfg.two_speaker_movement_penalty,
        empty_space_penalty=cfg.two_speaker_empty_space_penalty,
        face_size_penalty=cfg.two_speaker_face_size_penalty,
    )
    logger.info(
        "[TWO_SPEAKER_COMPOSITION] a=%s b=%s feasible=%s face_a=%s face_b=%s "
        "empty_space=%s cutoff=%s upscale=%s score=%.2f reason=%s",
        id_a, id_b, result.feasible,
        result.metrics.get("face_size_a"), result.metrics.get("face_size_b"),
        result.metrics.get("empty_space_ratio"), result.metrics.get("cutoff_ratio"),
        result.metrics.get("required_upscale"), result.confidence, result.reason)
    if not result.feasible:
        return _reject(result.reason)
    if result.confidence < cfg.two_speaker_enter_threshold:
        return _reject("score_below_threshold", {"score": result.confidence})
    qc["accept"] = 1
    logger.info("[TWO_SPEAKER_ACCEPT] a=%s b=%s score=%.2f candidate=%s",
                id_a, id_b, result.confidence,
                result.metrics.get("candidate"))
    logger.info("[TARGET_DECISION] mode=TWO_SPEAKER tracks=[%s,%s] score=%.2f",
                id_a, id_b, result.confidence)
    return True, (id_a, id_b), result, qc


def dump_active_speaker_log(timestamp: float, tracks: list[Track],
                            timeline: list, selected: str | None,
                            confidence: float | None,
                            crop: tuple[float, float, float, float] | None) -> str:
    """Developer-facing [ACTIVE SPEAKER] debug block."""
    lines = ["[ACTIVE SPEAKER]", f"timestamp: {timestamp:.2f}s",
             f"persons_detected: {len(tracks)}", ""]
    for index, track in enumerate(tracks):
        box = track.box_at(timestamp)
        segment = next(
            (item for item in timeline if item.start <= timestamp < item.end), None,
        )
        score = segment.confidence if segment and segment.speaker_id == track.speaker_id else 0.0
        lines.append(f"person_{index}:")
        lines.append(f"  bbox: {box}")
        lines.append(f"  tracking_id: {track.speaker_id}")
        lines.append(f"  speaking_score: {round(score, 3)}")
        lines.append("")
    lines.append(f"selected_speaker: {selected}")
    lines.append(f"confidence: {round(confidence, 3) if confidence is not None else None}")
    lines.append("")
    lines.append("target_crop:")
    if crop is None:
        lines.append("  none (static center fallback)")
    else:
        x, y, width, height = crop
        lines.append(f"  x: {x:.3f}")
        lines.append(f"  y: {y:.3f}")
        lines.append(f"  width: {width:.3f}")
        lines.append(f"  height: {height:.3f}")
    return "\n".join(lines)


def merge_trajectory(samples: list[tuple[float, str, float, float]],
                     scene_ids: list[int] | None = None,
                     max_position_delta: float = 0.03,
                     max_time_gap: float = 0.75,
                     sample_step: float = 0.5,
                     ) -> list[tuple[float, float, str, float, float]]:
    """Adaptive merge: collapse stable runs, keep waypoints for movers.

    A run accumulates while the speaker is unchanged, the scene is
    unchanged, samples stay contiguous, and displacement from the run
    anchor stays within ``max_position_delta`` (Euclidean, normalized
    units). Each flushed run emits ``(start, end, speaker, cx, cy)`` using
    the settled last-sample position.

    Stable person -> one instruction (same as the old collapse, but now a
    deliberate decision). Moving person -> waypoint instructions so the
    renderer follows the motion instead of freezing at the endpoint.
    """
    if not samples:
        return []
    if scene_ids is None:
        scene_ids = [0] * len(samples)
    merged: list[tuple[float, float, str, float, float]] = []
    run: list[int] = []
    anchor = (samples[0][2], samples[0][3])

    def flush(current: list[int]) -> None:
        if not current:
            return
        first = samples[current[0]]
        last = samples[current[-1]]
        merged.append((first[0], last[0] + sample_step, first[1],
                       last[2], last[3]))

    for index, (time, speaker, cx, cy) in enumerate(samples):
        if run:
            first = samples[run[0]]
            gap = time - samples[run[-1]][0]
            moved = math.hypot(cx - anchor[0], cy - anchor[1])
            if (speaker != first[1] or scene_ids[index] != scene_ids[run[0]]
                    or gap > max_time_gap or moved > max_position_delta):
                flush(run)
                run = [index]
                anchor = (cx, cy)
            else:
                run.append(index)
        else:
            run = [index]
            anchor = (cx, cy)
    flush(run)
    return merged


def guard_instruction(instruction, faces: list, src_w: int, src_h: int,
                      out_w: int = 1080, out_h: int = 1920,
                      max_upscale: float = 2.0,
                      visibility_min: float = 0.85) -> tuple[bool, str]:
    """Quality guard for one CropInstruction. Returns (keep, reason).

    Invalid geometry (bounds/aspect/zero-size) is rejected so the caller
    falls back to a safe static crop. Visibility/upscale shortfalls on an
    already-maximal window are kept but reported (Phase 1 has no zoom
    control yet; faking a fix would be worse than logging the truth).
    """
    from .framing_geometry import CropWindow, validate_crop

    window = CropWindow(
        x=instruction.center_x - instruction.width / 2,
        y=instruction.center_y - instruction.height / 2,
        width=instruction.width, height=instruction.height,
    )
    ok, reason, suggestion = validate_crop(
        window, src_w, src_h, out_w, out_h,
        faces=faces or None, max_upscale=max_upscale,
        visibility_min=visibility_min,
    )
    if ok:
        return True, "ok"
    if suggestion == "fallback":
        logger.warning("[QUALITY_GUARD] reject crop cx=%.3f cy=%.3f: %s",
                       instruction.center_x, instruction.center_y, reason)
        return False, reason
    logger.warning("[QUALITY_GUARD] keep maximal crop with known limit: %s", reason)
    return True, f"kept-with-limit: {reason}"


def _recenter_on_faces(cx: float, faces: list,
                       crop_width: float, safe_margin: float) -> float:
    """Shift crop center-x toward faces when coverage is poor.

    Triggers only when the worst-covered face is <50% visible; the move
    applies only if it strictly improves that worst case, so legitimate
    compositions are never touched and coverage can never get worse.
    Vertical is deliberately untouched (headroom rule owns cy).
    """
    from .framing_geometry import CropWindow, face_visibility

    if not faces or crop_width <= 0:
        return cx

    def worst(center_x: float) -> float:
        window = CropWindow(x=center_x - crop_width / 2, y=0.0,
                            width=crop_width, height=1.0)
        return min(face_visibility(box, window) for box in faces)

    if worst(cx) >= 0.5:
        return cx
    fx = sum((box[0] + box[2]) / 2 for box in faces) / len(faces)
    nx = min(1 - safe_margin - crop_width / 2,
             max(safe_margin + crop_width / 2, fx))
    if worst(nx) > worst(cx):
        logger.debug("[GUARD_RECENTER] cx=%.3f->%.3f worst_vis=%.2f->%.2f",
                     cx, nx, worst(cx), worst(nx))
        return nx
    return cx


def _format_key(key) -> str:
    if key is None:
        return "none"
    return f"e{key[0]}:{key[1]}"


def _log_target_event(event) -> None:
    """Render one TargetManager event as a structured log line."""
    time, kind, detail = event.time, event.kind, event.detail
    if kind == "SWITCH":
        logger.info(
            "[TARGET_SWITCH] time=%.2f from=%s to=%s from_score=%.2f "
            "to_score=%.2f reason=%s",
            time, _format_key(detail.get("from")), _format_key(detail.get("to")),
            detail.get("from_score", 0.0), detail.get("to_score", 0.0),
            detail.get("reason", ""),
        )
    elif kind == "LOST":
        logger.info("[TARGET_LOST] time=%.2f track=%s state=OCCLUDED",
                    time, _format_key(detail.get("track")))
    elif kind in ("GRACE_EXPIRED", "FALLBACK"):
        logger.info("[TARGET_FALLBACK] time=%.2f from=%s reason=%s fallback=%s",
                    time, _format_key(detail.get("from") or detail.get("to")),
                    detail.get("reason", "grace_expired"),
                    _format_key(detail.get("to")) if kind == "FALLBACK" else "static")
    elif kind == "CANDIDATE":
        logger.debug(
            "[TARGET_DECISION] time=%.2f state=CANDIDATE current=%s "
            "candidate=%s current_score=%.2f candidate_score=%.2f "
            "margin=%.2f decision=WAIT_HOLD",
            time, _format_key(detail.get("from")), _format_key(detail.get("to")),
            detail.get("current_score", 0.0), detail.get("candidate_score", 0.0),
            detail.get("margin", 0.0),
        )
    elif kind == "MARGIN_HOLD":
        logger.debug(
            "[TARGET_HOLD] time=%.2f track=%s reason=%s margin=%.2f",
            time, _format_key(detail.get("current")),
            "transition_locked" if detail.get("locked") else "margin_hold",
            detail.get("margin", 0.0),
        )
    elif kind == "CANCEL":
        logger.debug("[TARGET_DECISION] time=%.2f decision=CANCELLED candidate=%s",
                     time, _format_key(detail.get("candidate")))
    elif kind == "RESTORE":
        logger.debug("[TARGET_DECISION] time=%.2f decision=RESTORED to=%s",
                     time, _format_key(detail.get("to")))
    elif kind == "ACQUIRE":
        logger.debug("[TARGET_DECISION] time=%.2f state=TRACKING decision=ACQUIRE to=%s",
                     time, _format_key(detail.get("to")))
    elif kind == "SCENE_RESET":
        logger.debug("[TARGET_DECISION] time=%.2f decision=SCENE_RESET scene=%s",
                     time, detail.get("scene"))
    elif kind == "LOCK":
        logger.info("[TARGET_LOCK] time=%.2f track=%s until=%.2fs",
                    time, _format_key(detail.get("track")),
                    detail.get("until", 0.0))
    elif kind == "ASSOCIATED":
        logger.info("[TARGET_ACQUIRE] time=%.2fs track=%s associated_from=%s "
                    "reason=spatial_reacquire",
                    time, _format_key(detail.get("to")),
                    _format_key(detail.get("from")))
    if kind == "GRACE_EXPIRED":
        # Occlusion duration tiers: strong <300ms, fading <700ms, gone after.
        held_ms = detail.get("held_ms", 0)
        tier = ("strong" if held_ms <= 300
                else "fading" if held_ms <= 700 else "expired")
        logger.info("[OCCLUSION] time=%.2f track=%s duration_ms=%d tier=%s",
                    time, _format_key(detail.get("from")), held_ms, tier)


def _render_follow_samples(samples: list[tuple[float, str, float, float]],
                           sample_scenes: list[int],
                           anchors: list,
                           ease_marks: set,
                           faces_for,
                           layout: str,
                           src_w: int, src_h: int,
                           crop_width: float, crop_height: float,
                           rung_w: int, rung_h: int,
                           duration: float, cfg,
                           kf_stats: dict, guard_notes: list) -> tuple:
    """Shared Phase 3 render tail for follow modes (SINGLE / TWO_SPEAKER).

    ``samples`` are (time, label, cx, cy) anchor measurements; the single
    primary trajectory filter (Kalman per contiguous run), deadzone,
    velocity/acceleration limits, easing subdivision and quality guard
    run identically for both modes. ``faces_for(label, start, end)``
    supplies guard faces; ``    layout`` labels the built instructions.
    Returns (trajectory, instructions, keyframes). ``kf_stats``/``guard_notes``
    are mutated in place.
    """
    from .framing_config import use_hard_cuts

    hard_cut = use_hard_cuts(cfg)
    if hard_cut:
        logger.debug("[TRANSITION_STYLE] cut: easing/deadzone-exit/switch pans disabled")
    # Phase 3 temporal filter: Kalman per (speaker, scene)-contiguous
    # run (cut semantics preserved via per-run reset). This is the
    # single primary trajectory filter; the legacy center-EMA utility
    # (smooth_centers) is retained for tests, not stacked after this.
    from .smoothing import (filter_run, split_contiguous,
                            subdivide_eased_runs)

    trajectory = []
    filtered_samples: list[tuple[float, str, float, float]] = []
    filtered_scenes: list[int] = []
    for run, run_scene in split_contiguous(samples, sample_scenes):
        for point in filter_run(
                run, scene_id=run_scene,
                process_noise=cfg.kalman_process_noise,
                measurement_noise=cfg.kalman_measurement_noise,
                velocity_decay=cfg.kalman_velocity_decay,
                scale=crop_height):
            trajectory.append(point)
            filtered_samples.append(
                (point.timestamp, point.track_id, point.x, point.y))
            filtered_scenes.append(point.scene_id)
    # Phase 4: target-crop validation. Single-target samples must stay
    # near their anchor; drift means the camera lost its target.
    anchor_by_time = {t: (label, ax, ay) for t, label, ax, ay in anchors}
    target_errors: list[float] = []
    mismatch_count = 0
    for point in trajectory:
        anchor = anchor_by_time.get(round(point.timestamp, 3))
        if anchor is None or anchor[0] in ("static",):
            continue
        error = ((point.x - anchor[1]) ** 2 + (point.y - anchor[2]) ** 2) ** 0.5
        target_errors.append(error)
        if cfg.center_gap_protection and error > cfg.target_crop_error_threshold:
            mismatch_count += 1
            if mismatch_count <= 5:
                logger.warning(
                    "[CROP_TARGET_MISMATCH] time=%.2f target=%s error=%.3f",
                    point.timestamp, anchor[0], error)
    if target_errors:
        ordered_errors = sorted(target_errors)
        p95 = ordered_errors[min(len(ordered_errors) - 1,
                                 int(len(ordered_errors) * 0.95))]
        mean_err = sum(target_errors) / len(target_errors)
        kf_stats["target_lock_accuracy"] = round(
            sum(1 for e in target_errors
                if e <= cfg.target_crop_error_threshold) / len(target_errors), 3)
        logger.info(
            "[TARGET_CROP] mean_err=%.3f max_err=%.3f p95=%.3f mismatches=%d",
            round(mean_err, 3), round(ordered_errors[-1], 3),
            round(p95, 3), mismatch_count)
    else:
        kf_stats["target_lock_accuracy"] = 1.0
    # Ease marks force chunk boundaries so acquisitions restore as
    # eased pans; everything else merges exactly as before.
    mark_set = sorted(ease_marks)
    chunked: list[tuple[list, list, bool]] = []
    cur_samp: list = []
    cur_scen: list = []
    cur_marked = False
    for sample, sid in zip(filtered_samples, filtered_scenes):
        if cur_samp and round(sample[0], 3) in mark_set:
            chunked.append((cur_samp, cur_scen, cur_marked))
            cur_samp, cur_scen, cur_marked = [], [], True
        cur_samp.append(sample)
        cur_scen.append(sid)
    if cur_samp:
        chunked.append((cur_samp, cur_scen, cur_marked))
    if not chunked:
        chunked = [([], [], False)]
    # Adaptive merge: stable runs collapse (cheap), movers keep
    # waypoints (no more frozen walkers). Scene changes force splits.
    merged_all: list[tuple[float, float, str, float, float]] = []
    eased_starts: set[float] = set()
    for chunk_samp, chunk_scen, marked in chunked:
        if not chunk_samp:
            continue
        merged_chunk = merge_trajectory(
            chunk_samp, chunk_scen,
            max_position_delta=cfg.max_merge_position_delta,
            max_time_gap=cfg.max_merge_time_gap,
            sample_step=cfg.trajectory_sample_sec,
        )
        if marked and merged_chunk:
            eased_starts.add(merged_chunk[0][0])
        merged_all.extend(merged_chunk)
    smoothed_by_time = {round(p.timestamp, 3): (p.x, p.y)
                        for p in trajectory}
    merged, eased_spans = subdivide_eased_runs(
        merged_all, eased_starts, smoothed_by_time,
        easing_s=0.0 if hard_cut else cfg.easing_ms / 1000.0,
        waypoint_step=cfg.easing_waypoint_sec,
        min_distance=cfg.min_framing_movement,
    )
    if eased_spans:
        for point in trajectory:
            if any(s0 - 1e-9 <= point.timestamp <= s1 + 1e-9
                   for s0, s1 in eased_spans):
                point.eased = True
    # QUALITY path keyframes: deadzone + limits on the Kalman points
    # (legacy merge above keeps consuming the un-gated sequence, so
    # fallback behavior is bit-identical), sizes from merged runs,
    # eased transitions at same-scene label changes.
    from .smoothing import apply_deadzone, build_keyframes, limit_motion

    raw_seq = [(p.timestamp, p.x, p.y) for p in trajectory]
    raw_labels = [p.track_id for p in trajectory]
    raw_scenes = [p.scene_id for p in trajectory]
    gated_seq, n_held = apply_deadzone(
        raw_seq, raw_labels, raw_scenes,
        deadzone_x=cfg.framing_deadzone_x, deadzone_y=cfg.framing_deadzone_y,
        exit_ease_s=0.0 if hard_cut else cfg.framing_deadzone_exit_ease_s)
    limited_seq, n_trips = limit_motion(
        gated_seq, raw_labels, raw_scenes,
        max_velocity=cfg.max_crop_velocity, max_acceleration=cfg.max_crop_acceleration)
    kf_stats["deadzone_holds"] = n_held
    kf_stats["limit_trips"] = n_trips
    limited_points = [(t, x, y, label, scene)
                      for (t, x, y), label, scene
                      in zip(limited_seq, raw_labels, raw_scenes)]
    keyframes, built_stats = build_keyframes(
        limited_points,
        [(0.0, duration, crop_width, crop_height)],
        (crop_width, crop_height),
        transition_ms=cfg.switch_transition_ms,
        transition_steps=cfg.switch_transition_steps,
        min_scale_change=cfg.min_scale_change,
        jump_threshold=cfg.jump_threshold,
        hard_cut=hard_cut,
    )
    kf_stats.update(built_stats)
    kf_stats["deadzone_holds"] = n_held
    kf_stats["limit_trips"] = n_trips
    for switch in kf_stats.get("switches", []):
        logger.info(
            "[FRAMING_SWITCH] time=%.2f from=%s to=%s distance=%.3f "
            "transition_ms=%d",
            switch["time"], switch["from_label"], switch["to_label"],
            switch["distance"], switch["transition_ms"],
        )
    if kf_stats.get("jumps"):
        logger.warning("[FRAMING_JUMP] count=%d (teleport-class events)",
                       kf_stats["jumps"])
    logger.info(
        "[TRAJECTORY] samples=%d duration=%.1f interpolated_frames=%d "
        "max_velocity=%.3f max_acceleration=%.3f deadzone_holds=%d "
        "limit_trips=%d transitions=%d jumps=%d",
        len(trajectory), duration, len(keyframes),
        kf_stats.get("max_velocity", 0.0),
        kf_stats.get("max_acceleration", 0.0),
        n_held, n_trips, kf_stats.get("transitions", 0),
        kf_stats.get("jumps", 0),
    )
    from .visual import CropInstruction
    instructions: list = []
    for start, end, speaker, cx, cy in merged:
        cx = min(1 - cfg.safe_margin - crop_width / 2,
                 max(cfg.safe_margin + crop_width / 2, cx))
        cy = min(1 - crop_height / 2, max(crop_height / 2, cy))
        faces = list(faces_for(speaker, start, end))
        if faces:
            # A stale anchor can park the crop where its own face is
            # mostly outside. Shift horizontally toward the faces (never
            # worse than current) before the guard sees it; cy keeps the
            # deliberate headroom rule untouched.
            cx = _recenter_on_faces(cx, faces, crop_width, cfg.safe_margin)
        instruction = CropInstruction(
            start=start, end=min(end, duration), speaker_id=speaker,
            center_x=cx, center_y=cy, width=crop_width, height=crop_height,
            layout=layout,
        )
        keep, note = guard_instruction(
            instruction, faces, src_w, src_h,
            out_w=rung_w, out_h=rung_h,
            max_upscale=cfg.max_upscale_factor,
            visibility_min=cfg.face_visibility_min,
        ) if cfg.quality_guard_enabled else (True, "guard-disabled")
        if not keep:
            guard_notes.append(f"[{start:.1f}-{end:.1f}] {note}")
            continue
        if note != "ok":
            guard_notes.append(f"[{start:.1f}-{end:.1f}] {note}")
        instructions.append(instruction)
    return trajectory, instructions, keyframes


def _relabel_single_context(instructions: list, solid, cfg) -> None:
    """Relabel SINGLE → SINGLE_WITH_CONTEXT when a nearby silent person
    is genuinely inside the single crop (§23, §32).

    Pure relabel (no geometry change): the speaker stays the target; the
    layout only records that context rides along. A face counts as
    context when ≥50% visible — well below the speaker bar, so this
    never promotes a bystander to target.
    """
    if not instructions or not solid:
        return
    from .framing_geometry import CropWindow, face_visibility

    for instruction in instructions:
        if getattr(instruction, "layout", "single") != "single":
            continue
        if instruction.speaker_id == "static":
            continue
        window = CropWindow(
            x=instruction.center_x - instruction.width / 2,
            y=instruction.center_y - instruction.height / 2,
            width=instruction.width, height=instruction.height,
        )
        mid = min((instruction.start + instruction.end) / 2, instruction.end)
        for track in solid:
            if track.speaker_id == instruction.speaker_id:
                continue
            box = track.box_at(mid)
            if box is not None and face_visibility(box, window) >= 0.5:
                try:
                    instruction.layout = "single_with_context"
                except Exception:
                    pass
                break


def _nearest_known_face(solid, timestamp: float, scene_of=None):
    """Temporally-nearest detected face (past OR future) in the section.

    Offline analysis has seen the whole tape, so a gap with no live
    detection (profile views, motion blur) can still anchor on the
    nearest known position instead of parking on empty space (§21, §24).
    Returns (speaker_id, box) or None when the section holds no faces.
    Deterministic: nearest time, then largest area, then lowest id.

    When ``scene_of`` is given, only boxes inside the SAME camera shot
    are eligible: a box 10s away past a cut belongs to another angle
    and must never anchor this shot.
    """
    if scene_of is not None:
        try:
            want = scene_of(timestamp)
        except Exception:
            want = None
    else:
        want = None
    best = None
    for track in solid:
        for stamp, box in track.boxes:
            if want is not None:
                try:
                    if scene_of(stamp) != want:
                        continue
                except Exception:
                    pass
            gap = abs(stamp - timestamp)
            area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
            key = (gap, -area, track.speaker_id)
            if best is None or key < best[0]:
                best = (key, track.speaker_id, box)
    if best is None:
        return None
    return best[1], best[2]


def _has_past_boxes(solid, timestamp: float) -> bool:
    """Whether any solid track detected a face at or before ``timestamp``.

    Distinguishes "target never acquired yet" (lookahead allowed) from
    "tracked target lost" (bounded grace → static, Phase 2 semantics).
    """
    return any(stamp <= timestamp + 1e-6
               for track in solid for stamp, _ in track.boxes)


def _has_future_boxes(solid, timestamp: float, scene_of=None) -> bool:
    """Whether any solid track detects a face strictly after ``timestamp``
    (same camera shot when ``scene_of`` is given).

    A gap BRIDGED by a returning face is detector blindness (profile,
    occlusion, blur) — not departure — so the person stays framed
    through it. No future + past-only means the person truly left:
    static fallback stands.
    """
    if scene_of is not None:
        try:
            want = scene_of(timestamp)
        except Exception:
            want = None
    else:
        want = None
    for track in solid:
        for stamp, _ in track.boxes:
            if stamp <= timestamp + 1e-6:
                continue
            if want is not None:
                try:
                    if scene_of(stamp) != want:
                        continue
                except Exception:
                    pass
            return True
    return False


def _backfill_leading_static(samples: list, anchors: list) -> int:
    """Pre-position a leading static run on the first confirmed target.

    In cut mode there is no easing pan to smooth a "center, then shift"
    opening: the first frames would flash an arbitrary center before the
    first hard cut. Copying the first non-static center backward removes
    the flash without touching timing. Labels are kept as-is (downstream
    grouping/validation treat "static" as absence, which stays true).
    Returns the number of backfilled samples.
    """
    first_live = None
    for time, label, cx, cy in samples:
        if label != "static":
            first_live = (cx, cy)
            break
    if first_live is None:
        return 0
    fixed = 0
    for i, (time, label, cx, cy) in enumerate(samples):
        if label != "static":
            break
        samples[i] = (time, label, first_live[0], first_live[1])
        fixed += 1
    for i, (time, label, ax, ay) in enumerate(anchors):
        if label != "static" or i >= fixed:
            break
        anchors[i] = (time, label, round(first_live[0], 4), round(first_live[1], 4))
    return fixed


def _enforce_person_presence(instructions: list, solid,
                             crop_width: float, crop_height: float,
                             cfg, guard_notes: list,
                             scene_of=None) -> int:
    """Hard §21 rule: no final crop may be person-free while the section
    holds faces. A crop showing no face above
    ``person_presence_min_visibility`` is recentered on the nearest known
    face (same clamp as every other instruction). Returns corrected count.
    Split panes already show people and are left alone.

    Like the sampling-loop lookahead, correction skips a lost target's
    bounded static fallback (Phase 2): past faces with no return.
    A gap bridged by a returning face is blindness, not departure.
    """
    if not instructions or not solid:
        return 0
    from .framing_geometry import (CropWindow, compute_target_anchor,
                                   face_visibility)

    corrected = 0
    for instruction in instructions:
        if getattr(instruction, "layout", "split") == "split":
            continue
        window = CropWindow(
            x=instruction.center_x - instruction.width / 2,
            y=instruction.center_y - instruction.height / 2,
            width=instruction.width, height=instruction.height,
        )
        mid = min((instruction.start + instruction.end) / 2, instruction.end)
        if (_has_past_boxes(solid, mid)
                and not _has_future_boxes(solid, mid, scene_of)):
            continue
        holds_person = False
        # Freshness matters: box_at() is timeless, so a box from long ago
        # must not certify "person present" for a crop showing a wall.
        # Only boxes detected within anchor_stale_after_ms count.
        stale_s = cfg.anchor_stale_after_ms / 1000.0
        for track in solid:
            box = track.box_at(mid)
            if box is None:
                continue
            latest = max((stamp for stamp, _ in track.boxes
                          if stamp <= mid + 1e-6), default=None)
            if latest is None or (mid - latest) > stale_s + 1e-9:
                continue
            if face_visibility(box, window) >= cfg.person_presence_min_visibility:
                holds_person = True
                break
        if holds_person:
            continue
        found = _nearest_known_face(solid, mid, scene_of=scene_of)
        if found is None:
            continue
        tid, box = found
        cx, cy = compute_target_anchor(
            box, headroom_factor=cfg.headroom_factor,
            crop_height=crop_height)
        cx = min(1 - cfg.safe_margin - crop_width / 2,
                 max(cfg.safe_margin + crop_width / 2, cx))
        cy = min(1 - crop_height / 2, max(crop_height / 2, cy))
        logger.warning(
            "[PERSON_FREE_CORRECTED] %.1f-%.1fs from=%s to=%s cx=%.3f->%.3f",
            instruction.start, instruction.end, instruction.speaker_id,
            tid, instruction.center_x, cx)
        instruction.speaker_id = tid
        instruction.center_x = cx
        instruction.center_y = cy
        guard_notes.append(
            f"[{instruction.start:.1f}-{instruction.end:.1f}] "
            f"person-free crop recentered on {tid}")
        corrected += 1
    return corrected


def _talknet_escalate_windows(observations, tracks_by_id, video_path,
                             audio, duration, cfg, detector, evidence_rows,
                             src_w=0, src_h=0, acquire_failed=False) -> dict:
    """Heuristic-first TalkNet escalation for hard windows (§10-11).

    Groups observations per window, applies decide_escalation on HEURISTIC
    scores, and scores only triggered windows (bounded batches, cached).
    Attaches ``obs.talknet_score`` in place; the SAME Target Manager then
    consumes fused scores. Returns a small summary dict. Never raises:
    every failure degrades to heuristic-only.
    """
    from .talknet.fusion import decide_escalation

    summary: dict = {"windows": 0, "escalated": 0, "reasons": {},
                     "scored_faces": 0}
    if detector is None:
        return summary
    tn_cfg = detector.config
    try:
        eff = tn_cfg.effective()
    except Exception:
        eff = {"escalation_stride": 1, "max_faces_per_window": 6}
    stride = max(1, int(eff.get("escalation_stride", 1)))
    max_faces = max(1, int(eff.get("max_faces_per_window", 6)))
    by_window: dict[float, list] = {}
    for obs in observations:
        by_window.setdefault(round(obs.timestamp, 3), []).append(obs)
    load_ok: bool | None = None
    for index, stamp in enumerate(sorted(by_window)):
        if index % stride:
            continue
        window_obs = by_window[stamp]
        if len(window_obs) < 2:
            continue
        ranked = sorted(((o.active_score, o.speaker_id) for o in window_obs),
                        key=lambda kv: (-kv[0], kv[1]))
        best, second = ranked[0][0], ranked[1][0]
        strong_mouth = sum(1 for o in window_obs if o.lip_score > 0.6)
        overlap = sum(1 for o in window_obs if o.active_score >= 0.5) >= 2
        escalate, reason = decide_escalation(
            len(window_obs), best, second,
            strong_mouth_count=strong_mouth, overlap=overlap,
            new_onset=False, target_lost=False,
            acquire_failed=acquire_failed,
            trigger_margin=tn_cfg.trigger_margin,
            trigger_confidence=tn_cfg.trigger_confidence,
            min_people=tn_cfg.min_people)
        if not escalate:
            continue
        if load_ok is None:
            load_ok = detector.ensure_ready()
            if load_ok:
                try:
                    from .talknet.model import get_model, resolve_model_path

                    path = resolve_model_path(detector.config.model_path)
                    if path is not None and detector.device_info is not None:
                        get_model(path, detector.device_info.device,
                                  detector.config.cpu_threads).warmup()
                except Exception as exc:
                    logger.warning("[TALKNET_FALLBACK] reason=warmup_failed: %s", exc)
        if not load_ok:
            return summary
        summary["windows"] += 1
        summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1
        tracks = [tracks_by_id[sid] for _, sid in ranked[:max_faces]
                  if sid in tracks_by_id]
        if not tracks:
            continue
        wstart = max(0.0, stamp - tn_cfg.window_ms / 2000.0)
        wend = min(duration, stamp + tn_cfg.window_ms / 2000.0)
        try:
            result = detector.score_faces(
                video_path, tracks, wstart, wend, audio, 16000,
                src_w, src_h, reason=reason)
        except Exception as exc:
            logger.warning("[TALKNET_FALLBACK] reason=score_failed: %s", exc)
            continue
        summary["escalated"] += 1
        summary["scored_faces"] += result.faces_scored
        if result.device.startswith("cuda"):
            detector.metrics["gpu_count"] += 0  # counted at init; kept explicit
        by_track = {s.track_id: s for s in result.scores}
        for obs in window_obs:
            hit = by_track.get(obs.speaker_id)
            if hit is None:
                continue
            try:
                obs.talknet_score = max(0.0, min(1.0, float(hit.score)))
            except (TypeError, ValueError):
                continue
            for row in evidence_rows:
                if (row.get("track_id") == obs.speaker_id
                        and abs(row.get("timestamp", -1) - obs.timestamp) < 1e-6):
                    row["talknet_score"] = obs.talknet_score
                    break
    if summary["escalated"]:
        detector.metrics["trigger_count"] += summary["escalated"]
        logger.info("[TALKNET_TRIGGER] windows=%d reasons=%s",
                    summary["escalated"], summary["reasons"])
    try:
        detector.trigger_log.append(summary)
    except Exception:
        pass
    return summary


def analyze_section(video_path: str, duration: float,
                    sample_fps: float | None = None,
                    frame_source=None,
                    config=None,
                    talknet_config=None) -> FramingPlan:
    """Full auto-framing analysis for one clip section.

    Raises only on unusable input (missing file, no decodable stream);
    returns a static-mode plan (with reason) whenever vision is uncertain.

    ``frame_source`` (optional iterable of ``(timestamp, frame)``) injects
    frames for tests; otherwise a bounded-memory ffmpeg stream at the
    configured analysis fps/resolution is used. Frames are released after
    processing (only a 1-frame buffer is ever held). ``sample_fps``
    overrides the configured analysis fps when given. No input CFR
    conversion is performed; the ``fps`` filter normalizes sampling and
    ``index/fps`` timestamps are valid for that sampled stream.

    ``talknet_config`` (None = disabled): TalkNet escalation for hard
    windows. Heuristic always runs first; TalkNet only scores ambiguous /
    crowded windows and fuses into the SAME Target Manager pipeline.
    """
    import time as _time

    from .errors import ExternalCommandError
    from .framing_config import FramingConfig, resolve_analysis_profile
    from .framing_stream import AnalysisFrame, SceneCutDetector, iter_analysis_frames
    from .visual import build_speaker_timeline, make_crop_instructions

    cfg = config or FramingConfig.from_env()
    fps, analysis_width, adaptive = resolve_analysis_profile(
        sample_fps, None, cfg)
    analysis_start = _time.monotonic()

    # Soft RAM guard: if the process is already over budget, analyze at the
    # floor rate instead of failing. Never aborts analysis.
    from .framing_stream import rss_mb

    memory_guard = False
    if cfg.max_analysis_memory_mb > 0:
        current_rss = rss_mb()
        if current_rss > cfg.max_analysis_memory_mb:
            fps = min(fps, cfg.analysis_fps_min)
            memory_guard = True
            logger.warning(
                "[FRAMING_PERF] memory guard: rss=%.0fMB over budget %.0fMB; "
                "analysis fps dropped to %g",
                current_rss, cfg.max_analysis_memory_mb, fps)

    # Probe actual source dimensions so we can compute source-aspect-aware
    # crop fractions. Detection boxes are normalized (0-1), so analysis
    # resolution is transparent to tracking/composition.
    # On failure (test stubs, missing file) fall back to assuming a standard
    # 1920×1080 landscape source; the renderer re-probes anyway.
    try:
        src_w, src_h = probe_dimensions(video_path)
    except Exception:
        src_w, src_h = 1920, 1080

    from .media import probe_fps

    _r_fps, _avg_fps, _vfr = probe_fps(video_path)
    logger.info(
        "[MEDIA_SOURCE] width=%d height=%d fps_r=%s fps_avg=%s vfr=%s "
        "duration=%.1f",
        src_w, src_h,
        f"{_r_fps:.3f}" if _r_fps else "unknown",
        f"{_avg_fps:.3f}" if _avg_fps else "unknown",
        _vfr, duration,
    )
    logger.info(
        "[FRAMING_CONFIG] analysis_fps=%g analysis_width=%d "
        "output=%dx%d output_fps_mode=%s deadzone=%.3f/%.3f "
        "vmax=%.2f amax=%.2f transition_ms=%d",
        fps, analysis_width, cfg.output_width, cfg.output_height,
        cfg.output_fps_mode, cfg.framing_deadzone_x, cfg.framing_deadzone_y,
        cfg.max_crop_velocity, cfg.max_crop_acceleration,
        cfg.switch_transition_ms,
    )

    if frame_source is None:
        feed = iter_analysis_frames(video_path, fps=fps,
                                    max_width=analysis_width)
        live_stream = True
    else:
        feed = iter(frame_source)
        live_stream = False

    audio = extract_audio_mono(video_path)
    energy, active = vad_energy_curve(
        audio, method=cfg.vad_method,
        noise_percentile=cfg.vad_noise_percentile,
        noise_ratio=cfg.vad_noise_ratio,
    )
    active = smooth_vad_mask(
        active, window_sec=cfg.vad_window_sec,
        hangover_ms=cfg.vad_hangover_ms,
        min_speech_ms=cfg.vad_min_speech_ms,
    )

    detector = BlazeFaceDetector(
        max_faces=cfg.max_faces,
        min_confidence=cfg.face_detection_min_confidence)
    tracker = IoUTracker(
        iou_threshold=cfg.iou_match_threshold,
        max_missed=cfg.max_missed_samples,
        reacquire_missed=cfg.reacquire_missed,
        reacquire_iou=cfg.reacquire_iou,
    )
    scene = SceneCutDetector(
        threshold=cfg.scene_cut_threshold,
        min_interval_ms=cfg.scene_cut_min_interval_ms,
        thumb_width=cfg.scene_cut_thumb_width,
    )
    # Mouth motion needs consecutive frames: single-frame buffer only.
    # The adaptive sampler gates DETECTION only (scene-cut + buffer update
    # run on every decoded frame); injected test sources always analyze.
    from .framing_stream import AdaptiveSampler

    allow_stride2 = adaptive and fps / 2.0 >= cfg.analysis_fps_min - 1e-9
    sampler = AdaptiveSampler(
        still_threshold=cfg.still_threshold,
        still_windows=cfg.still_windows,
        max_stride=2 if allow_stride2 else 1,
    ) if live_stream else None
    motion_by_track: dict[str, list[tuple[float, float]]] = {}
    scene_id = 0
    scene_times: list[float] = []
    frames_analyzed = 0
    frames_decoded = 0
    produced_any = False
    try:
        previous_frame = None
        for item in feed:
            # Streaming seam: AnalysisFrame objects from the live pipe,
            # plain (timestamp, frame) tuples from injected test sources.
            if isinstance(item, AnalysisFrame):
                stamp, frame = item.timestamp, item.frame
            else:
                stamp, frame = item
            produced_any = True
            frames_decoded += 1
            if cfg.scene_cut_enabled:
                # Phase 1 effect: reset tracker + smoothing history so the
                # old scene's crop is never carried over blindly.
                is_cut, scene_score = scene.update(frame, stamp)
                if is_cut:
                    tracker.reset()
                    if sampler is not None:
                        sampler.reset()
                    scene_id += 1
                    scene_times.append(round(stamp, 3))
            else:
                # Cuts disabled: still score inter-frame motion for the
                # adaptive sampler (stateless diff, no cut state).
                from .framing_stream import thumb_diff

                scene_score = thumb_diff(previous_frame, frame,
                                         cfg.scene_cut_thumb_width) \
                    if previous_frame is not None else 0.0
            analyze_this = sampler.update(scene_score) if sampler is not None else True
            if not analyze_this:
                previous_frame = frame
                continue
            frames_analyzed += 1
            detections = detector.detect(frame)
            # Crowded-scene prioritization: the tracked target survives
            # the detector cap instead of losing to higher-confidence
            # strangers (spec §16-17). Stable, deterministic.
            detections = _prioritize_detections(
                list(detections), tracker.tracks, cfg.max_faces)
            tracker.update(stamp, detections)
            if previous_frame is not None:
                for box, _ in detections:
                    best, best_iou = None, 0.3
                    for track in tracker.tracks:
                        track_box = track.box_at(stamp)
                        if track_box is None:
                            continue
                        score = iou(track_box, box)
                        if score >= best_iou:
                            best, best_iou = track, score
                    if best is not None:
                        ratio, head = mouth_head_motion(
                            previous_frame, frame, box)
                        motion_by_track.setdefault(
                            best.speaker_id, []).append((stamp, ratio, head))
            previous_frame = frame
    finally:
        detector.close()
    if not produced_any:
        raise ExternalCommandError(f"No decodable frames in {video_path}")

    lip_by_track: dict[str, list[tuple[float, float]]] = {
        speaker: [(stamp, lip_score_from_motion(ratio))
                  for stamp, ratio, _head in series]
        for speaker, series in motion_by_track.items()
    }
    # Raw upper-band motion per track (head-turn context; normalized
    # against each track's own baseline downstream, never a vote).
    head_by_track: dict[str, list[tuple[float, float]]] = {
        speaker: [(stamp, head) for stamp, _ratio, head in series]
        for speaker, series in motion_by_track.items()
    }
    # Samples are already normalized 0-1 speaking signals (not raw openness).

    solid = tracker.solid_tracks()
    from .speaker_score import ScoreWeights

    weights = ScoreWeights(
        visual=cfg.speaker_weight_visual,
        audio=cfg.speaker_weight_audio,
        continuity=cfg.speaker_weight_continuity,
    )
    evidence_rows: list = []
    anchors: list = []
    snapshots: dict = {}
    observations = _observations_from_tracks(
        tracker.history, energy, active, lip_by_track, 0.0, duration,
        weights=weights, spatial_enabled=cfg.speaker_spatial,
        vad_window_sec=cfg.vad_window_sec,
        face_quality_weight=cfg.face_quality_weight,
        evidence_out=evidence_rows,
        head_by_track=head_by_track,
        av_window_ms=cfg.av_window_ms,
        av_min_samples=cfg.av_min_samples,
        av_neutral_floor=cfg.av_neutral_floor,
        mouth_windows=(cfg.mouth_short_ms, cfg.mouth_medium_ms,
                       cfg.mouth_long_ms),
        mouth_weights=(cfg.mouth_w_short, cfg.mouth_w_medium,
                       cfg.mouth_w_long),
        head_damp_strength=cfg.head_damp_strength,
    )
    # Phase TalkNet: heuristic-first escalation (Q1 only). The fused
    # scorer below is the SOLE TalkNet touchpoint for the timeline,
    # peaks, and pair co-activity; Target Manager, geometry, smoothing
    # and rendering never see TalkNet directly.
    tracks_by_id = {track.speaker_id: track for track in tracker.history}
    talknet_summary: dict = {"enabled": False}
    talknet_detector = None

    def _final_score(obs):
        talknet_value = getattr(obs, "talknet_score", None)
        if talknet_value is None or talknet_detector is None:
            return obs.active_score
        return talknet_detector.fused_score(obs)

    if talknet_config is not None and getattr(talknet_config, "enabled", False):
        from .talknet.detector import TalkNetDetector

        talknet_detector = TalkNetDetector(talknet_config)
        _talknet_escalate_windows(
            observations, tracks_by_id, video_path, audio,
            duration, cfg, talknet_detector, evidence_rows,
            src_w=src_w, src_h=src_h)
    else:
        talknet_detector = None
    timeline_metrics: dict = {}

    def _run_timeline(extra_metrics: dict | None = None) -> list:
        metrics_target = extra_metrics if extra_metrics is not None else timeline_metrics
        return build_speaker_timeline(
            observations, duration,
            min_switch_seconds=cfg.switch_hysteresis_sec,
            min_active_score=cfg.min_active_score,
            min_hold_seconds=cfg.min_speaker_hold_ms / 1000.0,
            min_switch_interval_seconds=cfg.min_speaker_switch_interval_ms / 1000.0,
            track_lost_grace_seconds=cfg.track_lost_grace_ms / 1000.0,
            no_speaker_fallback_seconds=cfg.no_speaker_fallback_ms / 1000.0,
            force_switch_margin=cfg.force_switch_score_margin,
            activation_threshold=cfg.speaker_activation_threshold,
            switch_threshold=cfg.speaker_switch_threshold,
            score_smoothing_alpha=cfg.score_smoothing_alpha,
            switch_margin=cfg.switch_margin,
            ambiguity_margin=cfg.overlap_ambiguity_margin,
            onset_bonus=cfg.speaker_onset_bonus,
            onset_window_ms=cfg.speaker_onset_window_ms,
            onset_rise_min=cfg.speaker_onset_rise_min,
            continuity_bonus=cfg.continuity_bonus,
            continuity_keep_fraction=cfg.continuity_keep_fraction,
            unseen_decay=cfg.score_unseen_decay,
            onset_fast_hold_ms=cfg.onset_fast_hold_ms,
            onset_switch_margin=cfg.onset_switch_margin,
            movement_weight=cfg.switch_movement_weight,
            movement_full_at=cfg.switch_movement_full_at,
            visibility_weight=cfg.switch_visibility_weight,
            metrics_out=metrics_target,
            score_fn=(_final_score if talknet_detector is not None else None),
        )

    def _timeline_products():
        # Section-level speech evidence for the GROUP gate: per-track peak
        # fused score plus the fraction of speech windows with >=2 tracks
        # simultaneously above confidence (true overlap, not sequential).
        peaks: dict[str, float] = {}
        for obs in observations:
            scored = _final_score(obs)
            if scored > peaks.get(obs.speaker_id, 0.0):
                peaks[obs.speaker_id] = scored
        speech = 0
        overlap = 0
        times: list[float] = []
        grouped: dict[float, list] = {}
        for obs in observations:
            grouped.setdefault(round(obs.timestamp, 3), []).append(obs)
        for stamp, window_obs in grouped.items():
            loud = sum(1 for o in window_obs
                       if _final_score(o) >= cfg.group_min_confidence)
            if loud >= 1:
                speech += 1
                times.append(stamp)
            if loud >= 2:
                overlap += 1
        return peaks, grouped, speech, overlap, times

    timeline = _run_timeline()
    # Section-level speech evidence for the GROUP gate: per-track peak
    # fused score plus the fraction of speech windows with >=2 tracks
    # simultaneously above confidence (true overlap, not sequential).
    evidence_peaks, by_window, speech_windows, overlap_windows, speech_times = \
        _timeline_products()
    overlap_fraction = (overlap_windows / speech_windows) if speech_windows else 0.0
    # Second pass (§10 J): acquisition failed (no/weak segments) despite
    # speech -> escalate every speech window, then rebuild once. Bounded:
    # at most one rebuild per section.
    if (talknet_detector is not None
            and speech_windows > 0
            and (not timeline
                 or max((s.confidence for s in timeline), default=0.0) < 0.4)):
        _talknet_escalate_windows(
            observations, tracks_by_id, video_path, audio,
            duration, cfg, talknet_detector, evidence_rows,
            src_w=src_w, src_h=src_h, acquire_failed=True)
        second_metrics: dict = {}
        timeline = _run_timeline(second_metrics)
        for key, value in second_metrics.items():
            if key == "switches" and isinstance(value, list):
                timeline_metrics.setdefault("switches", []).extend(value)
            elif key not in timeline_metrics:
                timeline_metrics[key] = value
        (evidence_peaks, by_window, speech_windows,
         overlap_windows, speech_times) = _timeline_products()
        overlap_fraction = (overlap_windows / speech_windows) if speech_windows else 0.0
        logger.info("[TALKNET_TRIGGER] second_pass acquire_failed rebuilt=%d",
                    len(timeline))
    # Phase 4.1: TWO_SPEAKER gate runs BEFORE the single/group decision.
    # Accept needs exactly-two loud + temporal conversing + feasible
    # geometry + score ≥ enter; any rejection falls through to the
    # legacy path bit-identically (no behavior change when rejected).
    two_accept, two_pair, two_comp, two_qc = decide_two_speaker(
        solid, observations, evidence_peaks, src_w, src_h, cfg,
        score_fn=(_final_score if talknet_detector is not None else None))
    if two_accept:
        mode = "two_speaker"
        reason = (f"conversational pair {two_pair[0]}+{two_pair[1]} "
                  f"(score {two_comp.confidence:.2f}, "
                  f"{two_comp.metrics.get('candidate')})")
        timeline = []  # pair owns the section; no single timeline to follow
    else:
        two_pair, two_comp = None, None
        mode, reason = decide_framing(
            solid, timeline, duration,
            speech_evidence=evidence_peaks or None,
            overlap_fraction=overlap_fraction,
            group_min_active=cfg.group_min_active_speakers,
            group_min_confidence=cfg.group_min_confidence,
            group_activation_threshold=cfg.group_activation_threshold,
            overlap_ambiguity_margin=cfg.overlap_ambiguity_margin,
        )
    logger.info(
        "[GROUP_DECISION] mode=%s overlap=%.2f peaks=%s reason=%s",
        mode, round(overlap_fraction, 3),
        {tid: round(score, 2) for tid, score in sorted(evidence_peaks.items())},
        reason,
    )

    def scene_of(timestamp: float) -> int:
        """Scene epoch owning ``timestamp`` (cuts recorded in scene_times)."""
        epoch = 0
        for cut in scene_times:
            if timestamp >= cut:
                epoch += 1
            else:
                break
        return epoch

    # Correct source-aware portrait fractions (largest fitting 9:16 window).
    # crop_width covers (9/16)*(src_h/src_w) of the source width; height then
    # fills the frame for landscape (== 1.0) — never the old square-ish math.
    from .framing_geometry import portrait_window_size

    crop_w_px, crop_h_px = portrait_window_size(
        src_w, src_h, cfg.output_width, cfg.output_height)
    crop_width = crop_w_px / src_w
    crop_height = crop_h_px / src_h

    from .media import compute_vertical_ladder

    try:
        _, _, _, _, rung_w, rung_h = compute_vertical_ladder(src_w, src_h)
    except Exception:
        rung_w, rung_h = cfg.output_width, cfg.output_height

    instructions: list = []
    guard_notes: list[str] = []
    trajectory: list = []
    keyframes: list = []
    kf_stats: dict = {"transitions": 0, "jumps": 0, "size_steps": 0,
                      "switches": [], "max_velocity": 0.0,
                      "max_acceleration": 0.0,
                      "deadzone_holds": 0, "limit_trips": 0}
    # Phase 4.1 §40 QC counters exist on EVERY section (accept or reject).
    kf_stats["two_speaker_attempt_count"] = two_qc.get("attempt", 0)
    kf_stats["two_speaker_accept_count"] = 1 if mode == "two_speaker" else 0
    kf_stats["two_speaker_reject_count"] = (
        0 if mode == "two_speaker"
        else sum((two_qc.get("rejects", {}) or {}).values()))
    for _reason, _count in (two_qc.get("rejects", {}) or {}).items():
        kf_stats[f"two_speaker_reject_{_reason}"] = _count
    if mode == "two_speaker" and two_pair is not None:
        # Phase 4.1 render: per-step composition anchor from LIVE boxes
        # through the shared Phase 3 tail (Kalman → deadzone → limits →
        # easing → guard). No new smoothing is stacked; visibility still
        # beats smoothness (a rejected pair never reaches this branch).
        from .framing_geometry import compute_target_anchor as _cta

        id_a, id_b = two_pair
        track_a = next((t for t in solid if t.speaker_id == id_a), None)
        track_b = next((t for t in solid if t.speaker_id == id_b), None)
        pair_label = f"{id_a}+{id_b}"
        step = cfg.trajectory_sample_sec
        samples = []
        sample_scenes = []
        pair_valid_steps = 0
        pair_total_steps = 0
        pair_separated = False
        pair_exit_count = 0
        # Phase 4.2 conversation lock (§31-32): after a drifted exit the
        # pair may not re-form for two_speaker_lock_ms (no SINGLE->TWO
        # flap), and once the running valid-ratio sinks below the exit
        # threshold the pair is retired for the rest of the section.
        pair_cooldown_until = -1.0
        pair_retired = False
        pair_lock_holds = 0
        last_anchor = ((two_comp.center_x, two_comp.center_y)
                       if two_comp is not None else (0.5, 0.5))
        time = 0.0
        while time < duration:
            fresh = {}
            for tid, track in ((id_a, track_a), (id_b, track_b)):
                if track is None:
                    continue
                hits = [(stamp, box) for stamp, box in track.boxes
                        if time - step - 1e-6 <= stamp <= time + 1e-6]
                if hits:
                    fresh[tid] = hits[-1][1]
            pair_total_steps += 1
            speaker_label = pair_label
            if id_a in fresh and id_b in fresh:
                ax = (fresh[id_a][0] + fresh[id_a][2]) / 2
                bx = (fresh[id_b][0] + fresh[id_b][2]) / 2
                _drifted = abs(ax - bx) > cfg.two_speaker_max_distance
                _locked = time < pair_cooldown_until
                if _drifted or pair_retired or _locked:
                    # Drifted apart mid-section: the union midpoint would
                    # be a gap shot. Exit to the nearer face (continuity:
                    # no jump) and relabel so the handover is explicit.
                    # A fresh exit arms the conversation lock (§32): the
                    # pair may not re-form for two_speaker_lock_ms (no
                    # SINGLE->TWO flap). A retired pair never re-forms.
                    if not pair_separated:
                        _why = ("drifted_apart" if _drifted
                                else ("retired" if pair_retired else "lock_hold"))
                        logger.info(
                            "[TWO_SPEAKER_EXIT] time=%.2f pair=%s "
                            "reason=%s distance=%.3f",
                            time, pair_label, _why, round(abs(ax - bx), 3))
                        pair_exit_count += 1
                        if _drifted and not pair_retired:
                            pair_cooldown_until = (
                                time + cfg.two_speaker_lock_ms / 1000.0)
                            pair_lock_holds += 1
                    pair_separated = True
                    nearer = (id_a if abs(ax - last_anchor[0])
                              <= abs(bx - last_anchor[0]) else id_b)
                    speaker_label = nearer
                    box = fresh[nearer]
                    center = _cta(box, headroom_factor=cfg.headroom_factor,
                                  crop_height=crop_height)
                    last_anchor = center
                else:
                    if pair_separated:
                        logger.info(
                            "[TWO_SPEAKER_HOLD] time=%.2f pair=%s "
                            "reason=conversation_reformed",
                            time, pair_label)
                    pair_separated = False
                    center = _pair_anchor_for(
                        fresh[id_a], fresh[id_b],
                        headroom_factor=cfg.headroom_factor,
                        crop_height=crop_height)
                    last_anchor = center
                    pair_valid_steps += 1
                    # Running exit check (§31): a pair that is mostly
                    # invalid stops being a pair (single leader wins).
                    if (not pair_retired and time >= 2.0
                            and pair_valid_steps / max(1, pair_total_steps)
                            < cfg.two_speaker_exit_threshold):
                        pair_retired = True
                        logger.info(
                            "[TWO_SPEAKER_EXIT] time=%.2f pair=%s "
                            "reason=below_exit_threshold ratio=%.2f",
                            time, pair_label,
                            round(pair_valid_steps / max(1, pair_total_steps), 2))
            elif fresh:
                only_id = next(iter(fresh))
                speaker_label = only_id
                only = fresh[only_id]
                center = _cta(only, headroom_factor=cfg.headroom_factor,
                              crop_height=crop_height)
                last_anchor = center
            else:
                # Nobody fresh (brief detector loss): nearest known face
                # instead of freezing on possibly-empty space (§21).
                found = _nearest_known_face(solid, time, scene_of=scene_of)
                if found is not None:
                    _, box = found
                    center = _cta(box, headroom_factor=cfg.headroom_factor,
                                  crop_height=crop_height)
                    last_anchor = center
                else:
                    center = last_anchor
            samples.append((round(time, 3), speaker_label, center[0], center[1]))
            sample_scenes.append(scene_of(time))
            anchors.append((round(time, 3), speaker_label,
                            round(center[0], 4), round(center[1], 4)))
            snapshots[round(time, 3)] = ("TRACKING", speaker_label, None)
            time = round(time + step, 6)

        def _pair_faces_for(speaker, start, end):
            mid = min((start + end) / 2, duration - 1e-3)
            out = []
            wanted = (track_a, track_b) if speaker == pair_label else tuple(
                t for t in (track_a, track_b)
                if t is not None and t.speaker_id == speaker)
            for track in wanted:
                if track is not None:
                    box = track.box_at(mid)
                    if box is not None:
                        out.append(box)
            return out

        trajectory, instructions, keyframes = _render_follow_samples(
            samples, sample_scenes, anchors, set(),
            faces_for=_pair_faces_for, layout="two_speaker",
            src_w=src_w, src_h=src_h,
            crop_width=crop_width, crop_height=crop_height,
            rung_w=rung_w, rung_h=rung_h,
            duration=duration, cfg=cfg,
            kf_stats=kf_stats, guard_notes=guard_notes,
        )
        metrics = two_comp.metrics if two_comp is not None else {}
        face_sizes = [metrics.get("face_size_a", (0, 0)),
                      metrics.get("face_size_b", (0, 0))]
        kf_stats["two_speaker_duration_ms"] = int(duration * 1000)
        kf_stats["two_speaker_pair_valid_ratio"] = round(
            pair_valid_steps / max(1, pair_total_steps), 3)
        kf_stats["two_speaker_speaker_visibility"] = round(
            min(metrics.get("speaker_visibility_a", 0.0),
                metrics.get("speaker_visibility_b", 0.0)), 3)
        kf_stats["two_speaker_empty_space"] = metrics.get("empty_space_ratio", 0.0)
        kf_stats["two_speaker_cutoff"] = metrics.get("cutoff_ratio", 0.0)
        kf_stats["two_speaker_min_face_size"] = round(
            min([w for w, _ in face_sizes] + [0.0]), 4)
        kf_stats["two_speaker_switch_count"] = max(0, len(instructions) - 1)
        kf_stats["two_speaker_jitter_count"] = 0
        kf_stats["two_speaker_exit_count"] = pair_exit_count
        kf_stats["two_speaker_lock_holds"] = pair_lock_holds
        kf_stats["two_speaker_retired"] = pair_retired
        logger.info(
            "[TWO_SPEAKER_QC] accept=1 pair_valid=%.2f visibility=%.2f "
            "empty=%.2f cutoff=%.2f min_face=%.3f",
            kf_stats["two_speaker_pair_valid_ratio"],
            kf_stats["two_speaker_speaker_visibility"],
            kf_stats["two_speaker_empty_space"],
            kf_stats["two_speaker_cutoff"],
            kf_stats["two_speaker_min_face_size"],
        )
    elif mode == "speaker-follow" and timeline:
        # Normalize overlapping hysteresis segments into a partition:
        # each speaker owns [start, next.start). The hold period already
        # delayed the switch decision, so rendering follows reality.
        ordered = sorted(timeline, key=lambda item: item.start)
        edges = [
            (segment, segment.start,
             min(segment.end, ordered[index + 1].start if index + 1 < len(ordered) else duration))
            for index, segment in enumerate(ordered)
        ]
        from .framing_geometry import compute_target_anchor
        from .target_manager import STATIC_CENTER, TargetManager

        manager = TargetManager(
            switch_margin=cfg.switch_margin,
            lost_grace_s=cfg.track_lost_grace_ms / 1000.0,
            transition_lock_ms=cfg.target_transition_lock_ms,
            force_margin=cfg.force_switch_score_margin,
            assoc_max_gap_ms=cfg.assoc_max_gap_ms,
            assoc_max_dist=cfg.assoc_max_dist,
            assoc_max_size_ratio=cfg.assoc_max_size_ratio,
            assoc_spatial_weight=cfg.spatial_continuity_weight,
        )
        step = cfg.trajectory_sample_sec
        samples: list[tuple[float, str, float, float]] = []
        sample_scenes: list[int] = []
        ease_marks: set[float] = set()
        time = 0.0
        while time < duration:
            segment = next(
                (item for item, start, end in edges if start <= time < end),
                ordered[0],
            )
            track = next(
                (item for item in solid if item.speaker_id == segment.speaker_id), None,
            )
            seg_key = (track.epoch, segment.speaker_id) if track is not None else (tracker.epoch, segment.speaker_id)
            # Fresh presence: solid tracks with a detection in this window,
            # keyed by (epoch, id). Anchor is the formal target anchor
            # (face-top weighted, headroom applied); values carry size for
            # the association layer. Headroom is baked into the center so
            # frozen occlusion holds keep a valid composition.
            present: dict = {}
            for candidate in solid:
                fresh = [(stamp, box) for stamp, box in candidate.boxes
                         if time - step - 1e-6 <= stamp <= time + 1e-6]
                if not fresh:
                    continue
                _, box = fresh[-1]
                face_height = max(1e-6, box[3] - box[1])
                cx, cy = compute_target_anchor(
                    box, headroom_factor=cfg.headroom_factor,
                    crop_height=crop_height)
                size = max(1e-9, (box[2] - box[0]) * face_height)
                present[(candidate.epoch, candidate.speaker_id)] = (
                    (cx, cy), candidate.age_samples, size)
            decision, events = manager.step(
                time, seg_key, segment.confidence, present,
                scene_id=scene_of(time))
            for event in events:
                _log_target_event(event)
                # Acquisition-type discontinuities ease in; timeline SWITCH
                # cuts stay hard cuts (editorial standard). t=0 has no
                # previous position to ease from.
                if event.kind in ("ACQUIRE", "RESTORE", "FALLBACK",
                                  "GRACE_EXPIRED") and round(time, 3) > 0:
                    ease_marks.add(round(time, 3))
            if decision.kind == "static":
                # §21: never park on empty space while the section holds
                # faces (profile views / blur defeat the detector, but the
                # tape shows where people are). Lookahead applies while no
                # face has ever been seen yet (acquisition) OR a face
                # returns later in this shot (bridged blindness): only
                # past-without-future means the target truly left, and
                # Phase 2 owns that outcome (bounded grace → static,
                # never an immortal ghost-hold). Static also survives
                # when no face exists at all.
                if ((not _has_past_boxes(solid, time)
                        or _has_future_boxes(solid, time,
                                             scene_of=scene_of))):
                    _found = _nearest_known_face(solid, time, scene_of=scene_of)
                    if _found is not None:
                        speaker_label, box = _found
                        center = compute_target_anchor(
                            box, headroom_factor=cfg.headroom_factor,
                            crop_height=crop_height)
                        logger.debug(
                            "[TARGET_FALLBACK] time=%.2f from=static "
                            "reason=nearest_known_face fallback=%s",
                            time, speaker_label)
                    else:
                        speaker_label, center = "static", STATIC_CENTER
                else:
                    speaker_label, center = "static", STATIC_CENTER
            else:
                speaker_label = decision.key[1] if decision.key else segment.speaker_id
                center = decision.center
            samples.append((round(time, 3), speaker_label, center[0], center[1]))
            sample_scenes.append(scene_of(time))
            # Phase 4: anchor + manager snapshot per sample for target-crop
            # validation and evidence enrichment (post-smoothing compare).
            anchors.append((round(time, 3), speaker_label,
                            round(center[0], 4), round(center[1], 4)))
            snapshots[round(time, 3)] = (
                decision.state, decision.key[1] if decision.key else None,
                manager.candidate[1] if manager.candidate else None)
            time = round(time + step, 6)
        from .framing_config import use_hard_cuts

        if use_hard_cuts(cfg):
            # Cut mode: pre-position a leading static head on the first
            # confirmed target instead of flashing center then shifting.
            # (Ease mode keeps the pan: the eased span smooths it.)
            _backfill_leading_static(samples, anchors)
        def _single_faces_for(speaker, start, end):
            track = next(
                (item for item in solid if item.speaker_id == speaker), None)
            if track is None:
                return []
            box = track.box_at(min((start + end) / 2, duration - 1e-3))
            return [box] if box is not None else []

        trajectory, instructions, keyframes = _render_follow_samples(
            samples, sample_scenes, anchors, ease_marks,
            faces_for=_single_faces_for, layout="single",
            src_w=src_w, src_h=src_h,
            crop_width=crop_width, crop_height=crop_height,
            rung_w=rung_w, rung_h=rung_h,
            duration=duration, cfg=cfg,
            kf_stats=kf_stats, guard_notes=guard_notes,
        )
        _relabel_single_context(instructions, solid, cfg)
    elif mode == "speaker-follow":
        # No confident timeline: follow the highest-evidence track (not
        # the largest box — a big silent face must not beat a small
        # talking one). Falls back to largest when evidence is absent.
        from .visual import BoundingBox, FaceObservation, SpeakerSegment

        if evidence_peaks:
            leader_id = sorted(evidence_peaks,
                               key=lambda tid: (-evidence_peaks[tid], tid))[0]
            track = next((t for t in solid if t.speaker_id == leader_id), None)
        else:
            track = None
        if track is None:
            track = max(solid, key=lambda t: sum(
                (box[2] - box[0]) * (box[3] - box[1]) for _, box in t.boxes
            ))
        center = track.center_at(duration / 2) or (0.5, 0.5)
        candidates = make_crop_instructions(
            [SpeakerSegment(
                start=0, end=duration, speaker_id=track.speaker_id, confidence=track.confidence,
            )], [
                FaceObservation(
                    timestamp=duration / 2, speaker_id=track.speaker_id,
                    face=BoundingBox(
                        x=max(0.0, center[0] - 0.1), y=max(0.0, center[1] - 0.15),
                        width=0.2, height=0.3,
                    ),
                    lip_score=0, audio_score=0, confidence=track.confidence,
                )
            ],
            src_w=src_w, src_h=src_h,
        )
        if cfg.quality_guard_enabled:
            kept = []
            for instruction in candidates:
                box = track.box_at(duration / 2)
                keep, note = guard_instruction(
                    instruction, [box] if box else [], src_w, src_h,
                    out_w=rung_w, out_h=rung_h,
                    max_upscale=cfg.max_upscale_factor,
                    visibility_min=cfg.face_visibility_min,
                )
                if keep:
                    kept.append(instruction)
                else:
                    guard_notes.append(f"[single] {note}")
            instructions = kept
        else:
            instructions = candidates
    elif mode == "group":
        from .framing_geometry import safe_group_crop
        from .visual import CropInstruction

        faces = []
        for track in solid:
            box = track.box_at(duration / 2)
            if box:
                faces.append(box)
        if faces:
            window, report = safe_group_crop(
                faces, margin=cfg.group_margin,
                out_w=cfg.output_width, out_h=cfg.output_height,
                visibility_min=cfg.face_visibility_min,
                src_w=src_w, src_h=src_h,
            )
            # Gap-dominated window: both faces technically visible but the
            # crop is mostly the empty space between them. With exactly two
            # faces there is no middle occupant. Phase 4.2: NO split-screen
            # (§52 — two split panes are two video layers, forbidden).
            # Fall back to the strongest single speaker instead of an
            # empty-center shot: a correct single always beats a bad pair.
            gap_ratio = 0.0
            if len(faces) == 2:
                ordered = sorted(faces, key=lambda box: (box[0] + box[2]) / 2)
                gap_ratio = min(1.0, max(0.0, ordered[1][0] - ordered[0][2])
                                / max(window.width, 1e-9))
            if ((report["reason"] == "safest-max-window"
                    and min(report["visibility"]) < cfg.face_visibility_min
                    and len(faces) >= 2)
                    or (len(faces) == 2
                        and gap_ratio > cfg.two_speaker_max_empty_space)):
                from .framing_geometry import compute_target_anchor as _cta2

                _ranked = sorted(
                    ((evidence_peaks.get(t.speaker_id, 0.0), t) for t in solid),
                    key=lambda kv: (-kv[0], kv[1].speaker_id),
                )
                _leader = _ranked[0][1] if _ranked else max(
                    solid, key=lambda t: sum(
                        (b[2] - b[0]) * (b[3] - b[1]) for _, b in t.boxes))
                _mid = min(duration / 2, duration - 1e-3)
                _box = _leader.box_at(_mid) or _leader.boxes[-1][1]
                _cx, _cy = _cta2(_box, headroom_factor=cfg.headroom_factor,
                                 crop_height=crop_height)
                _cx = min(1 - cfg.safe_margin - crop_width / 2,
                          max(cfg.safe_margin + crop_width / 2, _cx))
                _cy = min(1 - crop_height / 2, max(crop_height / 2, _cy))
                logger.info(
                    "[COMPOSITION_REJECT] reason=%s gap=%.2f "
                    "fallback=single speaker=%s (no split-screen)",
                    report["reason"], round(gap_ratio, 2),
                    _leader.speaker_id)
                guard_notes.append(
                    f"[group] {report['reason']} gap={gap_ratio:.2f}: "
                    f"single {_leader.speaker_id} (split-screen removed)")
                instructions = [CropInstruction(
                    start=0, end=duration, speaker_id=_leader.speaker_id,
                    center_x=_cx, center_y=_cy,
                    width=crop_width, height=crop_height, layout="single",
                )]
                mode = "speaker-follow"
                reason = (f"gap-dominated group rejected "
                          f"(gap {gap_ratio:.2f}); single {_leader.speaker_id}")
            else:
                if report["reason"] != "union-fit":
                    logger.info("[GROUP_CROP] reason=%s visibility=%s",
                                report["reason"],
                                [round(value, 2) for value in report["visibility"]])
                group = make_group_crop(faces, safe_margin=cfg.group_margin,
                                        src_w=src_w, src_h=src_h)
                instruction = CropInstruction(
                    start=0, end=duration, speaker_id=group.speaker_id,
                    center_x=group.center_x, center_y=group.center_y,
                    width=group.width, height=group.height, layout=group.layout,
                )
                if cfg.quality_guard_enabled:
                    keep, note = guard_instruction(
                        instruction, faces, src_w, src_h,
                        out_w=rung_w, out_h=rung_h,
                        max_upscale=cfg.max_upscale_factor,
                        visibility_min=cfg.face_visibility_min,
                    )
                    if not keep:
                        logger.info(
                            "[COMPOSITION_REJECT] reason=group_guard "
                            "detail=%s fallback=safe_single", note)
                        guard_notes.append(f"[group] {note}")
                    else:
                        if note != "ok":
                            guard_notes.append(f"[group] {note}")
                        instructions = [instruction]
                else:
                    instructions = [instruction]

    if not instructions and solid:
        # §22 hierarchy bottom: never fail loudly (or render blind) while
        # faces exist. Safest single: highest speech evidence, else largest.
        from .framing_geometry import (compute_target_anchor,
                                       portrait_crop_for_center)
        from .visual import CropInstruction as _CI

        if evidence_peaks:
            _leader = sorted(evidence_peaks,
                             key=lambda tid: (-evidence_peaks[tid], tid))[0]
            _track = next((t for t in solid if t.speaker_id == _leader), None)
        else:
            _track = None
        if _track is None:
            _track = max(solid, key=lambda t: sum(
                (b[2] - b[0]) * (b[3] - b[1]) for _, b in t.boxes))
        _box = _track.box_at(duration / 2) or _track.boxes[-1][1]
        _cx, _cy = compute_target_anchor(
            _box, headroom_factor=cfg.headroom_factor,
            crop_height=crop_height)
        _win = portrait_crop_for_center(
            _cx, _cy, src_w, src_h, cfg.output_width, cfg.output_height)
        _cx, _cy = _win.center
        instructions = [_CI(
            start=0, end=duration, speaker_id=_track.speaker_id,
            center_x=_cx, center_y=_cy, width=_win.width,
            height=_win.height, layout="single")]
        guard_notes.append(f"[safe-fallback] empty plan -> {_track.speaker_id}")
        logger.warning("[SAFE_FALLBACK] empty instructions; single %s",
                       _track.speaker_id)

    # §21 final net across ALL modes: a crop holding no face while the
    # section holds faces is recentered on the nearest known face. Runs
    # before keyframes so every render path consumes corrected geometry.
    _enforce_person_presence(instructions, solid, crop_width, crop_height,
                             cfg, guard_notes, scene_of=scene_of)

    if not keyframes and instructions and all(
            item.layout != "split" for item in instructions):
        # Constant-geometry modes (single-face follow, group window):
        # static two-point keyframes so every mode can use the smooth
        # single-pass renderer. Split panes keep the legacy path.
        first = instructions[0]
        keyframes = [(0.0, first.center_x, first.center_y,
                      first.width, first.height, 0),
                     (duration, first.center_x, first.center_y,
                      first.width, first.height, 0)]

    first = instructions[0] if instructions else None
    crop = (
        (first.center_x - first.width / 2, first.center_y - first.height / 2,
         first.width, first.height)
        if first else None
    )
    segment = next((item for item in timeline if item.start <= 0 < item.end), None)
    debug_log = dump_active_speaker_log(
        0.0, solid, timeline,
        segment.speaker_id if segment else (instructions[0].speaker_id if instructions else None),
        segment.confidence if segment else None,
        crop,
    )
    if guard_notes:
        debug_log += "\n[QUALITY_GUARD]\n" + "\n".join(guard_notes)
    if scene_times:
        debug_log += "\n[SCENE_CUT] times=" + ",".join(f"{item:.2f}" for item in scene_times)
    # Phase 4: join manager snapshots into evidence rows for the debug
    # artifact (bounded: one row per track per window, same count).
    for row in evidence_rows:
        snap = snapshots.get(round(row["timestamp"], 3))
        if snap is not None:
            row["target_state"], row["current_target"], row["candidate"] = snap
        else:
            row["target_state"], row["current_target"], row["candidate"] = (
                "unknown", None, None)
    analysis_seconds = _time.monotonic() - analysis_start
    perf = {
        "video_duration": duration,
        "analysis_duration": round(analysis_seconds, 2),
        "analysis_fps": fps,
        "analysis_max_width": analysis_width,
        "frames_analyzed": frames_analyzed,
        "frames_decoded": frames_decoded,
        "frames_skipped": frames_decoded - frames_analyzed,
        "adaptive": sampler is not None,
        "memory_guard": memory_guard,
        "scene_cuts": len(scene_times),
        "instructions": len(instructions),
        "keyframes": len(keyframes),
        "deadzone_holds": kf_stats.get("deadzone_holds", 0),
        "limit_trips": kf_stats.get("limit_trips", 0),
        "transitions": kf_stats.get("transitions", 0),
        "jumps": kf_stats.get("jumps", 0),
        "peak_rss_mb": _peak_rss_mb(),
    }
    logger.info(
        "[FRAMING_PERF] video_duration=%.1f analysis_duration=%.1f "
        "analysis_fps=%g analysis_resolution=%d frames_analyzed=%d "
        "frames_skipped=%d scene_cuts=%d instructions=%d peak_rss_mb=%s",
        duration, analysis_seconds, fps, analysis_width,
        frames_analyzed, frames_decoded - frames_analyzed,
        len(scene_times), len(instructions),
        ("%.0f" % perf["peak_rss_mb"]) if perf["peak_rss_mb"] >= 0 else "unavailable",
    )
    section_metrics = compute_section_metrics(
        timeline_metrics, timeline, keyframes, solid, instructions,
        speech_times, duration, cfg)
    # TalkNet summary rides along (device, triggers, cache, timings).
    # UNKNOWN means escalation never triggered this section (model never
    # touched): report STANDBY so dashboards don't show a scary state.
    if talknet_detector is not None:
        try:
            _tn_status = talknet_detector.status
            if _tn_status == "UNKNOWN":
                _tn_status = "STANDBY"
            talknet_summary = {
                "enabled": True,
                "status": _tn_status,
                "device": (talknet_detector.device_info.device
                           if talknet_detector.device_info else "unknown"),
                "trigger_windows": list(
                    getattr(talknet_detector, "trigger_log", []) or []),
                "metrics": {k: v for k, v in talknet_detector.metrics.items()},
            }
            section_metrics["talknet"] = talknet_summary
            _dm = talknet_detector.metrics
            logger.info(
                "[TALKNET_PERF] device=%s inference=%d cache_hits=%d "
                "cache_miss=%d faces=%d infer_ms=%.0f",
                talknet_summary["device"], _dm.get("inference_count", 0),
                _dm.get("cache_hits", 0), _dm.get("cache_misses", 0),
                _dm.get("faces_scored", 0),
                _dm.get("inference_ms_total", 0.0))
        except Exception as exc:
            logger.warning("[TALKNET_PERF] summary_failed: %s", exc)
        finally:
            try:
                talknet_detector.close()
            except Exception:
                pass
    logger.info(
        "[FRAMING_METRICS] switches=%d false_switches=%d ambiguous=%d "
        "acq_latency_ms=%s switch_latency_mean_ms=%s coverage=%s "
        "empty_frame=%s edge_margin=%s",
        section_metrics["switch_count"], section_metrics["false_switch_count"],
        section_metrics["ambiguous_holds"],
        section_metrics["acquisition_latency_ms"],
        section_metrics["switch_latency_ms"]["mean"],
        section_metrics["speech_coverage"],
        section_metrics["empty_frame_ratio"],
        (section_metrics["composition"] or {}).get("mean_edge_margin"),
    )
    return FramingPlan(instructions=instructions, mode=mode, reason=reason,
                       tracks=solid, debug_log=debug_log,
                       scenes=scene_times, perf=perf,
                       trajectory=trajectory, keyframes=keyframes,
                       keyframe_stats=kf_stats, evidence_log=evidence_rows,
                       metrics=section_metrics)


def compute_section_metrics(timeline_metrics: dict,
                            timeline: list,
                            keyframes: list,
                            tracks: list,
                            instructions: list,
                            speech_times: list[float],
                            duration: float,
                            cfg) -> dict:
    """Phase 4.2 decision metrics from analysis products (no GT needed).

    - switch_latency_ms: onset->switch per switch (from timeline telemetry);
      acquisition_latency_ms: first speech -> first ACTIVATE.
    - false_switch_count: A->B->A with B <= 1.5s (jitter, not a decision).
    - speech_coverage: fraction of speech-active windows covered by any
      timeline segment (miss proxy: speech nobody framed).
    - empty_frame_ratio: fraction of sampled keyframe times whose crop
      holds no solid face above 0.5 visibility (must be ~0).
    - composition: mean edge-margin score + mean min-face-size score over
      keyframes (comfortable framing, §23-24).
    """
    from .framing_geometry import CropWindow, face_visibility

    metrics: dict = {
        "switch_count": timeline_metrics.get("switch_count", 0),
        "false_switch_count": timeline_metrics.get("false_switch_count", 0),
        "ambiguous_holds": timeline_metrics.get("ambiguous_holds", 0),
        "onset_speakers": list(timeline_metrics.get("onset_speakers", [])),
        "acquisition_latency_ms": timeline_metrics.get("acquisition_latency_ms"),
        "segment_count": timeline_metrics.get("segment_count", 0),
    }
    latencies = [s.get("latency_ms", 0) for s in timeline_metrics.get("switches", [])]
    metrics["switch_latency_ms"] = {
        "mean": round(sum(latencies) / len(latencies)) if latencies else None,
        "max": max(latencies) if latencies else None,
        "samples": len(latencies),
    }
    covered = 0
    for stamp in speech_times:
        for segment in timeline:
            try:
                if segment.start <= stamp < segment.end:
                    covered += 1
                    break
            except (AttributeError, TypeError):
                continue
    metrics["speech_coverage"] = (
        round(covered / len(speech_times), 3) if speech_times else None)
    empty = 0
    checked = 0
    margins: list[float] = []
    sizes: list[float] = []
    for point in keyframes:
        try:
            stamp, cx, cy, width, height = point[0], point[1], point[2], point[3], point[4]
        except (IndexError, TypeError, ValueError):
            continue
        window = CropWindow(x=cx - width / 2, y=cy - height / 2,
                            width=width, height=height)
        held = False
        best_vis = 0.0
        for track in tracks:
            try:
                box = track.box_at(stamp)
            except (AttributeError, TypeError):
                box = None
            if box is None:
                continue
            vis = face_visibility(box, window)
            best_vis = max(best_vis, vis)
            if vis >= 0.5:
                held = True
            area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
            sizes.append(min(1.0, area / 0.04))
        checked += 1
        if not held:
            empty += 1
        margins.append(min(1.0, max(0.0, min(cx, 1.0 - cx) / 0.08)))
    metrics["empty_frame_ratio"] = (
        round(empty / checked, 3) if checked else None)
    metrics["composition"] = {
        "mean_edge_margin": round(sum(margins) / len(margins), 3) if margins else None,
        "mean_face_size": round(sum(sizes) / len(sizes), 3) if sizes else None,
        "keyframes_checked": checked,
    }
    return metrics


def _peak_rss_mb() -> float:
    """Process peak RSS in MiB (Linux). Returns -1 when unavailable rather
    than fabricating a number (callers must render it as 'unavailable')."""
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return -1.0



