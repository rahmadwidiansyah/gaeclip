"""Explicit target state machine (Phase 2 decision layer).

Answers "WHO to follow" per sample. The evidence-level timeline
(``visual.build_speaker_timeline``) already sustains candidates through
hold/cooldown; this layer adds what the timeline cannot see:

- PRESENCE: is the decided speaker actually visible *now*?
- OCCLUDED: bounded hold of the last target inside a grace period.
- NO_TARGET: explicit fallback (best stable face, else static center) so a
  lost target never becomes an indefinite ghost-hold.
- SCENE: a cut resets evaluation; epoch-scoped keys make old identities
  unmatchable by construction.
- MARGIN: every switch is re-checked (candidate >= current + margin) and
  the numbers are logged even though the timeline already enforced them.

Identity is ALWAYS an ``(epoch, speaker_id)`` tuple — never a bare id —
so a reused track number after a scene cut is a different target.

This module is pure (no I/O, no logging side effects): it returns events
and the caller formats ``[TARGET_*]`` log lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NO_TARGET = "NO_TARGET"
TRACKING = "TRACKING"
CANDIDATE = "CANDIDATE"
SWITCHING = "SWITCHING"
OCCLUDED = "OCCLUDED"

STATIC_CENTER = (0.5, 0.5)


@dataclass
class ManagerEvent:
    time: float
    kind: str  # ACQUIRE|CANDIDATE|MARGIN_HOLD|SWITCH|CANCEL|LOST|RESTORE|GRACE_EXPIRED|FALLBACK|SCENE_RESET|LOCK|ASSOCIATED
    detail: dict = field(default_factory=dict)


@dataclass
class TargetDecision:
    state: str
    kind: str  # track|fallback|static
    key: tuple[int, str] | None
    center: tuple[float, float]
    reason: str


class TargetManager:
    """Decision-level target tracker over sampled timeline output.

    Margin/hold/cooldown are enforced by the timeline on live per-window
    scores; this layer trusts timeline switches (re-checking them against
    stale segment aggregates caused false blocks) and owns what the
    timeline cannot see: presence, occlusion grace, fallback, scene
    epoch identity. Margins are still recorded on every event for audit.
    """

    def __init__(self, switch_margin: float = 0.10,
                 lost_grace_s: float = 0.7,
                 transition_lock_ms: int = 650,
                 force_margin: float = 0.40,
                 assoc_max_gap_ms: int = 2000,
                 assoc_max_dist: float = 0.25,
                 assoc_max_size_ratio: float = 2.0,
                 assoc_spatial_weight: float = 0.7):
        self.switch_margin = switch_margin
        self.lost_grace_s = lost_grace_s
        self.transition_lock_ms = transition_lock_ms
        self.force_margin = force_margin
        self.assoc_max_gap_ms = assoc_max_gap_ms
        self.assoc_max_dist = assoc_max_dist
        self.assoc_max_size_ratio = assoc_max_size_ratio
        self.assoc_spatial_weight = assoc_spatial_weight
        self.reset()

    def reset(self) -> None:
        self.state = NO_TARGET
        self.current: tuple[int, str] | None = None
        self.current_kind = "static"
        self.current_conf = 0.0
        self.candidate: tuple[int, str] | None = None
        self.occluded_since: float | None = None
        self.occluded_center: tuple[float, float] = STATIC_CENTER
        self.last_live_center: tuple[float, float] = STATIC_CENTER
        self.occluded_conf = 0.0
        self.last_conf: dict[tuple[int, str], float] = {}
        self.last_switch_time = float("-inf")
        self.scene_id: int | None = None
        self.lock_until = float("-inf")
        self.known_ids: set[tuple[int, str]] = set()
        self.tracked_ids: set[tuple[int, str]] = set()
        self.aliases: dict[tuple[int, str], tuple[int, str]] = {}
        self.lost_registry: list[dict] = []

    # -- helpers ------------------------------------------------------
    def _remember_lost(self, key, present, timestamp):
        """Snapshot a disappearing target for lightweight association."""
        entry = present.get(key)
        center = self.last_live_center
        size = 0.0
        if entry is not None:
            center = entry[0]
            size = entry[2] if len(entry) > 2 else 0.0
        self.lost_registry.append({
            "key": key, "center": center, "size": size, "time": timestamp,
        })

    def _try_associate(self, timestamp, seg_key, present, events):
        """Adopt a lost label when a new track matches it spatially.

        Only truly-fresh identities are eligible: ids that were previously
        TRACKED as someone else are never relabeled (that would glue two
        different people together). Returns (key_to_use, associated).
        """
        from .speaker_score import associate_track

        if seg_key in self.tracked_ids:
            return seg_key, False
        entry = present.get(seg_key)
        if entry is None:
            return seg_key, False
        center = entry[0]
        size = entry[2] if len(entry) > 2 else 0.0
        candidates = []
        for lost in self.lost_registry:
            if lost["key"] == seg_key:
                continue
            candidates.append({"id": lost["key"], "center": lost["center"],
                               "size": lost["size"]})
        if not candidates:
            return seg_key, False
        gap_ms = max(0.0, (timestamp - max(
            (e["time"] for e in self.lost_registry), default=timestamp)) * 1000.0)
        match = associate_track(
            center, size, gap_ms, candidates,
            spatial_weight=self.assoc_spatial_weight,
            max_dist=self.assoc_max_dist,
            max_size_ratio=self.assoc_max_size_ratio,
            max_gap_ms=self.assoc_max_gap_ms)
        if match is None:
            return seg_key, False
        self.aliases[seg_key] = (match, timestamp)
        events.append(ManagerEvent(timestamp, "ASSOCIATED",
                                   {"from": match, "to": seg_key}))
        return match, True

    def _fallback(self, present: dict) -> tuple[tuple[int, str] | None,
                                                tuple[float, float]]:
        """Best stable visible face: oldest track wins; ties break to the
        lowest (epoch, id) so the choice is deterministic."""
        if not present:
            return None, STATIC_CENTER
        key = min(sorted(present), key=lambda k: (-present[k][1], k))
        return key, present[key][0]

    # -- main step ----------------------------------------------------
    def step(self, timestamp: float,
             seg_key: tuple[int, str] | None,
             seg_conf: float,
             present: dict[tuple[int, str], tuple[tuple[float, float], int]],
             scene_id: int = 0,
             ) -> tuple[TargetDecision, list[ManagerEvent]]:
        """Advance one sample. ``present`` maps key -> (center, age)."""
        events: list[ManagerEvent] = []
        if self.scene_id is None:
            self.scene_id = scene_id
        if scene_id != self.scene_id:
            self.scene_id = scene_id
            self.reset()
            self.scene_id = scene_id
            events.append(ManagerEvent(timestamp, "SCENE_RESET",
                                       {"scene": scene_id}))
        if seg_key is not None:
            self.last_conf[seg_key] = seg_conf
            self.known_ids.add(seg_key)
            # Alias resolution: a re-acquired track keeps the adopted label.
            # aliases maps new_key -> (old_key, adopted_at).
            if seg_key in self.aliases:
                seg_key = self.aliases[seg_key][0]
        # Prune the lost registry + aliases by TTL (bounded memory).
        if self.lost_registry or self.aliases:
            cutoff = timestamp - self.assoc_max_gap_ms / 1000.0
            self.lost_registry = [e for e in self.lost_registry if e["time"] >= cutoff]
            live_old = {e["key"] for e in self.lost_registry}
            self.aliases = {k: v for k, v in self.aliases.items()
                            if v[0] in live_old and v[1] >= cutoff}

        if self.state == NO_TARGET:
            decision = self._acquire(timestamp, seg_key, seg_conf, present, events)
        elif self.state == TRACKING:
            decision = self._track(timestamp, seg_key, seg_conf, present, events)
        elif self.state == CANDIDATE:
            decision = self._candidate(timestamp, seg_key, seg_conf, present, events)
        elif self.state == OCCLUDED:
            decision = self._occluded(timestamp, seg_key, seg_conf, present, events)
        else:  # SWITCHING is transient; never persists across steps.
            self.state = TRACKING
            decision = self._track(timestamp, seg_key, seg_conf, present, events)
        return decision, events

    def _acquire(self, timestamp, seg_key, seg_conf, present, events):
        if seg_key is not None and seg_key in present:
            adopted, _ = self._try_associate(timestamp, seg_key, present, events)
            return self._become(timestamp, adopted, "timeline", present, events,
                                acquire=True, center=present[seg_key][0])
        key, center = self._fallback(present)
        if key is not None:
            self.state = TRACKING
            self.current = key
            self.tracked_ids.add(key)
            self.current_kind = "fallback"
            self.current_conf = 0.0
            self.last_live_center = center
            events.append(ManagerEvent(timestamp, "FALLBACK",
                                       {"to": key, "reason": "best_visible_face"}))
            return TargetDecision(TRACKING, "fallback", key, center, "best_visible_face")
        return TargetDecision(NO_TARGET, "static", None, STATIC_CENTER, "no_face")

    def _become(self, timestamp, key, reason, present, events, acquire=False,
                center=None):
        self.state = TRACKING
        self.current = key
        self.tracked_ids.add(key)
        self.current_kind = "track"
        self.current_conf = self.last_conf.get(key, 0.0)
        self.candidate = None
        self.occluded_since = None
        if center is None:
            center = present[key][0] if key in present else STATIC_CENTER
        self.last_live_center = center
        events.append(ManagerEvent(
            timestamp, "ACQUIRE" if acquire else "RESTORE",
            {"to": key, "reason": reason}))
        return TargetDecision(TRACKING, "track", key, center, reason)

    def _track(self, timestamp, seg_key, seg_conf, present, events):
        current = self.current
        assert current is not None
        if current in present and (seg_key is None or seg_key == current):
            center = present[current][0]
            self.last_live_center = center
            return TargetDecision(TRACKING, self.current_kind, current, center, "hold")
        if current in present and seg_key is not None and seg_key != current:
            margin = seg_conf - self.current_conf
            if seg_key in present:
                # Transition lock: during the lock window after a switch,
                # only a force-margin candidate may take over (prevents
                # ping-pong while the camera is still travelling).
                if (timestamp < self.lock_until
                        and margin < self.force_margin):
                    events.append(ManagerEvent(timestamp, "MARGIN_HOLD",
                                               {"current": current, "candidate": seg_key,
                                                "current_score": round(self.current_conf, 3),
                                                "candidate_score": round(seg_conf, 3),
                                                "margin": round(margin, 3),
                                                "locked": True}))
                    self.last_live_center = present[current][0]
                    return TargetDecision(TRACKING, self.current_kind, current,
                                          present[current][0], "transition_locked")
                self.state = CANDIDATE
                self.candidate = seg_key
                self.last_live_center = present[current][0]
                events.append(ManagerEvent(timestamp, "CANDIDATE",
                                           {"from": current, "to": seg_key,
                                            "current_score": round(self.current_conf, 3),
                                            "candidate_score": round(seg_conf, 3),
                                            "margin": round(margin, 3)}))
                return TargetDecision(CANDIDATE, self.current_kind, current,
                                      present[current][0], "candidate_building")
            events.append(ManagerEvent(timestamp, "MARGIN_HOLD",
                                       {"current": current, "candidate": seg_key,
                                        "current_score": round(self.current_conf, 3),
                                        "candidate_score": round(seg_conf, 3),
                                        "margin": round(margin, 3)}))
            self.last_live_center = present[current][0]
            return TargetDecision(TRACKING, self.current_kind, current,
                                  present[current][0], "margin_hold")
        # Current target not visible -> bounded occlusion hold, unless the
        # timeline-confident successor is already present (prompt handover;
        # presence + timeline vetting suffice, no stale-aggregate margin).
        if seg_key is not None and seg_key in present:
            previous, previous_conf = current, self.current_conf
            self.state = TRACKING
            self.current = seg_key
            self.tracked_ids.add(seg_key)
            self.current_kind = "track"
            self.current_conf = seg_conf
            self.last_live_center = present[seg_key][0]
            self.last_switch_time = timestamp
            self.lock_until = timestamp + self.transition_lock_ms / 1000.0
            self.occluded_since = None
            events.append(ManagerEvent(timestamp, "LOCK",
                                       {"track": seg_key,
                                        "until": round(self.lock_until, 3)}))
            events.append(ManagerEvent(timestamp, "SWITCH",
                                       {"from": previous, "to": seg_key,
                                        "from_score": round(previous_conf, 3),
                                        "to_score": round(seg_conf, 3),
                                        "reason": "handover_incumbent_gone"}))
            return TargetDecision(SWITCHING, "track", seg_key,
                                  present[seg_key][0], "handover_incumbent_gone")
        self.state = OCCLUDED
        self.occluded_since = timestamp
        # Snapshot the last LIVE position (the track is already absent from
        # presence at this exact step; reading presence here would freeze a
        # stale default instead of the true last framing).
        self.occluded_center = self.last_live_center
        self.occluded_conf = self.current_conf
        self._remember_lost(current, present, timestamp)
        events.append(ManagerEvent(timestamp, "LOST",
                                   {"track": current, "state": OCCLUDED}))
        return TargetDecision(OCCLUDED, self.current_kind, current,
                              self.occluded_center, "occluded_hold")

    def _candidate(self, timestamp, seg_key, seg_conf, present, events):
        current = self.current
        candidate = self.candidate
        assert current is not None and candidate is not None
        if seg_key != candidate or candidate not in present:
            # Candidate faded or left: back to incumbent if visible.
            self.state = TRACKING
            self.candidate = None
            events.append(ManagerEvent(timestamp, "CANCEL",
                                       {"candidate": candidate}))
            if current in present:
                self.last_live_center = present[current][0]
                return TargetDecision(TRACKING, self.current_kind, current,
                                      present[current][0], "candidate_cancelled")
            return self._track(timestamp, None, 0.0, present, events)
        margin = seg_conf - self.current_conf
        # No margin gate here: the timeline sustained this candidate through
        # hold + margin on live scores; re-checking against stale segment
        # aggregates only produces false blocks.
        from_key, from_conf = current, self.last_conf.get(current, 0.0)
        self.state = TRACKING
        self.candidate = None
        self.last_switch_time = timestamp
        self.lock_until = timestamp + self.transition_lock_ms / 1000.0
        self.current = candidate
        self.tracked_ids.add(candidate)
        self.current_kind = "track"
        self.current_conf = seg_conf
        self.last_live_center = present[candidate][0]
        self.occluded_since = None
        events.append(ManagerEvent(timestamp, "LOCK",
                                   {"track": candidate,
                                    "until": round(self.lock_until, 3)}))
        events.append(ManagerEvent(timestamp, "SWITCH",
                                   {"from": from_key, "to": candidate,
                                    "from_score": round(from_conf, 3),
                                    "to_score": round(seg_conf, 3),
                                    "margin": round(margin, 3),
                                    "reason": "speaker_confidence_margin"}))
        return TargetDecision(SWITCHING, "track", candidate,
                              present[candidate][0], "speaker_confidence_margin")

    def _occluded(self, timestamp, seg_key, seg_conf, present, events):
        current = self.current
        assert current is not None
        if current in present:
            self.state = TRACKING
            self.occluded_since = None
            self.last_live_center = present[current][0]
            events.append(ManagerEvent(timestamp, "RESTORE", {"to": current}))
            return TargetDecision(TRACKING, self.current_kind, current,
                                  present[current][0], "restored_in_grace")
        assert self.occluded_since is not None
        if timestamp - self.occluded_since <= self.lost_grace_s:
            return TargetDecision(OCCLUDED, self.current_kind, current,
                                  self.occluded_center, "occluded_hold")
        events.append(ManagerEvent(timestamp, "GRACE_EXPIRED",
                                   {"from": current,
                                    "held_ms": int((timestamp - self.occluded_since) * 1000)}))
        self._remember_lost(current, present, timestamp)
        self.state = NO_TARGET
        self.current = None
        self.candidate = None
        self.occluded_since = None
        return self._acquire(timestamp, seg_key, seg_conf, present, events)
