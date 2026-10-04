from __future__ import annotations

import math
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .speaker_score import DEFAULT_WEIGHTS, ScoreWeights, calculate_speaker_score, onset_score


class BoundingBox(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def stays_in_frame(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("face bounding box must stay inside the frame")
        return self

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2


class FaceObservation(BaseModel):
    """Adapter-neutral output for YOLO/MediaPipe + TalkNet/SyncNet adapters."""

    model_config = ConfigDict(extra="forbid")
    timestamp: float = Field(ge=0)
    speaker_id: str = Field(min_length=1, max_length=64)
    face: BoundingBox
    lip_score: float = Field(ge=0, le=1)
    audio_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1, default=1)
    continuity_score: float = Field(ge=0, le=1, default=0.0)
    spatial_prior: float = Field(ge=0, le=1, default=1.0)
    score_weights: tuple[float, float, float] | None = None
    # Phase 4: face-quality modifier. quality_weight=0.0 (default) keeps
    # legacy behavior bit-identical; the builder passes config weight.
    quality_score: float = Field(ge=0, le=1, default=1.0)
    quality_weight: float = Field(ge=0, le=1, default=0.0)
    # Phase 4.2: per-face associated evidence (all optional, neutral
    # defaults, so older payloads and external adapters keep validating).
    # av_score: audio-visual correlation for THIS face (0-1).
    av_score: float = Field(ge=0, le=1, default=0.0)
    # head_score: whole-head motion context (turn != speech), 0-1.
    head_score: float = Field(ge=0, le=1, default=0.0)
    # talknet_score: TalkNet escalation verdict for this window (0-1).
    # None = not escalated (heuristic only). Fused downstream, never a
    # switch permission by itself — Target Manager still decides.
    talknet_score: float | None = Field(default=None, ge=0, le=1)

    @property
    def active_score(self) -> float:
        """Phase 2 fusion (weights normalized, modifiers bounded).

        Default weights (0.55/0.35/0.10) are the configured baseline;
        per-observation ``score_weights`` override them when given.
        """
        weights = ScoreWeights(*self.score_weights) if self.score_weights else DEFAULT_WEIGHTS
        base = calculate_speaker_score(
            self.lip_score, self.audio_score, self.continuity_score,
            face_quality=self.confidence, spatial_prior=self.spatial_prior,
            weights=weights,
        )
        modifier = 1.0 - self.quality_weight + self.quality_weight * self.quality_score
        return max(0.0, min(1.0, base * modifier))


class SpeakerSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    speaker_id: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def valid_range(self):
        if not math.isfinite(self.start) or not math.isfinite(self.end) or self.start >= self.end:
            raise ValueError("speaker segment must have a finite positive range")
        return self


class CropInstruction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    speaker_id: str = Field(min_length=1)
    center_x: float = Field(ge=0, le=1)
    center_y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    layout: str = Field(default="single")


@dataclass(frozen=True)
class _Candidate:
    speaker_id: str
    start: float
    score: float


def build_speaker_timeline(
    observations: list[FaceObservation],
    duration: float,
    min_switch_seconds: float = 2,
    sample_step: float = 0.1,
    min_active_score: float = 0.25,
    # New params with defaults that maintain backward-compatible behaviour
    min_hold_seconds: float | None = None,          # defaults to min_switch_seconds / 5
    min_switch_interval_seconds: float | None = None,  # defaults to min_switch_seconds
    track_lost_grace_seconds: float = 0.3,
    no_speaker_fallback_seconds: float = 1.5,
    force_switch_margin: float = 0.35,
    activation_threshold: float | None = None,  # defaults to min_active_score
    switch_threshold: float | None = None,  # defaults to min_active_score * 1.2
    score_smoothing_alpha: float = 0.4,
    switch_margin: float = 0.0,  # candidate must clear current+margin (0.0 = legacy)
    # Phase 4 additions (all default to legacy behavior when zero/disabled):
    ambiguity_margin: float = 0.0,  # best must clear runner-up; else AMBIGUOUS hold
    onset_bonus: float = 0.0,  # capped bonus for confirmed speech onsets
    onset_window_ms: int = 300,  # rising-evidence window for onset
    onset_rise_min: float = 0.25,  # min lip rise inside the window
    target_acquisition_min_ms: int = 300,  # force floor for onset-confirmed
    continuity_bonus: float = 0.0,  # incumbency tie-breaker (bounded)
    continuity_keep_fraction: float = 0.5,  # bonus needs score >= frac*threshold
    unseen_decay: float = 1.0,  # per-window multiplier for speakers with
    # no observation in the window (0.9 melts stale incumbents; 1.0 legacy)
    # Phase 4.2: onset fast path + switch confidence + metrics.
    onset_fast_hold_ms: int = 500,  # onset-confirmed hold (vs 2s normal)
    onset_switch_margin: float = 0.15,  # onset path needs a WIDER margin
    movement_weight: float = 0.5,  # crop-travel cost weight (0-1 scale)
    movement_full_at: float = 0.5,  # normalized dist costing full weight
    visibility_weight: float = 0.5,  # small/edge-face switch penalty weight
    metrics_out: dict | None = None,  # filled with onset/switch telemetry
    score_fn=None,  # TalkNet fusion: score_fn(obs)->float. None = active_score.
) -> list[SpeakerSegment]:
    """Build a deterministic active-speaker timeline with a temporal state machine.

    Separated temporal concepts (all configurable, all in seconds/ms units):
    - HOLD (``min_switch_seconds``): a candidate must sustain its score this
      long before it may replace the current speaker.
    - COOLDOWN (``min_switch_interval_seconds``): minimum gap between two
      switches, independent of hold.
    - MARGIN (``switch_margin``): at switch time the candidate must also
      clear ``current_score + margin``; near-ties keep the incumbent
      (continuity wins ties).
    - FORCE path: ``force_switch_margin`` gap after ``min_hold_seconds``.
    - GRACE (``track_lost_grace_seconds``) + FALLBACK
      (``no_speaker_fallback_seconds``): absence handling, not switching.

    Improvements over the original simple hysteresis:
    - Per-speaker EMA score smoothing prevents single noisy frames from
      triggering switches.
    - TRACK_LOST_GRACE keeps the current speaker assigned while they briefly
      leave the detection window (turn-away, blink, partial occlusion).
    - Separate MIN_SPEAKER_HOLD and MIN_SPEAKER_SWITCH_INTERVAL: a new
      candidate must hold their score for hold_seconds, but the next switch
      cannot happen sooner than switch_interval_seconds after the last one.
    - FORCE_SWITCH_SCORE_MARGIN allows an immediate override when the score
      gap is large, subject to a minimum re-switch guard.
    - NO_SPEAKER_FALLBACK only clears the current speaker after prolonged
      silence, preventing one-frame dropouts from fragmenting the timeline.

    Every switch decision emits a structured [SPEAKER_STATE] log line.
    """
    import logging
    _log = logging.getLogger(__name__)

    if duration <= 0 or min_switch_seconds <= 0 or sample_step <= 0:
        raise ValueError("duration, min_switch_seconds and sample_step must be positive")

    # Resolve defaults for backward-compatible optional parameters.
    _min_hold: float = min_hold_seconds if min_hold_seconds is not None else min_switch_seconds / 5
    _min_switch_interval: float = (
        min_switch_interval_seconds if min_switch_interval_seconds is not None
        else min_switch_seconds
    )
    _activation_threshold: float = (
        activation_threshold if activation_threshold is not None else min_active_score
    )
    _switch_threshold: float = (
        switch_threshold if switch_threshold is not None else min_active_score * 1.2
    )

    ordered = sorted(observations, key=lambda item: item.timestamp)
    if not ordered:
        return []

    # ---- State ----
    current: str | None = None
    candidate: _Candidate | None = None
    segments: list[SpeakerSegment] = []
    segment_start = 0.0
    segment_confidence = 0.0

    # Smoothed scores per speaker (EMA)
    smoothed_scores: dict[str, float] = {}
    # Recent raw lip evidence per speaker, bounded by onset window
    # (drives onset bonus; pruned every step, never grows unbounded).
    lip_history: dict[str, list[tuple[float, float]]] = {}
    # Latched onset bonus per speaker: a confirmed rise keeps boosting
    # through the acquisition window (the EMA needs ~500ms to catch up;
    # a 300ms-only bonus would always expire first and never matter).
    onset_latch: dict[str, tuple[float, float]] = {}
    # Last timestamp each speaker was seen in any detection window
    last_seen: dict[str, float] = {}
    # Timestamp when we last performed a switch
    last_switch_time: float = -_min_switch_interval  # allow immediate first switch
    # Timestamp when current speaker was last considered "present"
    current_last_seen: float = 0.0
    # Timestamp when we first entered "no confident speaker" state
    no_speaker_since: float | None = None
    # Phase 4.2 telemetry (bounded dicts; also mirrored into metrics_out).
    last_center: dict[str, tuple[float, float]] = {}
    onset_first_seen: dict[str, float] = {}
    switches: list[dict] = []
    ambiguous_holds = 0
    first_speech_time: float | None = None
    first_activate_time: float | None = None

    time = 0.0
    while time < duration:
        # ---- 1. Collect observations in this window ----
        # NOTE: epsilon guards float dust (e.g. |8.25-8.2| computes as
        # 0.05000000000000071 > 0.05). Without it, observations on exact
        # boundaries vanish depending on representation noise, freezing
        # smoothed scores mid-ramp and deadlocking ambiguity holds.
        half = sample_step / 2
        frame = [item for item in ordered
                 if abs(item.timestamp - time) <= half + 1e-9]

        # ---- 2. Update EMA smoothed scores + last_seen ----
        seen_this_step: set[str] = set()
        for obs in frame:
            raw = score_fn(obs) if score_fn is not None else obs.active_score
            prev = smoothed_scores.get(obs.speaker_id, raw)
            smoothed_scores[obs.speaker_id] = (
                score_smoothing_alpha * raw + (1.0 - score_smoothing_alpha) * prev
            )
            last_seen[obs.speaker_id] = time
            seen_this_step.add(obs.speaker_id)
            last_center[obs.speaker_id] = (
                obs.face.center_x, obs.face.center_y)
            if (first_speech_time is None
                    and obs.audio_score >= _activation_threshold):
                first_speech_time = time
            # Phase 4: bounded lip-evidence history for onset detection.
            hist = lip_history.setdefault(obs.speaker_id, [])
            hist.append((time, obs.lip_score))
            cutoff = time - onset_window_ms / 1000.0
            while hist and hist[0][0] < cutoff - 1e-9:
                hist.pop(0)

        # ---- 2b. Decay unseen speakers ----
        # A speaker with no observation in this window melts toward zero
        # instead of freezing at a stale high: a departed incumbent stops
        # blocking fresh candidates (fixes timeline lag). At 1.0 (legacy)
        # this loop is skipped bit-identically.
        if unseen_decay < 1.0:
            for sid in list(smoothed_scores):
                if sid not in seen_this_step:
                    melted = smoothed_scores[sid] * unseen_decay
                    smoothed_scores[sid] = melted if melted > 1e-6 else 0.0

        # ---- 3. Find best speaker using smoothed scores ----
        # Deterministic order (never set-iteration order: hash seeds must
        # not decide ties) with incumbent-wins-ties for extra stability.
        ranked = sorted(
            ((sid, smoothed_scores[sid]) for sid in seen_this_step
             if smoothed_scores[sid] >= _activation_threshold),
            key=lambda kv: (-kv[1], 0 if kv[0] == current else 1, kv[0]),
        )
        if ranked:
            best_id, best_score = ranked[0]
            if len(ranked) > 1:
                second_id, second_score = ranked[1]
            else:
                second_id, second_score = None, best_score
        else:
            best_id, best_score = None, 0.0
            second_id, second_score = None, 0.0
        # Phase 4: ambiguity gate. Overlapping speech with a thin margin is
        # not a decision — hold the incumbent instead of flapping. A lone
        # speaker (no runner-up) is never ambiguous.
        ambiguous = (
            ambiguity_margin > 0.0
            and current is not None
            and best_id is not None
            and best_id != current
            and second_id is not None
            and (best_score - second_score) < ambiguity_margin
        )
        if ambiguous:
            ambiguous_holds += 1
            _log.debug(
                "[SPEAKER_AMBIGUITY] ts=%.2fs top=%s second=%s margin=%.3f",
                time, best_id, second_id, best_score - second_score,
            )
        switch_best_id = None if ambiguous else best_id
        switch_best_score = 0.0 if ambiguous else best_score
        # Phase 4.2: onset bonus for RISING speakers, not just the current
        # best. A new speaker climbs from below: unless its rise is latched
        # while still runner-up, the latch can never exist at the moment it
        # becomes best (rise already over) and the fast path never fires.
        # Audio-gated (window must carry voice) and capped; bounded state.
        decide_score = switch_best_score
        onset_confirmed = False
        # Latch lifetime must cover the fast hold it enables: a rise
        # confirmed at T stays usable until T + window + fast_hold, else
        # the hold always outlives the latch and the fast path can never
        # fire. Refresh still needs a NEW rise (no immortal latch).
        latch_ms = (max(onset_window_ms, target_acquisition_min_ms)
                    + onset_fast_hold_ms)
        # Prune expired latches every window (bounded state).
        for sid in [k for k, (_, at) in onset_latch.items()
                    if time - at > latch_ms / 1000.0 + 1e-9]:
            del onset_latch[sid]
        if onset_bonus > 0.0:
            for _sid, _score in ((best_id, best_score), (second_id, second_score)):
                if _sid is None or _sid == current:
                    continue
                if _score < 0.15:
                    continue
                _audio = next(
                    (o.audio_score for o in frame if o.speaker_id == _sid), 0.0)
                if _audio < _activation_threshold:
                    continue
                _bonus = onset_bonus * onset_score(
                    lip_history.get(_sid, []), time,
                    onset_window_ms, onset_rise_min)
                if _bonus > 0:
                    onset_latch[_sid] = (_bonus, time)
                    if _sid not in onset_first_seen:
                        onset_first_seen[_sid] = time
        if switch_best_id is not None:
            latched = onset_latch.get(switch_best_id)
            if latched is not None:
                latched_bonus, latched_at = latched
                decide_score = min(1.0, switch_best_score + latched_bonus)
                onset_confirmed = True
                _log.debug(
                    "[SPEAKER_ONSET] ts=%.2fs speaker=%s bonus=%.3f",
                    time, switch_best_id, latched_bonus,
                )

        # ---- 4. Current-speaker lost / grace / fallback tracking ----
        if current is not None:
            time_since_seen = time - last_seen.get(current, current_last_seen)
            if current in seen_this_step:
                current_last_seen = time
                no_speaker_since = None
            elif time_since_seen > track_lost_grace_seconds:
                # Current speaker absent beyond grace period.
                # Only start the fallback timer when there is NO confident
                # replacement at all (not even a candidate building up).
                has_any_qualified = best_id is not None or (
                    candidate is not None
                    and smoothed_scores.get(candidate.speaker_id, 0.0) >= _activation_threshold
                )
                if not has_any_qualified:
                    # No confident replacement yet; start or extend no-speaker timer.
                    if no_speaker_since is None:
                        no_speaker_since = time
                    elif time - no_speaker_since > no_speaker_fallback_seconds:
                        _log.debug(
                            "[SPEAKER_STATE] ts=%.3fs current=%s candidate=%s "
                            "current_score=%.2f candidate_score=%.2f "
                            "candidate_duration=%dms required=%dms decision=CLEAR_FALLBACK",
                            time, current,
                            candidate.speaker_id if candidate else "none",
                            smoothed_scores.get(current, 0.0),
                            best_score,
                            0, 0,
                        )
                        # Close current segment and clear state.
                        if segment_start < time:
                            segments.append(SpeakerSegment(
                                start=segment_start, end=time,
                                speaker_id=current, confidence=segment_confidence,
                            ))
                        _log.info(
                            "[TARGET_LOST] ts=%.2fs track=%s reason=silence_ttl",
                            time, current,
                        )
                        _log.info(
                            "[TARGET_FALLBACK] ts=%.2fs fallback_type=static "
                            "reason=no_qualified_speaker",
                            time,
                        )
                        current = None
                        candidate = None
                        no_speaker_since = None
                        time = round(time + sample_step, 6)
                        continue
                else:
                    # A candidate is building up; don't start fallback clock.
                    no_speaker_since = None

        # ---- 5. Retrieve current speaker's smoothed score ----
        # Phase 4 incumbency: a small continuity bonus while the current
        # target still carries reasonable evidence (bounded; can never make
        # a collapsed target immortal).
        current_score: float = smoothed_scores.get(current, 0.0) if current else 0.0
        if (continuity_bonus > 0.0 and current is not None
                and current_score >= continuity_keep_fraction * _activation_threshold):
            current_score = min(1.0, current_score + continuity_bonus)

        # ---- 6. State machine transitions ----
        if current is None and switch_best_id is not None:
            # No active speaker — activate immediately on first qualified detection.
            _log.debug(
                "[SPEAKER_STATE] ts=%.3fs current=none candidate=%s "
                "current_score=0.00 candidate_score=%.2f "
                "candidate_duration=0ms required=0ms decision=ACTIVATE",
                time, switch_best_id, decide_score,
            )
            _log.info(
                "[TARGET_DECISION] ts=%.2fs state=ACQUIRING target=%s "
                "confidence=%.2f reason=first_qualified",
                time, switch_best_id, round(decide_score, 2),
            )
            if first_activate_time is None:
                first_activate_time = time
            current = switch_best_id
            segment_start = time
            segment_confidence = best_score
            current_last_seen = time
            candidate = None
            no_speaker_since = None

        elif best_id == current:
            # Confirmed current speaker — reset candidate, update confidence.
            candidate = None
            segment_confidence = max(segment_confidence, best_score)
            no_speaker_since = None

        elif switch_best_id is not None and decide_score >= _switch_threshold:
            # A different speaker qualifies — evaluate switch logic.
            # (switch_best_id is None under ambiguity: hold, don't flap.)
            candidate_duration_s = (
                (time - candidate.start) if candidate and candidate.speaker_id == switch_best_id else 0.0
            )
            candidate_duration_ms = int(candidate_duration_s * 1000)
            required_ms = int(_min_switch_interval * 1000)
            cooldown_ok = (time - last_switch_time) >= _min_switch_interval

            # Force-switch override: large score gap + minimum re-switch guard.
            # Onset-confirmed candidates use the faster acquisition floor
            # (strong new evidence should not wait out a full hold).
            score_gap = decide_score - current_score
            force_floor = (target_acquisition_min_ms / 1000.0
                           if onset_confirmed else _min_hold)
            force_switch = (
                score_gap >= force_switch_margin
                and (time - last_switch_time) >= force_floor
            )

            if force_switch:
                _log.debug(
                    "[SPEAKER_STATE] ts=%.3fs current=%s candidate=%s "
                    "current_score=%.2f candidate_score=%.2f "
                    "candidate_duration=%dms required=%dms decision=FORCE_SWITCH",
                    time, current, switch_best_id,
                    current_score, decide_score,
                    candidate_duration_ms, required_ms,
                )
                if current is not None and segment_start < time:
                    segments.append(SpeakerSegment(
                        start=segment_start, end=time,
                        speaker_id=current, confidence=segment_confidence,
                    ))
                _prev_target = current
                current = switch_best_id
                segment_start = time
                segment_confidence = best_score
                current_last_seen = time
                last_switch_time = time
                candidate = None
                no_speaker_since = None
                _latency_ms = 0
                if switch_best_id in onset_first_seen:
                    _latency_ms = int(round(
                        (time - onset_first_seen[switch_best_id]) * 1000))
                switches.append({
                    "from": _prev_target,
                    "to": switch_best_id, "time": round(time, 3),
                    "latency_ms": _latency_ms, "onset": True,
                    "kind": "FORCE_SWITCH",
                })
                _log.info(
                    "[TARGET_SWITCH] ts=%.2fs from=%s to=%s reason=%s "
                    "confidence=%.2f latency_ms=%d",
                    time, _prev_target, switch_best_id,
                    "force_gap", round(decide_score, 2), _latency_ms,
                )

            else:
                # Normal hold-then-switch path, plus the Phase 4.2 onset
                # fast path: an onset-confirmed candidate with a WIDE
                # margin may switch after onset_fast_hold_ms instead of
                # the full hysteresis. The wider margin (not just speed)
                # is what keeps blips from flipping the camera.
                if candidate is None or candidate.speaker_id != switch_best_id:
                    candidate = _Candidate(switch_best_id, time, best_score)

                from .speaker_score import (  # local: keeps import graph light
                    movement_cost, switch_confidence)

                cand_center = last_center.get(switch_best_id)
                cur_center = last_center.get(current) if current else None
                move_cost = movement_cost(
                    cand_center, cur_center, movement_weight, movement_full_at)
                vis_pen = 0.0
                for _obs in frame:
                    if _obs.speaker_id != switch_best_id:
                        continue
                    _area = _obs.face.width * _obs.face.height
                    _small = 1.0 - min(1.0, _area / 0.04)
                    _gap = min(_obs.face.center_x, 1.0 - _obs.face.center_x,
                               _obs.face.center_y, 1.0 - _obs.face.center_y)
                    _edge = max(0.0, 1.0 - _gap / 0.08)
                    vis_pen = visibility_weight * (0.5 * _small + 0.5 * _edge)
                    break
                switch_score = switch_confidence(
                    decide_score, 0.0, move_cost, vis_pen)
                # Geometry raises the BAR (wider required margin), it never
                # vetoes outright: decisive evidence still switches across
                # the frame, while tie-ish far jumps are held. The cost
                # DECAYS as the candidate sustains its lead (patience, not
                # impossibility): a lead held past 2x the normal hold only
                # needs the configured margin. The onset fast path keeps
                # the plain onset margin: a confirmed rise IS the evidence,
                # travel cost stays on the normal path.
                geo_cost = min(0.15, 0.5 * move_cost + 0.5 * vis_pen)
                held_ratio = ((time - candidate.start + sample_step)
                              / max(min_switch_seconds, 1e-9))
                geo_factor = max(0.0, 1.0 - held_ratio / 2.0)
                required_margin = switch_margin + geo_cost * geo_factor
                fast_onset = (
                    onset_confirmed
                    and (decide_score - current_score) >= onset_switch_margin
                )
                required_hold = ((onset_fast_hold_ms / 1000.0)
                                 if fast_onset else min_switch_seconds)
                held_enough = (
                    (time - candidate.start + sample_step) >= required_hold
                )
                margin_ok = (decide_score - current_score) >= required_margin
                _log.debug(
                    "[TARGET_CANDIDATE] ts=%.3fs candidate=%s "
                    "speaker_conf=%.2f switch_conf=%.2f margin=%.2f "
                    "req_margin=%.2f onset=%d move_cost=%.2f vis_pen=%.2f",
                    time, switch_best_id,
                    decide_score, switch_score,
                    decide_score - current_score, required_margin,
                    1 if fast_onset else 0,
                    move_cost, vis_pen,
                )

                if held_enough and cooldown_ok and margin_ok:
                    _decision = "ONSET_SWITCH" if fast_onset else "SWITCH"
                    _log.debug(
                        "[SPEAKER_STATE] ts=%.3fs current=%s candidate=%s "
                        "current_score=%.2f candidate_score=%.2f "
                        "candidate_duration=%dms required=%dms margin=%.2f decision=%s",
                        time, current, switch_best_id,
                        current_score, decide_score,
                        candidate_duration_ms, required_ms, switch_margin,
                        _decision,
                    )
                    _onset_at = onset_first_seen.get(switch_best_id)
                    _latency_ms = (int(round((time - _onset_at) * 1000))
                                   if _onset_at is not None else 0)
                    switches.append({
                        "from": current, "to": switch_best_id,
                        "time": round(time, 3), "latency_ms": _latency_ms,
                        "onset": bool(fast_onset or _onset_at is not None),
                        "kind": _decision,
                    })
                    _log.info(
                        "[TARGET_SWITCH] ts=%.2fs from=%s to=%s reason=%s "
                        "confidence=%.2f latency_ms=%d",
                        time, current, switch_best_id,
                        "strong_onset" if fast_onset else "sustained_margin",
                        round(switch_score, 2), _latency_ms,
                    )
                    if current is not None and segment_start < time:
                        segments.append(SpeakerSegment(
                            start=segment_start, end=time,
                            speaker_id=current, confidence=segment_confidence,
                        ))
                    current = switch_best_id
                    segment_start = candidate.start
                    segment_confidence = candidate.score
                    current_last_seen = time
                    last_switch_time = time
                    candidate = None
                    no_speaker_since = None
                else:
                    _log.debug(
                        "[SPEAKER_STATE] ts=%.3fs current=%s candidate=%s "
                        "current_score=%.2f candidate_score=%.2f "
                        "candidate_duration=%dms required=%dms margin=%.2f decision=HOLD",
                        time, current if current else "none", switch_best_id,
                        current_score, decide_score,
                        candidate_duration_ms, required_ms, switch_margin,
                    )

        time = round(time + sample_step, 6)

    # ---- Close final segment ----
    if current is not None and segment_start < duration:
        segments.append(SpeakerSegment(
            start=segment_start, end=duration,
            speaker_id=current, confidence=segment_confidence,
        ))
    if metrics_out is not None:
        try:
            from .speaker_score import count_false_switches

            _false = count_false_switches(segments)
        except Exception:
            _false = 0
        _acq_ms = None
        if first_speech_time is not None and first_activate_time is not None:
            _acq_ms = int(round(
                (first_activate_time - first_speech_time) * 1000))
        metrics_out.update({
            "switches": switches,
            "switch_count": len(switches),
            "false_switch_count": _false,
            "ambiguous_holds": ambiguous_holds,
            "onset_speakers": sorted(onset_first_seen),
            "acquisition_latency_ms": _acq_ms,
            "segment_count": len(segments),
        })
    return segments


def make_crop_instructions(
    timeline: list[SpeakerSegment],
    observations: list[FaceObservation],
    aspect_width: int = 9,
    aspect_height: int = 16,
    safe_margin: float = 0.08,
    src_w: int = 0,
    src_h: int = 0,
) -> list[CropInstruction]:
    """Map speaker segments to a smoothed, safe portrait crop window.

    ``src_w`` and ``src_h`` are the actual source video dimensions in pixels.
    When provided, the crop fractions stored in each CropInstruction are
    computed correctly for the source aspect ratio so that the renderer
    (render_dynamic_vertical) can use center_x/center_y for proper
    source-aware positioning.

    The ``width``/``height`` fields are set to the fraction of the source that
    the portrait window covers so that any legacy consumer that reads them
    gets a geometrically accurate value.  The primary renderer (media.py)
    derives the pixel crop from center + source dimensions anyway.
    """
    if aspect_width <= 0 or aspect_height <= 0 or not 0 <= safe_margin < 0.5:
        raise ValueError("invalid crop parameters")
    # If we have actual source dimensions, compute the fraction of source
    # width/height occupied by the portrait window (so width*src_w gives the
    # correct pixel crop width, and similarly for height).
    if src_w > 0 and src_h > 0:
        # Portrait crop pixel width = src_h * aspect_width / aspect_height
        # (largest 9:16 window fitting into the source, limited by src_w).
        crop_px_w = min(src_w, src_h * aspect_width / aspect_height)
        crop_px_h = crop_px_w * aspect_height / aspect_width
        crop_width = crop_px_w / src_w
        crop_height = crop_px_h / src_h
    else:
        # Fallback (source dimensions unknown): use normalised fractions.
        crop_width = min(1.0, aspect_width / aspect_height)
        crop_height = min(1.0, crop_width * aspect_height / aspect_width)
    by_speaker: dict[str, list[FaceObservation]] = {}
    for item in observations:
        by_speaker.setdefault(item.speaker_id, []).append(item)
    result = []
    for segment in timeline:
        frames = [
            item for item in by_speaker.get(segment.speaker_id, [])
            if segment.start <= item.timestamp <= segment.end
        ]
        if frames:
            center_x = sum(item.face.center_x for item in frames) / len(frames)
            center_y = sum(item.face.center_y for item in frames) / len(frames)
        else:
            center_x, center_y = 0.5, 0.5
        center_x = min(1 - safe_margin - crop_width / 2, max(safe_margin + crop_width / 2, center_x))
        center_y = min(1 - crop_height / 2, max(crop_height / 2, center_y))
        result.append(CropInstruction(
            start=segment.start, end=segment.end, speaker_id=segment.speaker_id,
            center_x=center_x, center_y=center_y, width=crop_width,
            height=crop_height,
        ))
    return result


def make_split_screen_instructions(
    segment: SpeakerSegment,
    observations: list[FaceObservation],
    safe_margin: float = 0.08,
) -> list[CropInstruction]:
    """Legacy split-pane builder (two video layers).

    Phase 4.2 REMOVED split-screen from all production paths (§52): kept
    only so historical artifacts/tests referencing the symbol keep
    importing. Always returns [] — callers must use single-crop layout.
    """
    return []


def make_pair_crop_instructions(
    segment: SpeakerSegment,
    observations: list[FaceObservation],
    src_w: int = 0,
    src_h: int = 0,
    safe_margin: float = 0.08,
    max_span: float = 0.72,
    min_edge: float = 0.02,
) -> list[CropInstruction]:
    """One comfortable 9:16 crop holding the top-2 active faces, or [].

    Feasibility (all must pass, else caller falls back to the single
    leader — never an empty-center shot, never split-screen):
    union span within max_span, both faces fully inside with min_edge.
    """
    active = sorted(
        (
            item for item in observations
            if segment.start <= item.timestamp <= segment.end
        ),
        key=lambda item: item.active_score,
        reverse=True,
    )
    speakers: list[FaceObservation] = []
    for item in active:
        if item.speaker_id not in {value.speaker_id for value in speakers}:
            speakers.append(item)
        if len(speakers) == 2:
            break
    if len(speakers) < 2:
        return []
    boxes = [
        (item.face.x, item.face.y,
         item.face.x + item.face.width, item.face.y + item.face.height)
        for item in speakers
    ]
    span = max(b[2] for b in boxes) - min(b[0] for b in boxes)
    if span > max_span:
        return []
    ux1, uy1 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    ux2, uy2 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    ucx, ucy = (ux1 + ux2) / 2, (uy1 + uy2) / 2
    if src_w > 0 and src_h > 0:
        crop_px_w = min(src_w, src_h * 9 / 16)
        crop_px_h = crop_px_w * 16 / 9
        crop_width = crop_px_w / src_w
        crop_height = crop_px_h / src_h
    else:
        crop_width = min(1.0, 9 / 16)
        crop_height = min(1.0, crop_width * 16 / 9)
    center_x = min(1 - safe_margin - crop_width / 2,
                   max(safe_margin + crop_width / 2, ucx))
    center_y = min(1 - crop_height / 2, max(crop_height / 2, ucy))
    left, top = center_x - crop_width / 2, center_y - crop_height / 2
    for x1, y1, x2, y2 in boxes:
        if not (x1 >= left + min_edge - 1e-9 and x2 <= left + crop_width - min_edge + 1e-9
                and y1 >= top + min_edge - 1e-9 and y2 <= top + crop_height - min_edge + 1e-9):
            return []
    return [CropInstruction(
        start=segment.start, end=segment.end,
        speaker_id=f"{speakers[0].speaker_id}+{speakers[1].speaker_id}",
        center_x=center_x, center_y=center_y,
        width=crop_width, height=crop_height, layout="single",
    )]


def make_layout_instructions(
    timeline: list[SpeakerSegment],
    observations: list[FaceObservation],
    src_w: int = 0,
    src_h: int = 0,
) -> list[CropInstruction]:
    """Single-crop layout for every segment (no split-screen, §52).

    Tries a comfortable two-face crop first; falls back to the single
    leader crop (never an empty-center shot).
    """
    result: list[CropInstruction] = []
    for segment in timeline:
        pair = make_pair_crop_instructions(
            segment, observations, src_w=src_w, src_h=src_h)
        if pair:
            result.extend(pair)
        else:
            result.extend(make_crop_instructions([segment], observations, src_w=src_w, src_h=src_h))
    return result
