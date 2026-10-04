"""Speaker scoring primitives (Phase 2). Pure functions, no dependencies.

Fusion rule (all components normalized to 0-1 BEFORE fusion)::

    fused = (wv*visual + wa*audio + wc*continuity) / (wv+wa+wc)
    final = fused * face_quality * spatial_prior      # bounded modifiers

Position never votes: ``spatial_prior`` defaults to 1.0 (neutral) and is
only applied when explicitly enabled. Being center-frame therefore cannot
by itself make someone the speaker.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScoreWeights:
    """Fusion weights. Normalized by sum at use-site so any positive
    combination stays in 0-1 (equal split when all are zero)."""

    visual: float = 0.55
    audio: float = 0.35
    continuity: float = 0.10

    @property
    def total(self) -> float:
        total = self.visual + self.audio + self.continuity
        return total if total > 0 else 1.0


DEFAULT_WEIGHTS = ScoreWeights()


def normalize_score(value: float) -> float:
    """Clamp any speaking component into 0-1 (NaN/inf safe)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number == float("inf") or number == float("-inf"):
        return 0.0
    return min(1.0, max(0.0, number))


def continuity_from_boxes(box_times: list[float], timestamp: float,
                          expected_step: float = 0.5) -> float:
    """Track-maturity continuity in 0-1 from detection timestamps.

    Mature, freshly-seen tracks score near 1; young or long-lost tracks
    score near 0. Deterministic and analysis-rate explicit. This is the
    track-intrinsic half of continuity; incumbency (being the current
    target) is handled by hold/hysteresis, NOT by inflating this score,
    so continuity alone can never make a target immortal.
    """
    past = [stamp for stamp in box_times if stamp <= timestamp + 1e-6]
    if not past:
        return 0.0
    maturity = min(1.0, len(past) / 6.0)  # mature after ~6 detections
    if any(abs(stamp - timestamp) <= expected_step + 1e-6 for stamp in past):
        return maturity
    gap = timestamp - max(past)
    lost_steps = max(1, int(round(gap / expected_step)))
    return maturity * (0.5 ** lost_steps)


def spatial_prior_for(center_x: float, center_y: float,
                      enabled: bool = False) -> float:
    """Mild center-bias modifier in [0.85, 1.0]. Disabled (1.0) by default
    so spatial position cannot decide who speaks."""
    if not enabled:
        return 1.0
    distance = ((center_x - 0.5) ** 2 + (center_y - 0.5) ** 2) ** 0.5
    return max(0.85, 1.0 - 0.3 * min(1.0, distance))


def calculate_speaker_score(visual_score: float, audio_score: float,
                            continuity_score: float = 0.0,
                            face_quality: float = 1.0,
                            spatial_prior: float = 1.0,
                            weights: ScoreWeights | None = None) -> float:
    """Fuse normalized components into one 0-1 speaker score."""
    weights = weights or DEFAULT_WEIGHTS
    visual = normalize_score(visual_score)
    audio = normalize_score(audio_score)
    continuity = normalize_score(continuity_score)
    fused = (weights.visual * visual + weights.audio * audio
             + weights.continuity * continuity) / weights.total
    return normalize_score(fused * normalize_score(face_quality)
                           * normalize_score(spatial_prior))


# ---------------------------------------------------------------------------
# Phase 4.2: per-face speech evidence, AV association, switch confidence,
# comfortable composition. All pure functions; thresholds arrive as
# arguments (config lives in framing_config, never here).
# ---------------------------------------------------------------------------

def onset_score(history: list[tuple[float, float]],
                now: float,
                window_ms: int = 300,
                rise_min: float = 0.25) -> float:
    """Rising-edge speech onset in 0-1 from recent (t, lip) samples.

    Strong onset looks like 0.10 -> 0.20 -> 0.42 -> 0.65 -> 0.80 inside the
    window; jitter like 0.70 -> 0.20 -> 0.65 -> 0.10 scores ~0. Requires at
    least 2 samples; empty/short history returns 0.0 (never invents).
    """
    window_s = window_ms / 1000.0
    recent = [(t, v) for t, v in history if now - window_s - 1e-9 <= t <= now + 1e-9]
    if len(recent) < 2:
        return 0.0
    first = recent[0][1]
    peak = max(v for _, v in recent)
    last = recent[-1][1]
    rise = peak - first
    if rise < rise_min:
        return 0.0
    # Sustained at the end (not a spike that already collapsed).
    sustain = min(1.0, last / max(peak, 1e-9))
    return normalize_score(rise * (0.5 + 0.5 * sustain))


def ambiguity_margin(scores: dict[str, float]) -> tuple[str | None, str | None, float]:
    """(best_id, second_id, best-second gap). Deterministic: ties break to
    the lowest id so hash seeds never decide."""
    if not scores:
        return None, None, 0.0
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best_id, best = ranked[0]
    second_id, second = ranked[1] if len(ranked) > 1 else (None, best)
    return best_id, second_id, max(0.0, best - second)


def confidence_level(score: float, margin: float) -> str:
    """HIGH/MEDIUM/LOW/AMBIGUOUS switch-readiness label."""
    if margin < 0:
        return "AMBIGUOUS"
    if score >= 0.6 and margin >= 0.15:
        return "HIGH"
    if score >= 0.4 and margin >= 0.05:
        return "MEDIUM"
    if score >= 0.25:
        return "LOW"
    return "AMBIGUOUS"


def face_quality_score(area: float,
                       edge_clip: float,
                       confidence: float) -> float:
    """Face quality modifier in 0-1 from geometry + detector confidence.

    area: normalized bbox area. edge_clip: fraction of the box outside the
    frame (0 = fully visible). Tiny, clipped, or low-confidence faces score
    low. This is a MODIFIER (multiplied with small weight); it can never
    elect a silent face over strong speech evidence.
    """
    size = min(1.0, max(0.0, area) / 0.04)  # full marks at ~4% frame area
    visible = min(1.0, max(0.0, 1.0 - edge_clip * 4.0))
    return normalize_score(0.5 * size + 0.3 * visible + 0.2 * normalize_score(confidence))


def associate_track(lost_center: tuple[float, float],
                    lost_size: float,
                    gap_ms: float,
                    candidates: list[dict],
                    spatial_weight: float = 0.7,
                    max_dist: float = 0.25,
                    max_size_ratio: float = 2.0,
                    max_gap_ms: int = 2000) -> str | None:
    """Lightweight lost-target association (no ReID).

    candidates: [{id, center, size}]. Returns the best id, or None when
    nothing passes ALL hard gates (distance, size ratio, gap). Score =
    spatial proximity blended with size similarity; ties -> lowest id.
    """
    if gap_ms > max_gap_ms or not candidates:
        return None
    scored = []
    for cand in candidates:
        dist = ((cand["center"][0] - lost_center[0]) ** 2
                + (cand["center"][1] - lost_center[1]) ** 2) ** 0.5
        if dist > max_dist:
            continue
        if cand["size"] <= 0 or lost_size <= 0:
            # Size unknown on either side: neutral, never a veto.
            size_sim = 1.0
        else:
            size_ratio = max(cand["size"], 1e-9) / max(lost_size, 1e-9)
            if size_ratio > max_size_ratio or size_ratio < 1.0 / max_size_ratio:
                continue
            size_sim = 1.0 - min(1.0, abs(size_ratio - 1.0))
        spatial_sim = 1.0 - min(1.0, dist / max_dist)
        score = spatial_weight * spatial_sim + (1.0 - spatial_weight) * size_sim
        scored.append((score, cand["id"]))
    if not scored:
        return None
    best_score = max(score for score, _ in scored)
    winners = sorted(cid for score, cid in scored if score == best_score)
    return winners[0]


def associated_audio_score(vad: float,
                           av_correlation: float,
                           neutral_floor: float = 0.25) -> float:
    """Per-face audio evidence: the global VAD gate SCALED by how well this
    face's mouth motion correlates with the audio envelope.

    ``vad`` is still a hard gate (silence -> 0 for everyone), but a talking
    room no longer hands every face the same 0.9: high correlation keeps
    ~full VAD, zero correlation (degenerate/uncorrelated) keeps only the
    neutral floor. All inputs/outputs normalized 0-1.
    """
    vad = normalize_score(vad)
    corr = normalize_score(av_correlation)
    floor = min(1.0, max(0.0, neutral_floor))
    return normalize_score(vad * (floor + (1.0 - floor) * corr))


def av_correlation(lip_series: list[tuple[float, float]],
                   vad_series: list[tuple[float, float]],
                   window_ms: int = 1200,
                   min_samples: int = 3) -> float:
    """Temporal audio-visual association in 0-1 for one face.

    Pearson correlation between demeaned mouth-motion and VAD-envelope
    samples inside the trailing window, mapped (r+1)/2. Degenerate input
    (flat VAD, flat mouth, too few samples) returns 0.5 NEUTRAL — never
    invents evidence, never vetoes. Rolling windows only; no full-tape
    buffering (callers pass bounded series).
    """
    if not lip_series or not vad_series:
        return 0.5
    window_s = window_ms / 1000.0
    now = max(lip_series[-1][0], vad_series[-1][0])
    lips = [v for t, v in lip_series if now - window_s - 1e-9 <= t <= now + 1e-9]
    vads = [v for t, v in vad_series if now - window_s - 1e-9 <= t <= now + 1e-9]
    if len(lips) < min_samples or len(vads) < min_samples:
        return 0.5
    mean_lip = sum(lips) / len(lips)
    mean_vad = sum(vads) / len(vads)
    var_lip = sum((v - mean_lip) ** 2 for v in lips)
    var_vad = sum((v - mean_vad) ** 2 for v in vads)
    if var_lip <= 1e-12 or var_vad <= 1e-12:
        return 0.5
    n = min(len(lips), len(vads))
    cov = sum((lips[i] - mean_lip) * (vads[i] - mean_vad) for i in range(n))
    denom = (var_lip * var_vad * (n / max(len(lips), 1))
             * (n / max(len(vads), 1))) ** 0.5
    if denom <= 1e-12:
        return 0.5
    pearson = max(-1.0, min(1.0, cov / denom))
    return normalize_score((pearson + 1.0) / 2.0)


def window_median(series: list[tuple[float, float]],
                  now: float,
                  window_ms: int) -> float | None:
    """Median of values inside the trailing window; None when empty."""
    window_s = window_ms / 1000.0
    values = [v for t, v in series if now - window_s - 1e-9 <= t <= now + 1e-9]
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def fuse_mouth_motion(short: float | None,
                      medium: float | None,
                      long: float | None,
                      w_short: float = 0.3,
                      w_medium: float = 0.5,
                      w_long: float = 0.2) -> float:
    """Multi-window mouth evidence: SHORT reacts (onset), MEDIUM decides
    (sustained speech), LONG vetoes blips (continuity). Missing windows are
    skipped and weights renormalized; all-empty -> 0.0."""
    parts = [(short, w_short), (medium, w_medium), (long, w_long)]
    kept = [(v, w) for v, w in parts if v is not None]
    if not kept:
        return 0.0
    total_w = sum(w for _, w in kept)
    if total_w <= 0:
        return 0.0
    return normalize_score(sum(v * w for v, w in kept) / total_w)


def head_motion_score(now: float, baseline: float) -> float:
    """Track-relative head-motion excess in 0-1 (head turn context).

    Relative to the track's own baseline, so exposure/compression levels
    cannot shift it: 0 when still, ~1 when moving at twice the usual rate.
    """
    try:
        now_f, base_f = float(now), float(baseline)
    except (TypeError, ValueError):
        return 0.0
    if base_f <= 1e-9:
        return 0.0 if now_f <= 1e-9 else 1.0
    return normalize_score((now_f - base_f) / (base_f + 1e-6))


def damp_by_head_motion(mouth: float,
                        head_excess: float,
                        strength: float = 0.3) -> tuple[float, float]:
    """Contextual head-motion damping (head turn != speech).

    ``head_excess`` is the track-relative motion excess in 0-1 (see
    ``head_motion_score``). Returns (damped_mouth, head_excess). Head motion
    never ADDS speech evidence; when the whole head moves a lot while the
    mouth signal is only moderate, the reading is likely bbox/head jitter
    and is damped. Strong mouth (>= 0.7) is never damped.
    """
    mouth = normalize_score(mouth)
    excess = normalize_score(head_excess)
    if mouth >= 0.7 or excess <= 0:
        return mouth, excess
    damped = mouth * (1.0 - strength * excess * (0.7 - mouth) / 0.7)
    return normalize_score(damped), excess


def switch_confidence(speaker_confidence: float,
                      onset_bonus: float = 0.0,
                      movement_cost: float = 0.0,
                      visibility_penalty: float = 0.0,
                      composition_bonus: float = 0.0,
                      ambiguity_penalty: float = 0.0) -> float:
    """Camera-move readiness in 0-1, SEPARATE from speaker confidence.

    "How sure is the FACE talking" (speaker_confidence) vs "how sure must
    the CAMERA move" (this). Geometry/continuity costs can only LOWER the
    score; they can never elect a silent face (caller adds them only for
    candidates above the switch threshold).
    """
    value = (normalize_score(speaker_confidence)
             + normalize_score(onset_bonus)
             + normalize_score(composition_bonus)
             - normalize_score(movement_cost)
             - normalize_score(visibility_penalty)
             - normalize_score(ambiguity_penalty))
    return normalize_score(value)


def movement_cost(face_a: tuple[float, float] | None,
                  face_b: tuple[float, float] | None,
                  weight: float = 0.5,
                  full_cost_at: float = 0.5) -> float:
    """Normalized cost of moving the crop between two face centers."""
    if face_a is None or face_b is None:
        return 0.0
    dist = ((face_a[0] - face_b[0]) ** 2 + (face_a[1] - face_b[1]) ** 2) ** 0.5
    return normalize_score(min(1.0, dist / full_cost_at) * weight)


def count_false_switches(segments: list,
                         max_blip_seconds: float = 1.5) -> int:
    """A->B->A where the B visit lasts <= max_blip_seconds: jitter, not a
    decision. Segments need .speaker_id/.start/.end (SpeakerSegment)."""
    count = 0
    for prev, cur, nxt in zip(segments, segments[1:], segments[2:]):
        try:
            if (prev.speaker_id == nxt.speaker_id
                    and cur.speaker_id != prev.speaker_id
                    and (cur.end - cur.start) <= max_blip_seconds + 1e-9):
                count += 1
        except (AttributeError, TypeError):
            continue
    return count
