"""Centralized Dynamic Framing configuration (Phase 1).

Single source of truth for all framing tunables. Every value can be
overridden via environment (``GAECLIP_FRAMING_*``) without code changes;
no database migration and no API contract change is required because the
worker always runs auto-framing with these process-level settings.

Legacy module-level constants in ``autoframing.py`` are kept as importable
aliases during transition but new code must read from here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _getenv_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _getenv_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _getenv_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _getenv_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class FramingConfig:
    """All tunables for dynamic framing. Defaults preserve the audited
    (pre-Phase-1) behaviour unless noted as an intentional Phase-1 change."""

    # -- analysis input -------------------------------------------------
    analysis_max_width: int = 640        # PHASE 1 change (was: full source res)
    analysis_fps: float = 12.0           # QUALITY phase (was 6.0): denser anchors only
    # -- adaptive sampling + performance modes (Phase 6) ----------------
    # Decode at most ANALYSIS_FPS_MAX; the sampler skips detector runs
    # (stride 2) while the scene is still, never below ANALYSIS_FPS_MIN
    # effective rate. Timestamps always preserved (stride, never resample).
    adaptive_sampling: bool = True
    analysis_fps_min: float = 6.0
    analysis_fps_max: float = 15.0
    # Motion score below this for STILL_WINDOWS straight windows = still.
    still_threshold: float = 0.02
    still_windows: int = 4
    # Soft RAM guard (MiB, process RSS): exceeded -> drop to fps_min + log.
    # Never fails analysis; 0 disables.
    max_analysis_memory_mb: float = 4000.0
    # Perf mode for ANALYSIS only (encode stays on GAECLIP_RENDER_PROFILE):
    # "dev" -> 4 fps / 480 px / no adaptive; "quality" -> config values.
    perf_mode: str = "quality"
    audio_sample_rate: int = 16000
    vad_window_sec: float = 0.1
    max_faces: int = 6
    # -- VAD method + post-processing (Phase 5) -------------------------
    # "peak": legacy max(peak*0.15, 1e-4) threshold (unchanged default).
    # "noise_floor": percentile-based adaptive threshold that survives
    # mixed-level audio (loud burst no longer masks quiet speech).
    vad_method: str = "peak"
    vad_noise_percentile: float = 30.0
    vad_noise_ratio: float = 4.0
    # Hangover bridges sub-200ms gaps; min-speech kills flicker blips.
    vad_hangover_ms: int = 200
    vad_min_speech_ms: int = 150

    # -- scene-cut detection (numpy only, no new dependency) ------------
    # Threshold 0.25 (measured: real wide<->solo cuts score 0.28-0.35 on
    # Titik Kumpul footage; 0.35 caught none). Interval 500ms so rapid
    # angle ping-pong (~0.7s apart) isn't swallowed.
    scene_cut_enabled: bool = True
    scene_cut_threshold: float = 0.25    # normalized mean-abs-diff 0..1
    scene_cut_min_interval_ms: int = 500
    scene_cut_thumb_width: int = 96

    # -- tracking (IoUTracker, kept for Phase 1) ------------------------
    iou_match_threshold: float = 0.3
    max_missed_samples: int = 4
    reacquire_missed: int = 12
    reacquire_iou: float = 0.5

    # -- speaking evidence thresholds -----------------------------------
    min_active_score: float = 0.25
    # -- Phase 4: multi-person acquisition & association -----------------
    # Runner-up margin: best must clear second-best by this to be
    # switchable/activatable; otherwise AMBIGUOUS (hold incumbent).
    speaker_min_margin: float = 0.12
    overlap_ambiguity_margin: float = 0.12
    # Speech onset: rising lip evidence inside onset_window_ms after VAD
    # goes active earns a capped bonus (accelerates force-switch path).
    # Window MUST exceed ~2x the observation step (0.5s): with a 300ms
    # window a 0.5s-cadence history holds <2 samples and onset can never
    # confirm. 500ms keeps two samples and stays a genuine onset scale.
    speaker_onset_bonus: float = 0.15
    speaker_onset_rise_min: float = 0.25
    speaker_onset_window_ms: int = 500
    # Acquisition confirmation floor (force path still needs min_hold).
    target_acquisition_min_ms: int = 300
    # Incumbency tie-breaker: added while current evidence stays above
    # continuity_keep_fraction * threshold. Small by design.
    continuity_bonus: float = 0.05
    continuity_keep_fraction: float = 0.5
    # Transition lock after a switch; force margin still overrides.
    target_transition_lock_ms: int = 650
    # Group mode gates: need >= min speakers each with evidence >= min
    # confidence, and mean evidence >= activation threshold.
    group_min_active_speakers: int = 2
    group_min_confidence: float = 0.5
    group_activation_threshold: float = 0.5
    # Face quality modifier weight (0 = legacy behavior exactly).
    face_quality_weight: float = 0.1
    # -- Phase 4.2: per-face speech evidence + AV association -------------
    # Global VAD is a GATE, not per-face evidence: each face's audio score
    # is vad * (floor + (1-floor) * av_correlation). Floor 0.25 keeps a
    # weak signal when correlation is degenerate (flat audio), never zero.
    av_window_ms: int = 1200
    av_min_samples: int = 3
    av_neutral_floor: float = 0.25
    # Multi-window mouth motion: SHORT reacts (onset), MEDIUM decides,
    # LONG vetoes blips. Weights renormalize when a window is empty.
    mouth_short_ms: int = 150
    mouth_medium_ms: int = 500
    mouth_long_ms: int = 1000
    mouth_w_short: float = 0.3
    mouth_w_medium: float = 0.5
    mouth_w_long: float = 0.2
    # Head motion is CONTEXT only (turn != speech): damp moderate mouth
    # readings when the whole head moves; strong mouth never damped.
    head_damp_strength: float = 0.3
    # Onset fast path: onset-confirmed candidates may switch after this
    # hold (instead of the full 2s) if they ALSO clear onset_switch_margin
    # over the incumbent. Normal path stays conservative.
    onset_fast_hold_ms: int = 500
    onset_switch_margin: float = 0.15
    # Switch-confidence geometry costs (bounded 0-1 penalties/bonus).
    switch_movement_weight: float = 0.5
    switch_movement_full_at: float = 0.5
    switch_visibility_weight: float = 0.5
    switch_composition_bonus: float = 0.10
    # Crowded scenes: detector cap (compute is CNN-bound, not face-bound;
    # measured overhead 4->6 negligible) + prioritization keeps the active
    # target when detections overflow the cap.
    # (max_faces default raised 4 -> 6 below and in from_env.)
    # Lightweight spatial association for lost-target reacquire.
    spatial_continuity_weight: float = 0.7
    assoc_max_dist: float = 0.25
    assoc_max_size_ratio: float = 2.0
    assoc_max_gap_ms: int = 2000
    # Target-crop validation (single mode): |crop-anchor| beyond this
    # logs [CROP_TARGET_MISMATCH].
    target_crop_error_threshold: float = 0.12
    center_gap_protection: bool = True
    # -- two-speaker co-framing (Phase 4.1) --------------------------------
    # TWO_SPEAKER is a composition MODE, never a goal: a correct single
    # always beats a bad pair shot. Activation needs exactly-two relevant
    # candidates with sufficient evidence AND a geometrically feasible
    # single 9:16 crop (hard constraints below); score never overrides them.
    two_speaker_enabled: bool = True
    two_speaker_enter_threshold: float = 0.65
    two_speaker_exit_threshold: float = 0.50
    two_speaker_lock_ms: int = 1200
    two_speaker_min_duration_ms: int = 800
    two_speaker_max_distance: float = 0.45
    min_two_speaker_face_width: float = 0.06
    min_two_speaker_face_height: float = 0.10
    min_two_speaker_edge_margin: float = 0.02
    two_speaker_max_upscale: float = 2.0
    two_speaker_max_crop_width: float = 0.60
    two_speaker_max_empty_space: float = 0.45
    two_speaker_max_cutoff: float = 0.15
    two_speaker_temporal_overlap_ms: int = 1000
    two_speaker_movement_penalty: float = 0.10
    two_speaker_empty_space_penalty: float = 0.50
    two_speaker_face_size_penalty: float = 0.40
    two_speaker_speaker_visibility_threshold: float = 0.85
    two_speaker_min_score: float = 0.55
    # Empty-center protection: the gap between the two faces over crop
    # width above this rejects the pair (two tiny people around a void).
    two_speaker_max_gap_ratio: float = 0.50
    # -- person-presence safety (§21) --------------------------------------
    # No final crop may be person-free while faces exist in the section:
    # a crop holding no face above this visibility is corrected to the
    # nearest known face instead of parking on empty space.
    person_presence_min_visibility: float = 0.5
    # -- face detection recall (Phase 4.1 follow-up) -----------------------
    # BlazeFace short-side misses full profiles (~0.45) at the old 0.5
    # floor, blinding whole conversational stretches (measured: 82%
    # of sampled frames face-less on bike footage). 0.4 recovers the
    # profile class while staying above the torso-FP/noise tail (~0.29).
    # Low-conf boxes still score through confidence-weighted evidence,
    # so they inform presence without electing leaders on their own.
    face_detection_min_confidence: float = 0.4
    # -- speaker scoring (Phase 2 fusion; modifiers, not votes) ---------
    speaker_weight_visual: float = 0.55
    speaker_weight_audio: float = 0.35
    speaker_weight_continuity: float = 0.10
    speaker_face_quality: bool = True   # multiply by detection confidence
    speaker_spatial: bool = False       # center-bias modifier (default off)
    switch_margin: float = 0.10         # candidate must clear current+margin
    switch_hysteresis_sec: float = 2.0
    min_speaker_hold_ms: int = 600
    min_speaker_switch_interval_ms: int = 1500
    track_lost_grace_ms: int = 400
    no_speaker_fallback_ms: int = 1800
    # Anchor freshness: a target anchor computed from a detection older
    # than this is STALE (the face has moved on) and must not freeze the
    # camera; the manager re-acquires instead.
    anchor_stale_after_ms: int = 1000
    force_switch_score_margin: float = 0.40
    speaker_activation_threshold: float = 0.28
    speaker_switch_threshold: float = 0.33
    score_smoothing_alpha: float = 0.35
    # Unseen-speaker decay per timeline window (0-1): smoothed scores of
    # speakers with no observation in the window are multiplied by this,
    # so a stale-high incumbent yields to fresh evidence instead of
    # blocking switches forever. 1.0 = legacy (frozen) behavior.
    score_unseen_decay: float = 0.9

    # -- legacy EMA (retained for smooth_centers utility; analyze path
    #    uses the Kalman filter as its single primary trajectory filter) -
    smoothing_alpha: float = 0.20
    max_step: float = 0.06
    min_framing_movement: float = 0.012
    headroom_factor: float = 0.18
    safe_margin: float = 0.06
    # -- Kalman trajectory filter (Phase 3 primary filter) --------------
    # Tuned on a synthetic noisy walker (0.2->0.8 over 8s @0.5s sampling):
    # mean lag Kalman ~0.012 vs EMA(0.2) ~0.22, zero step overshoot.
    kalman_process_noise: float = 1e-3
    kalman_measurement_noise: float = 2e-2
    kalman_velocity_decay: float = 0.7
    # -- acquisition easing (fallback/restore/scene-start only) ---------
    # 400ms: responsive shorts pacing (was 600).
    easing_ms: int = 400
    easing_waypoint_sec: float = 0.25
    # -- virtual-camera motion shaping (QUALITY phase) ------------------
    # Deadzone: ignore target wander below these (normalized units);
    # vertical gate is wider (headroom stability). Exits ease over
    # exit_ease_s instead of snapping (kills hold-then-pop stutter).
    framing_deadzone_x: float = 0.04
    framing_deadzone_y: float = 0.06
    framing_deadzone_exit_ease_s: float = 1.0
    # Safety rails (only bind on glitches/teleports; deliberate eased
    # transitions are exempt). Units per second.
    max_crop_velocity: float = 0.6
    max_crop_acceleration: float = 3.0
    # Zoom: quantize size keyframes (MIN_SCALE_CHANGE doubles as the
    # shimmer threshold); transitions lerp size across boundaries.
    min_scale_change: float = 0.01
    max_zoom_speed: float = 0.5
    # Transition style for speaker switches AND acquisition/restore
    # easing ("sat-sat-sat" hard cuts vs smooth pans). "cut" emits a
    # same-timestamp boundary pair (renderer holds each side: no pan
    # frames); "ease" keeps the legacy eased substeps. Roll back via
    # FRAMING_TRANSITION_STYLE=ease without a rebuild.
    transition_style: str = "cut"
    # Speaker-switch easing window (timeline SWITCH only, ease style;
    # scene cuts stay hard cuts). 400ms: responsive shorts pacing (was 650).
    switch_transition_ms: int = 400
    switch_transition_steps: int = 8
    # Jump-event threshold (normalized units between consecutive keyframes,
    # same scene): telemetry tripwire, not a filter.
    jump_threshold: float = 0.15
    # -- output fps policy ----------------------------------------------
    # "source": follow probed section fps (24/25/30/50/60, clamped);
    # VFR or probe failure falls back to 30. Never the analysis rate.
    output_fps_mode: str = "source"
    output_fps_fallback: float = 30.0
    output_fps_min: float = 24.0
    output_fps_max: float = 60.0

    # -- adaptive merge (Phase 1 fix for trajectory collapse) -----------
    trajectory_sample_sec: float = 0.5
    max_merge_position_delta: float = 0.03   # normalized units (~3% width)
    max_merge_time_gap: float = 0.75         # seconds; larger gap forces split
    max_merge_scale_delta: float = 0.02      # reserved (crop size fixed in P1)

    # -- composition / output geometry ----------------------------------
    output_width: int = 1080
    output_height: int = 1920
    output_fps: int = 30
    group_margin: float = 0.06
    max_crop_movement: float = 0.06          # reserved for Phase 3 easing
    face_visibility_min: float = 0.85        # min face fraction inside crop
    face_visibility_edge_tol: float = 0.05   # tolerated edge overshoot

    # -- quality guard ---------------------------------------------------
    quality_guard_enabled: bool = True
    max_upscale_factor: float = 2.0          # matches vertical ladder limit

    # -- encoding profiles (production default untouched) ----------------
    production_preset: str = "medium"
    dev_preset: str = "veryfast"

    @classmethod
    def from_env(cls) -> "FramingConfig":
        """Build config with GAECLIP_FRAMING_* overrides applied."""
        prefix = "GAECLIP_FRAMING_"
        return cls(
            analysis_max_width=_getenv_int(prefix + "ANALYSIS_MAX_WIDTH", 640),
            analysis_fps=_getenv_float(prefix + "ANALYSIS_FPS", 12.0),
            adaptive_sampling=_getenv_bool(prefix + "ADAPTIVE_SAMPLING", True),
            analysis_fps_min=_getenv_float(prefix + "ANALYSIS_FPS_MIN", 6.0),
            analysis_fps_max=_getenv_float(prefix + "ANALYSIS_FPS_MAX", 15.0),
            still_threshold=_getenv_float(prefix + "STILL_THRESHOLD", 0.02),
            still_windows=_getenv_int(prefix + "STILL_WINDOWS", 4),
            max_analysis_memory_mb=_getenv_float(prefix + "MAX_ANALYSIS_MEMORY_MB", 4000.0),
            perf_mode=_getenv_str(prefix + "PERF_MODE", "quality"),
            audio_sample_rate=_getenv_int(prefix + "AUDIO_SAMPLE_RATE", 16000),
            vad_window_sec=_getenv_float(prefix + "VAD_WINDOW_SEC", 0.1),
            vad_method=_getenv_str(prefix + "VAD_METHOD", "peak"),
            vad_noise_percentile=_getenv_float(prefix + "VAD_NOISE_PERCENTILE", 30.0),
            vad_noise_ratio=_getenv_float(prefix + "VAD_NOISE_RATIO", 4.0),
            vad_hangover_ms=_getenv_int(prefix + "VAD_HANGOVER_MS", 200),
            vad_min_speech_ms=_getenv_int(prefix + "VAD_MIN_SPEECH_MS", 150),
            max_faces=_getenv_int(prefix + "MAX_FACES", 6),
            scene_cut_enabled=_getenv_bool(prefix + "SCENE_CUT_ENABLED", True),
            scene_cut_threshold=_getenv_float(prefix + "SCENE_CUT_THRESHOLD", 0.25),
            scene_cut_min_interval_ms=_getenv_int(prefix + "SCENE_CUT_MIN_INTERVAL_MS", 500),
            scene_cut_thumb_width=_getenv_int(prefix + "SCENE_CUT_THUMB_WIDTH", 96),
            iou_match_threshold=_getenv_float(prefix + "IOU_MATCH_THRESHOLD", 0.3),
            max_missed_samples=_getenv_int(prefix + "MAX_MISSED_SAMPLES", 4),
            reacquire_missed=_getenv_int(prefix + "REACQUIRE_MISSED", 12),
            reacquire_iou=_getenv_float(prefix + "REACQUIRE_IOU", 0.5),
            min_active_score=_getenv_float(prefix + "MIN_ACTIVE_SCORE", 0.25),
            speaker_min_margin=_getenv_float(prefix + "SPEAKER_MIN_MARGIN", 0.12),
            overlap_ambiguity_margin=_getenv_float(prefix + "OVERLAP_AMBIGUITY_MARGIN", 0.12),
            speaker_onset_bonus=_getenv_float(prefix + "SPEAKER_ONSET_BONUS", 0.15),
            speaker_onset_rise_min=_getenv_float(prefix + "SPEAKER_ONSET_RISE_MIN", 0.25),
            speaker_onset_window_ms=_getenv_int(prefix + "SPEAKER_ONSET_WINDOW_MS", 500),
            target_acquisition_min_ms=_getenv_int(prefix + "TARGET_ACQUISITION_MIN_MS", 300),
            continuity_bonus=_getenv_float(prefix + "CONTINUITY_BONUS", 0.05),
            continuity_keep_fraction=_getenv_float(prefix + "CONTINUITY_KEEP_FRACTION", 0.5),
            target_transition_lock_ms=_getenv_int(prefix + "TARGET_TRANSITION_LOCK_MS", 650),
            group_min_active_speakers=_getenv_int(prefix + "GROUP_MIN_ACTIVE_SPEAKERS", 2),
            group_min_confidence=_getenv_float(prefix + "GROUP_MIN_CONFIDENCE", 0.5),
            group_activation_threshold=_getenv_float(prefix + "GROUP_ACTIVATION_THRESHOLD", 0.5),
            face_quality_weight=_getenv_float(prefix + "FACE_QUALITY_WEIGHT", 0.1),
            av_window_ms=_getenv_int(prefix + "AV_WINDOW_MS", 1200),
            av_min_samples=_getenv_int(prefix + "AV_MIN_SAMPLES", 3),
            av_neutral_floor=_getenv_float(prefix + "AV_NEUTRAL_FLOOR", 0.25),
            mouth_short_ms=_getenv_int(prefix + "MOUTH_SHORT_MS", 150),
            mouth_medium_ms=_getenv_int(prefix + "MOUTH_MEDIUM_MS", 500),
            mouth_long_ms=_getenv_int(prefix + "MOUTH_LONG_MS", 1000),
            mouth_w_short=_getenv_float(prefix + "MOUTH_W_SHORT", 0.3),
            mouth_w_medium=_getenv_float(prefix + "MOUTH_W_MEDIUM", 0.5),
            mouth_w_long=_getenv_float(prefix + "MOUTH_W_LONG", 0.2),
            head_damp_strength=_getenv_float(prefix + "HEAD_DAMP_STRENGTH", 0.3),
            onset_fast_hold_ms=_getenv_int(prefix + "ONSET_FAST_HOLD_MS", 500),
            onset_switch_margin=_getenv_float(prefix + "ONSET_SWITCH_MARGIN", 0.15),
            switch_movement_weight=_getenv_float(prefix + "SWITCH_MOVEMENT_WEIGHT", 0.5),
            switch_movement_full_at=_getenv_float(prefix + "SWITCH_MOVEMENT_FULL_AT", 0.5),
            switch_visibility_weight=_getenv_float(prefix + "SWITCH_VISIBILITY_WEIGHT", 0.5),
            switch_composition_bonus=_getenv_float(prefix + "SWITCH_COMPOSITION_BONUS", 0.10),
            spatial_continuity_weight=_getenv_float(prefix + "SPATIAL_CONTINUITY_WEIGHT", 0.7),
            assoc_max_dist=_getenv_float(prefix + "ASSOC_MAX_DIST", 0.25),
            assoc_max_size_ratio=_getenv_float(prefix + "ASSOC_MAX_SIZE_RATIO", 2.0),
            assoc_max_gap_ms=_getenv_int(prefix + "ASSOC_MAX_GAP_MS", 2000),
            target_crop_error_threshold=_getenv_float(prefix + "TARGET_CROP_ERROR_THRESHOLD", 0.12),
            center_gap_protection=_getenv_bool(prefix + "CENTER_GAP_PROTECTION", True),
            two_speaker_enabled=_getenv_bool(prefix + "TWO_SPEAKER_ENABLED", True),
            two_speaker_enter_threshold=_getenv_float(prefix + "TWO_SPEAKER_ENTER_THRESHOLD", 0.65),
            two_speaker_exit_threshold=_getenv_float(prefix + "TWO_SPEAKER_EXIT_THRESHOLD", 0.50),
            two_speaker_lock_ms=_getenv_int(prefix + "TWO_SPEAKER_LOCK_MS", 1200),
            two_speaker_min_duration_ms=_getenv_int(prefix + "TWO_SPEAKER_MIN_DURATION_MS", 800),
            two_speaker_max_distance=_getenv_float(prefix + "TWO_SPEAKER_MAX_DISTANCE", 0.45),
            min_two_speaker_face_width=_getenv_float(prefix + "MIN_TWO_SPEAKER_FACE_WIDTH", 0.06),
            min_two_speaker_face_height=_getenv_float(prefix + "MIN_TWO_SPEAKER_FACE_HEIGHT", 0.10),
            min_two_speaker_edge_margin=_getenv_float(prefix + "MIN_TWO_SPEAKER_EDGE_MARGIN", 0.02),
            two_speaker_max_upscale=_getenv_float(prefix + "TWO_SPEAKER_MAX_UPSCALE", 2.0),
            two_speaker_max_crop_width=_getenv_float(prefix + "TWO_SPEAKER_MAX_CROP_WIDTH", 0.60),
            two_speaker_max_empty_space=_getenv_float(prefix + "TWO_SPEAKER_MAX_EMPTY_SPACE", 0.45),
            two_speaker_max_cutoff=_getenv_float(prefix + "TWO_SPEAKER_MAX_CUTOFF", 0.15),
            two_speaker_temporal_overlap_ms=_getenv_int(prefix + "TWO_SPEAKER_TEMPORAL_OVERLAP_MS", 1000),
            two_speaker_movement_penalty=_getenv_float(prefix + "TWO_SPEAKER_MOVEMENT_PENALTY", 0.10),
            two_speaker_empty_space_penalty=_getenv_float(prefix + "TWO_SPEAKER_EMPTY_SPACE_PENALTY", 0.50),
            two_speaker_face_size_penalty=_getenv_float(prefix + "TWO_SPEAKER_FACE_SIZE_PENALTY", 0.40),
            two_speaker_speaker_visibility_threshold=_getenv_float(prefix + "TWO_SPEAKER_SPEAKER_VISIBILITY_THRESHOLD", 0.85),
            two_speaker_min_score=_getenv_float(prefix + "TWO_SPEAKER_MIN_SCORE", 0.55),
            two_speaker_max_gap_ratio=_getenv_float(prefix + "TWO_SPEAKER_MAX_GAP_RATIO", 0.50),
            person_presence_min_visibility=_getenv_float(prefix + "PERSON_PRESENCE_MIN_VISIBILITY", 0.5),
            face_detection_min_confidence=_getenv_float(prefix + "FACE_DETECTION_MIN_CONFIDENCE", 0.4),
            speaker_weight_visual=_getenv_float(prefix + "SPEAKER_WEIGHT_VISUAL", 0.55),
            speaker_weight_audio=_getenv_float(prefix + "SPEAKER_WEIGHT_AUDIO", 0.35),
            speaker_weight_continuity=_getenv_float(prefix + "SPEAKER_WEIGHT_CONTINUITY", 0.10),
            speaker_face_quality=_getenv_bool(prefix + "SPEAKER_FACE_QUALITY", True),
            speaker_spatial=_getenv_bool(prefix + "SPEAKER_SPATIAL", False),
            switch_margin=_getenv_float(prefix + "SWITCH_MARGIN", 0.10),
            switch_hysteresis_sec=_getenv_float(prefix + "SWITCH_HYSTERESIS_SEC", 2.0),
            min_speaker_hold_ms=_getenv_int(prefix + "MIN_SPEAKER_HOLD_MS", 900),
            min_speaker_switch_interval_ms=_getenv_int(prefix + "MIN_SPEAKER_SWITCH_INTERVAL_MS", 1000),
            track_lost_grace_ms=_getenv_int(prefix + "TRACK_LOST_GRACE_MS", 700),
            no_speaker_fallback_ms=_getenv_int(prefix + "NO_SPEAKER_FALLBACK_MS", 1800),
            anchor_stale_after_ms=_getenv_int(prefix + "ANCHOR_STALE_AFTER_MS", 1000),
            force_switch_score_margin=_getenv_float(prefix + "FORCE_SWITCH_SCORE_MARGIN", 0.40),
            speaker_activation_threshold=_getenv_float(prefix + "SPEAKER_ACTIVATION_THRESHOLD", 0.28),
            speaker_switch_threshold=_getenv_float(prefix + "SPEAKER_SWITCH_THRESHOLD", 0.33),
            score_smoothing_alpha=_getenv_float(prefix + "SCORE_SMOOTHING_ALPHA", 0.35),
            score_unseen_decay=_getenv_float(prefix + "SCORE_UNSEEN_DECAY", 0.9),
            smoothing_alpha=_getenv_float(prefix + "SMOOTHING_ALPHA", 0.20),
            max_step=_getenv_float(prefix + "MAX_STEP", 0.06),
            min_framing_movement=_getenv_float(prefix + "MIN_FRAMING_MOVEMENT", 0.012),
            kalman_process_noise=_getenv_float(prefix + "KALMAN_PROCESS_NOISE", 1e-3),
            kalman_measurement_noise=_getenv_float(prefix + "KALMAN_MEASUREMENT_NOISE", 2e-2),
            kalman_velocity_decay=_getenv_float(prefix + "KALMAN_VELOCITY_DECAY", 0.7),
            easing_ms=_getenv_int(prefix + "EASING_MS", 400),
            easing_waypoint_sec=_getenv_float(prefix + "EASING_WAYPOINT_SEC", 0.25),
            framing_deadzone_x=_getenv_float(prefix + "FRAMING_DEADZONE_X", 0.04),
            framing_deadzone_y=_getenv_float(prefix + "FRAMING_DEADZONE_Y", 0.06),
            framing_deadzone_exit_ease_s=_getenv_float(prefix + "FRAMING_DEADZONE_EXIT_EASE_S", 1.0),
            max_crop_velocity=_getenv_float(prefix + "MAX_CROP_VELOCITY", 0.6),
            max_crop_acceleration=_getenv_float(prefix + "MAX_CROP_ACCELERATION", 3.0),
            min_scale_change=_getenv_float(prefix + "MIN_SCALE_CHANGE", 0.01),
            max_zoom_speed=_getenv_float(prefix + "MAX_ZOOM_SPEED", 0.5),
            switch_transition_ms=_getenv_int(prefix + "SWITCH_TRANSITION_MS", 400),
            switch_transition_steps=_getenv_int(prefix + "SWITCH_TRANSITION_STEPS", 8),
            transition_style=_getenv_str(prefix + "TRANSITION_STYLE", "cut"),
            jump_threshold=_getenv_float(prefix + "JUMP_THRESHOLD", 0.15),
            output_fps_mode=_getenv_str(prefix + "OUTPUT_FPS_MODE", "source"),
            output_fps_fallback=_getenv_float(prefix + "OUTPUT_FPS_FALLBACK", 30.0),
            output_fps_min=_getenv_float(prefix + "OUTPUT_FPS_MIN", 24.0),
            output_fps_max=_getenv_float(prefix + "OUTPUT_FPS_MAX", 60.0),
            headroom_factor=_getenv_float(prefix + "HEADROOM_FACTOR", 0.18),
            safe_margin=_getenv_float(prefix + "SAFE_MARGIN", 0.06),
            trajectory_sample_sec=_getenv_float(prefix + "TRAJECTORY_SAMPLE_SEC", 0.5),
            max_merge_position_delta=_getenv_float(prefix + "MAX_MERGE_POSITION_DELTA", 0.03),
            max_merge_time_gap=_getenv_float(prefix + "MAX_MERGE_TIME_GAP", 0.75),
            max_merge_scale_delta=_getenv_float(prefix + "MAX_MERGE_SCALE_DELTA", 0.02),
            output_width=_getenv_int(prefix + "OUTPUT_WIDTH", 1080),
            output_height=_getenv_int(prefix + "OUTPUT_HEIGHT", 1920),
            output_fps=_getenv_int(prefix + "OUTPUT_FPS", 30),
            group_margin=_getenv_float(prefix + "GROUP_MARGIN", 0.06),
            max_crop_movement=_getenv_float(prefix + "MAX_CROP_MOVEMENT", 0.06),
            face_visibility_min=_getenv_float(prefix + "FACE_VISIBILITY_MIN", 0.85),
            face_visibility_edge_tol=_getenv_float(prefix + "FACE_VISIBILITY_EDGE_TOL", 0.05),
            quality_guard_enabled=_getenv_bool(prefix + "QUALITY_GUARD_ENABLED", True),
            max_upscale_factor=_getenv_float(prefix + "MAX_UPSCALE_FACTOR", 2.0),
            production_preset=_getenv_str(prefix + "PRODUCTION_PRESET", "medium"),
            dev_preset=_getenv_str(prefix + "DEV_PRESET", "veryfast"),
        )


DEFAULT_CONFIG: FramingConfig = FramingConfig()


def use_hard_cuts(config: FramingConfig | None = None) -> bool:
    """True unless transition_style is explicitly "ease".

    Fail-safe toward cuts: unknown values behave like "cut" so a typo
    can never silently reintroduce pans the user asked to remove.
    """
    style = (getattr(config, "transition_style", "cut") or "cut").strip().lower()
    return style != "ease"


# Render quality tiers: (libx264 preset, CRF). Higher tier = slower + sharper.
RENDER_PROFILES: dict[str, tuple[str, int]] = {
    "fast": ("veryfast", 22),
    "balanced": ("medium", 20),
    "quality": ("medium", 18),
}
DEFAULT_RENDER_PROFILE = "quality"


def resolve_encode_profile(explicit: str | None = None) -> tuple[str, int]:
    """(preset, crf) for final renders. Explicit > RENDER_PROFILE env/DB
    default (quality). Legacy GAECLIP_RENDER_PROFILE=dev aliases to fast."""
    if explicit:
        profile = explicit.strip().lower()
    else:
        # Legacy env (dev/...) or default. DB-backed flow passes the
        # resolved Settings value as `explicit`, preserving DB>env>default.
        profile = os.getenv("GAECLIP_RENDER_PROFILE", "").strip().lower()
        if not profile:
            profile = DEFAULT_RENDER_PROFILE
    if profile == "dev":
        profile = "fast"
    if profile not in RENDER_PROFILES:
        profile = DEFAULT_RENDER_PROFILE
    return RENDER_PROFILES[profile]


def resolve_analysis_profile(explicit_fps: float | None = None,
                             explicit_width: int | None = None,
                             config: FramingConfig | None = None
                             ) -> tuple[float, int, bool]:
    """(decode_fps, max_width, adaptive) for analysis.

    Explicit args always win and pin a FIXED rate (deterministic callers).
    Otherwise PERF_MODE=dev pins cheap fixed (4 fps / 480 px); quality mode
    decodes at fps_max with motion-gated stride (effective rate >= fps_min).
    Encode profile stays separate.
    """
    cfg = config or DEFAULT_CONFIG
    if explicit_fps is not None or explicit_width is not None:
        fps = explicit_fps if explicit_fps is not None else cfg.analysis_fps
        width = explicit_width if explicit_width is not None else cfg.analysis_max_width
        return fps, width, False
    profile = os.getenv("GAECLIP_FRAMING_PERF_MODE", cfg.perf_mode).strip().lower()
    if profile == "dev":
        return 4.0, 480, False
    if cfg.adaptive_sampling:
        return cfg.analysis_fps_max, cfg.analysis_max_width, True
    return cfg.analysis_fps, cfg.analysis_max_width, False


def resolve_encode_preset(explicit: str | None = None,
                          config: FramingConfig | None = None) -> str:
    """Encode preset without changing the production default.

    - explicit preset always wins (API/tests).
    - GAECLIP_RENDER_PROFILE=dev selects the fast development preset.
    - otherwise the production preset (medium) is preserved.
    """
    if explicit:
        return explicit
    profile = os.getenv("GAECLIP_RENDER_PROFILE", "production").strip().lower()
    cfg = config or DEFAULT_CONFIG
    if profile == "dev":
        return cfg.dev_preset
    return cfg.production_preset
