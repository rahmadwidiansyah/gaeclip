"""Temporal filtering for crop trajectories (Phase 3). Pure + numpy-only.

Single primary trajectory filter: a constant-velocity Kalman filter over
``[x, y, s, vx, vy, vs]`` (center + log-scale + velocities). It replaces the
legacy center-EMA in the analyze path (``smooth_centers`` is retained as a
tested utility, not stacked after the Kalman filter).

Scale ``s`` is tracked for stability even though Phase 3 keeps crop size
fixed: the filter holds scale steady instead of inventing zoom.

Transitions
-----------
Timeline SWITCH events stay hard cuts (editorial standard). Easing applies
only to *acquisition-type* discontinuities (fallback acquisition, restore
after occlusion, scene-start): ``subdivide_eased_runs`` replaces one big
jump with a few waypoint instructions along an ease-in-out curve, which
trim/concat renders as a fast eased pan instead of a teleport.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class TrajectoryPoint:
    """One filtered trajectory state (debug/telemetry, pre-merge)."""

    timestamp: float
    x: float
    y: float
    scale: float  # crop-height fraction (linear domain)
    vx: float = 0.0
    vy: float = 0.0
    vs: float = 0.0
    track_id: str = ""
    scene_id: int = 0
    eased: bool = False


def ease_in_out(progress: float) -> float:
    """Cubic ease-in-out on [0, 1]; clamped, monotonic, symmetric."""
    p = min(1.0, max(0.0, progress))
    if p < 0.5:
        return 4.0 * p * p * p
    return 1.0 - ((-2.0 * p + 2.0) ** 3) / 2.0


class KalmanFilter:
    """Constant-velocity Kalman filter for [x, y, log_s, vx, vy, vs].

    Numpy-only, variable dt, position-only measurements. Missing
    measurements coast via predict-only steps (caller bize bounded gaps).
    """

    def __init__(self, process_noise: float = 1e-3,
                 measurement_noise: float = 2e-2,
                 velocity_decay: float = 0.85):
        self.q = process_noise
        self.r = measurement_noise
        # Mean-reverting velocity: faces start/stop rather than cruising.
        # Each predict multiplies velocity by this factor, so stale momentum
        # dies out instead of sailing past a stopped target (no floaty
        # overshoot), while sustained motion re-learns velocity every step
        # (no slew-cap lag penalty on genuine fast moves).
        self.velocity_decay = velocity_decay
        self.x: list[float] | None = None  # state vector, None until init
        # Covariance 6x6 as nested lists (no scipy dependency).
        self.p: list[list[float]] | None = None

    def reset(self, position: tuple[float, float, float]) -> None:
        """(Re)initialize at a measurement with zero velocity."""
        x, y, s = position
        self.x = [x, y, math.log(max(1e-6, s)), 0.0, 0.0, 0.0]
        big = 1.0
        self.p = [[big if i == j else 0.0 for j in range(6)] for i in range(6)]

    @staticmethod
    def _mat_vec_mul(mat: list[list[float]], vec: list[float]) -> list[float]:
        return [sum(row[j] * vec[j] for j in range(len(vec))) for row in mat]

    @staticmethod
    def _transpose(mat: list[list[float]]) -> list[list[float]]:
        return [list(row) for row in zip(*mat)]

    @staticmethod
    def _mat_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
        n, m, k = len(a), len(b[0]), len(b)
        return [[sum(a[i][t] * b[t][j] for t in range(k)) for j in range(m)]
                for i in range(n)]

    def _transition(self, dt: float) -> list[list[float]]:
        f = [[1.0 if i == j else 0.0 for j in range(6)] for i in range(6)]
        f[0][3] = dt
        f[1][4] = dt
        f[2][5] = dt
        return f

    def predict(self, dt: float) -> tuple[float, float, float]:
        """Advance the model without a measurement (coast)."""
        assert self.x is not None and self.p is not None, "call reset() first"
        dt = max(1e-6, dt)
        f = self._transition(dt)
        self.x = self._mat_vec_mul(f, self.x)
        # Mean-reverting velocity (see __init__): damps stale momentum.
        decay = self.velocity_decay ** (dt / 0.5)
        self.x[3] *= decay
        self.x[4] *= decay
        self.x[5] *= decay
        ft = self._transpose(f)
        self.p = self._mat_mul(self._mat_mul(f, self.p), ft)
        # Process noise grows with dt (discrete white-noise model, q scaled).
        for i in (0, 1, 2):
            self.p[i][i] += self.q * dt * dt
        for i in (3, 4, 5):
            self.p[i][i] += self.q
        x, y, ls, vx, vy, vs = self.x
        return x, y, math.exp(ls)

    def update(self, measurement: tuple[float, float, float]
               ) -> tuple[float, float, float]:
        """Fuse a position measurement; returns (x, y, scale).

        Full (small-matrix) Kalman update with H = [I_3 | 0]: the gain is
        6x3 so the innovation also corrects the unobserved VELOCITY states
        through the cross-covariances. Covariance follows the Joseph form
        for numerical stability.
        """
        assert self.x is not None and self.p is not None, "call reset() first"
        mx, my, ms = measurement
        z = [mx, my, math.log(max(1e-6, ms))]
        innov = [z[a] - self.x[a] for a in range(3)]
        # K[i][a] = P[i][a] / (P[a][a] + R), axes independent in R.
        k = [[self.p[i][a] / (self.p[a][a] + self.r) for a in range(3)]
             for i in range(6)]
        for i in range(6):
            self.x[i] += sum(k[i][a] * innov[a] for a in range(3))
        # Joseph form: P = (I-KH)P(I-KH)^T + K R K^T.
        ikh = [[(1.0 if i == j else 0.0) - (k[i][j] if j < 3 else 0.0)
                for j in range(6)] for i in range(6)]
        ikh_t = self._transpose(ikh)
        term1 = self._mat_mul(self._mat_mul(ikh, self.p), ikh_t)
        krk = [[sum(k[i][a] * self.r * k[j][a] for a in range(3))
                for j in range(6)] for i in range(6)]
        self.p = [[term1[i][j] + krk[i][j] for j in range(6)] for i in range(6)]
        return self.x[0], self.x[1], math.exp(self.x[2])

    def step(self, dt: float, measurement: tuple[float, float, float] | None
             ) -> tuple[float, float, float, float, float, float]:
        """One predict (+ optional update); returns (x, y, s, vx, vy, vs)."""
        if self.x is None:
            assert measurement is not None
            self.reset(measurement)
            x, y, s = measurement
            return x, y, s, 0.0, 0.0, 0.0
        self.predict(dt)
        if measurement is not None:
            self.update(measurement)
        assert self.x is not None
        x, y, ls, vx, vy, vs = self.x
        return x, y, math.exp(ls), vx, vy, vs


def split_contiguous(samples: list[tuple[float, str, float, float]],
                     scene_ids: list[int] | None = None
                     ) -> list[tuple[list[tuple[float, str, float, float]], int]]:
    """Group samples into (same-speaker, same-scene) contiguous runs.

    Returns [(run_samples, scene_id)]. Pure structural split; no smoothing.
    """
    if not samples:
        return []
    if scene_ids is None:
        scene_ids = [0] * len(samples)
    runs: list[tuple[list, int]] = []
    current: list = []
    current_sid: int = 0
    for sample, sid in zip(samples, scene_ids):
        if current and (sample[1] != current[0][1] or sid != current_sid):
            runs.append((current, current_sid))
            current = []
        if not current:
            current_sid = sid
        current.append(sample)
    if current:
        runs.append((current, current_sid))
    return runs


def filter_run(samples: list[tuple[float, str, float, float]],
               scene_id: int = 0,
               process_noise: float = 1e-3,
               measurement_noise: float = 2e-2,
               velocity_decay: float = 0.7,
               scale: float = 1.0) -> list[TrajectoryPoint]:
    """Kalman-filter one contiguous run; returns per-sample trajectory."""
    points: list[TrajectoryPoint] = []
    if not samples:
        return points
    kf = KalmanFilter(process_noise, measurement_noise, velocity_decay)
    prev_t: float | None = None
    for timestamp, speaker, cx, cy in samples:
        dt = timestamp - prev_t if prev_t is not None else 0.0
        x, y, s, vx, vy, vs = kf.step(max(dt, 1e-6), (cx, cy, scale))
        points.append(TrajectoryPoint(
            timestamp=timestamp, x=x, y=y, scale=s,
            vx=vx, vy=vy, vs=vs, track_id=speaker, scene_id=scene_id))
        prev_t = timestamp
    return points


def subdivide_eased_runs(
        merged: list[tuple[float, float, str, float, float]],
        eased_starts: set[float],
        smoothed_by_time: dict[float, tuple[float, float]],
        easing_s: float = 0.6,
        waypoint_step: float = 0.25,
        min_distance: float = 0.012
) -> tuple[list[tuple[float, float, str, float, float]], list[tuple[float, float]]]:
    """Replace acquisition-type run onsets with eased waypoint instructions.

    ``merged``: (start, end, speaker, cx, cy) runs (constant-center each).
    ``eased_starts``: run start times flagged by the caller (fallback /
    restore / scene-start acquisitions — never timeline SWITCH cuts).
    Each flagged run's first ``easing_s`` seconds are subdivided at
    ``waypoint_step`` along ease-in-out from the previous run's end center
    toward this run's own smoothed curve (offset-lerp: converges exactly,
    never overshoots). Waypoints closer than ``min_distance`` to the
    previous end are skipped (no instruction spam on near-identical
    restores). The first run and zero-length runs are never subdivided.

    Returns (runs, eased_spans) where eased_spans lists (span_start,
    span_end) for trajectory flagging.
    """
    if not merged:
        return [], []
    out: list[tuple[float, float, str, float, float]] = []
    spans: list[tuple[float, float]] = []
    prev_end_center: tuple[float, float] | None = None
    for start, end, speaker, cx, cy in merged:
        span = min(easing_s, end - start) if end - start > 1e-9 else 0.0
        dist = math.hypot(cx - prev_end_center[0], cy - prev_end_center[1]) \
            if prev_end_center is not None else 0.0
        if (start in eased_starts and prev_end_center is not None
                and span > 1e-9 and dist >= min_distance):
            # Waypoints live strictly inside [start, start+span]; the
            # remainder keeps the run constant. (Never stretch the final
            # waypoint to `end`: its reference lookup would land on the
            # NEXT run's curve and corrupt the handover.)
            n_steps = max(1, int(math.ceil(span / waypoint_step)))
            cursor = start
            for i in range(1, n_steps + 1):
                waypoint_end = start + span * i / n_steps
                # Reference curve value at this waypoint time.
                ref = smoothed_by_time.get(round(waypoint_end, 3),
                                           smoothed_by_time.get(round(cursor, 3), (cx, cy)))
                progress = ease_in_out((waypoint_end - start) / span) if span > 0 else 1.0
                ex = prev_end_center[0] + (ref[0] - prev_end_center[0]) * progress
                ey = prev_end_center[1] + (ref[1] - prev_end_center[1]) * progress
                out.append((round(cursor, 3), round(waypoint_end, 3), speaker, ex, ey))
                cursor = waypoint_end
            spans.append((round(start, 3), round(start + span, 3)))
            if cursor < end - 1e-9:
                out.append((round(cursor, 3), end, speaker, cx, cy))
        else:
            out.append((start, end, speaker, cx, cy))
        prev_end_center = (cx, cy)
    return out, spans


def trajectory_to_json(points: list[TrajectoryPoint]) -> list[dict]:
    """Serializable debug trajectory (Phase 1 §29 artifact format)."""
    return [dict(timestamp=round(p.timestamp, 3), x=round(p.x, 4),
                 y=round(p.y, 4), scale=round(p.scale, 4),
                 vx=round(p.vx, 4), vy=round(p.vy, 4),
                 track_id=p.track_id, scene_id=p.scene_id,
                 eased=p.eased) for p in points]


def build_keyframes(points: list[tuple[float, float, float, str, int]],
                    run_sizes: list[tuple[float, float, float, float]],
                    full_size: tuple[float, float],
                    transition_ms: int = 650,
                    transition_steps: int = 6,
                    min_scale_change: float = 0.01,
                    jump_threshold: float = 0.15,
                    hard_cut: bool = False,
                    ) -> tuple[list[tuple[float, float, float, float, float, int]], dict]:
    """Assemble render keyframes (t, cx, cy, w, h, scene) from a smoothed
    point sequence.

    - ``points``: (t, x, y, label, scene) sorted by t (deadzoned + limited).
    - ``run_sizes``: (start, end, w, h) size track (e.g. merged runs);
      points outside every run use ``full_size``.
    - Same-scene label change: eased transition over ``transition_ms``
      (positions AND sizes lerped along ease_in_out, ``transition_steps``
      substeps); originals inside the window are superseded. With
      ``hard_cut`` the boundary is a duplicate-time pair instead
      (old value then new value at the same timestamp, exactly like a
      scene change): the renderer holds each side, zero pan frames.
    - Scene change: duplicate-time boundary pair (hard cut, zero-width).
    - Size steps smaller than ``min_scale_change`` apply instantly at the
      boundary instead of lerping (indistinguishable anyway).
    Returns (keyframes, stats) with transitions/jumps/size_steps counts
    and per-transition records for [FRAMING_SWITCH] logging.
    """
    keyframes: list[tuple[float, float, float, float, float, int]] = []
    stats: dict = {"transitions": 0, "jumps": 0, "size_steps": 0,
                   "switches": []}

    def size_at(timestamp: float) -> tuple[float, float]:
        for start, end, w, h in run_sizes:
            if start - 1e-9 <= timestamp <= end + 1e-9:
                return w, h
        return full_size

    ordered = sorted(points, key=lambda p: p[0])
    idx = 0
    prev: tuple | None = None  # (t, x, y, w, h, scene, label)
    n = len(ordered)
    while idx < n:
        timestamp, x, y, label, scene = ordered[idx]
        w, h = size_at(timestamp)
        if prev is None:
            prev = (timestamp, x, y, w, h, scene, label)
            keyframes.append((timestamp, x, y, w, h, scene))
            idx += 1
            continue
        p_t, p_x, p_y, p_w, p_h, p_scene, p_label = prev
        if scene != p_scene:
            # Hard cut: same timestamp, old value then new value.
            keyframes.append((timestamp, p_x, p_y, p_w, p_h, p_scene))
            keyframes.append((timestamp, x, y, w, h, scene))
            prev = (timestamp, x, y, w, h, scene, label)
            idx += 1
            continue
        if label != p_label:
            if hard_cut:
                # Instant cut: duplicate-time boundary pair, old side then
                # new side. Jump detector skips zero-dt pairs by
                # construction, so deliberate cuts never trip it.
                keyframes.append((timestamp, p_x, p_y, p_w, p_h, p_scene))
                keyframes.append((timestamp, x, y, w, h, scene))
                prev = (timestamp, x, y, w, h, scene, label)
                idx += 1
                stats["transitions"] += 1
                stats["switches"].append(dict(time=round(timestamp, 3),
                                              from_label=p_label, to_label=label,
                                              distance=round(((x - p_x) ** 2 + (y - p_y) ** 2) ** 0.5, 4),
                                              transition_ms=0))
                continue
            trans_s = transition_ms / 1000.0
            window_end = timestamp + trans_s
            # Reference curve: upcoming points within the window.
            upcoming = [(t2, x2, y2) for t2, x2, y2, lb2, sc2 in ordered[idx:]
                        if t2 <= window_end + 1e-9 and sc2 == scene]
            size_end = size_at(min(window_end,
                                   upcoming[-1][0] if upcoming else timestamp))
            # Sizes lerp only when the change is visible.
            dw, dh = abs(size_end[0] - p_w), abs(size_end[1] - p_h)
            lerp_size = dw >= min_scale_change or dh >= min_scale_change
            if not lerp_size:
                stats["size_steps"] += 1
            for step in range(1, transition_steps + 1):
                frac = step / transition_steps
                progress = ease_in_out(frac)
                wt = timestamp + trans_s * frac
                # Reference: interpolate upcoming curve at wt.
                ref_x, ref_y = upcoming[-1][1], upcoming[-1][2] if upcoming else (x, y)
                for (t_a, x_a, y_a), (t_b, x_b, y_b) in zip(upcoming, upcoming[1:]):
                    if t_a - 1e-9 <= wt <= t_b + 1e-9 and t_b > t_a:
                        blend = (wt - t_a) / (t_b - t_a)
                        ref_x, ref_y = x_a + (x_b - x_a) * blend, y_a + (y_b - y_a) * blend
                        break
                ex = p_x + (ref_x - p_x) * progress
                ey = p_y + (ref_y - p_y) * progress
                if lerp_size:
                    ew = p_w + (size_end[0] - p_w) * progress
                    eh = p_h + (size_end[1] - p_h) * progress
                else:
                    ew, eh = (size_end if frac >= 1.0 - 1e-9 else (p_w, p_h))
                keyframes.append((round(wt, 3), ex, ey, ew, eh, scene))
            stats["transitions"] += 1
            stats["switches"].append(dict(time=round(timestamp, 3),
                                          from_label=p_label, to_label=label,
                                          distance=round(((x - p_x) ** 2 + (y - p_y) ** 2) ** 0.5, 4),
                                          transition_ms=transition_ms))
            # Consume originals inside the window (superseded by the ease).
            while idx < n and ordered[idx][0] <= window_end + 1e-9 and ordered[idx][4] == scene:
                idx += 1
            # Continue from the eased endpoint.
            last = keyframes[-1]
            prev = (last[0], last[1], last[2], last[3], last[4], last[5], label)
            continue
        keyframes.append((timestamp, x, y, w, h, scene))
        prev = (timestamp, x, y, w, h, scene, label)
        idx += 1
    # Jump detection over same-scene consecutive pairs (zero-dt scene
    # duplicates excluded: hard cuts are legal by construction).
    max_velocity = 0.0
    max_acceleration = 0.0
    prev_vx = prev_vy = 0.0
    prev_t: float | None = None
    for first, second in zip(keyframes, keyframes[1:]):
        if first[5] != second[5] or second[0] - first[0] <= 1e-9:
            prev_t = None
            prev_vx = prev_vy = 0.0
            continue
        dt = second[0] - first[0]
        vx = (second[1] - first[1]) / dt
        vy = (second[2] - first[2]) / dt
        speed = (vx * vx + vy * vy) ** 0.5
        max_velocity = max(max_velocity, speed)
        if prev_t is not None:
            pdt = first[0] - prev_t
            if pdt > 1e-9:
                ax = (vx - prev_vx) / pdt
                ay = (vy - prev_vy) / pdt
                max_acceleration = max(max_acceleration,
                                       (ax * ax + ay * ay) ** 0.5)
        prev_t, prev_vx, prev_vy = first[0], vx, vy
        dist = ((second[1] - first[1]) ** 2 + (second[2] - first[2]) ** 2) ** 0.5
        if dist > jump_threshold:
            stats["jumps"] += 1
    stats["max_velocity"] = round(max_velocity, 4)
    stats["max_acceleration"] = round(max_acceleration, 4)
    return keyframes, stats


def apply_deadzone(seq: list[tuple[float, float, float]],
                   labels: list[str],
                   scenes: list[int],
                   deadzone_x: float = 0.04,
                   deadzone_y: float = 0.06,
                   exit_ease_s: float = 0.0,
                   ) -> tuple[list[tuple[float, float, float]], int]:
    """Anchor gate: hold the anchor until the target leaves the deadzone.

    Per contiguous (label, scene) run. Micro head-wander never moves the
    virtual camera; genuine relocations pass through untouched. Returns
    (gated sequence, held_count).

    ``exit_ease_s``: when the target leaves the zone, converge from the
    frozen anchor toward the live target over this many seconds
    (ease-in-out) instead of snapping. 0.0 = legacy snap behavior,
    kept as the default so existing callers are bit-identical.
    """
    gated: list[tuple[float, float, float]] = []
    held = 0
    anchor: tuple[float, float] | None = None
    anchor_key: tuple[str, int] | None = None
    ease_from: tuple[float, float] | None = None
    ease_t0: float = 0.0
    for (timestamp, x, y), label, scene in zip(seq, labels, scenes):
        key = (label, scene)
        if key != anchor_key:
            anchor = (x, y)
            anchor_key = key
            ease_from = None
            gated.append((timestamp, x, y))
            continue
        assert anchor is not None
        if abs(x - anchor[0]) < deadzone_x and abs(y - anchor[1]) < deadzone_y:
            ease_from = None  # back inside: cancel ease, hold steady
            gated.append((timestamp, anchor[0], anchor[1]))
            held += 1
            continue
        if exit_ease_s <= 0:
            anchor = (x, y)
            gated.append((timestamp, x, y))
            continue
        if ease_from is None:
            ease_from = anchor
            ease_t0 = timestamp
        progress = min(1.0, (timestamp - ease_t0) / exit_ease_s)
        blend = ease_in_out(progress)
        ox = ease_from[0] + (x - ease_from[0]) * blend
        oy = ease_from[1] + (y - ease_from[1]) * blend
        gated.append((timestamp, ox, oy))
        if progress >= 1.0:
            anchor = (x, y)
            ease_from = None
    return gated, held


def limit_motion(seq: list[tuple[float, float, float]],
                 labels: list[str],
                 scenes: list[int],
                 max_velocity: float = 0.6,
                 max_acceleration: float = 3.0,
                 ) -> tuple[list[tuple[float, float, float]], int]:
    """Slew + acceleration clamps per contiguous run (units/sec).

    Safety rails against teleports/glitches only; deliberate eased
    transitions are inserted downstream and stay exempt. Returns
    (limited sequence, trip_count).
    """
    limited: list[tuple[float, float, float]] = []
    trips = 0
    prev: tuple[float, float, float] | None = None
    prev_vx = prev_vy = 0.0
    prev_key: tuple[str, int] | None = None
    for (timestamp, x, y), label, scene in zip(seq, labels, scenes):
        key = (label, scene)
        if prev is None or key != prev_key:
            limited.append((timestamp, x, y))
            prev = (timestamp, x, y)
            prev_vx = prev_vy = 0.0
            prev_key = key
            continue
        prev_t, prev_x, prev_y = prev
        dt = max(1e-6, timestamp - prev_t)
        # Velocity clamp.
        vx = (x - prev_x) / dt
        vy = (y - prev_y) / dt
        speed = (vx * vx + vy * vy) ** 0.5
        if speed > max_velocity:
            trips += 1
            scale = max_velocity / speed
            vx, vy = vx * scale, vy * scale
            x, y = prev_x + vx * dt, prev_y + vy * dt
        # Acceleration clamp (change in velocity per second).
        ax = (vx - prev_vx) / dt
        ay = (vy - prev_vy) / dt
        accel = (ax * ax + ay * ay) ** 0.5
        if accel > max_acceleration:
            trips += 1
            scale = max_acceleration / accel
            vx = prev_vx + ax * scale * dt
            vy = prev_vy + ay * scale * dt
            x, y = prev_x + vx * dt, prev_y + vy * dt
        limited.append((timestamp, x, y))
        prev = (timestamp, x, y)
        prev_vx, prev_vy = vx, vy
        prev_key = key
    return limited, trips
