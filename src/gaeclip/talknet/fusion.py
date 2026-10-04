"""Escalation decision + score fusion (§10-13).

Heuristic runs FIRST on every window (cheap). TalkNet is the escalation
layer for hard cases only. Fusion never replaces the heuristic blindly:
weights renormalize over AVAILABLE terms, NaN-safe, and without TalkNet
the result is exactly the heuristic score (zero behavior change).

Weight mapping vs the §13 formula: continuity, face-quality and onset
already live structurally inside the heuristic path (continuity fusion
weight, quality modifier, timeline onset latch), so re-adding them here
would double-count. obs-level fusion therefore blends talknet with the
heuristic verdict; the timeline still applies onset/ambiguity/hysteresis
on top. Documented, configurable, testable.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def fuse_scores(talknet: float | None,
                heuristic: float,
                continuity: float | None = None,
                quality: float | None = None,
                onset: float | None = None,
                weight_talknet: float = 0.55,
                weight_heuristic: float = 0.25,
                weight_continuity: float = 0.10,
                weight_face_quality: float = 0.05,
                weight_onset: float = 0.10) -> float:
    """Normalized blend in 0-1. TalkNet absent -> heuristic exactly."""
    def clean(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number:
            return None
        return max(0.0, min(1.0, number))

    heuristic = clean(heuristic)
    if heuristic is None:
        heuristic = 0.0
    talknet = clean(talknet)
    if talknet is None:
        # No TalkNet verdict: heuristic EXACTLY (continuity/quality live
        # inside it structurally; re-adding them here would double-count).
        return heuristic
    terms = [(weight_heuristic, heuristic), (weight_talknet, talknet)]
    for weight, value in ((weight_continuity, continuity),
                          (weight_face_quality, quality),
                          (weight_onset, onset)):
        cleaned = clean(value)
        if cleaned is not None and weight > 0:
            terms.append((weight, cleaned))
    total = sum(weight for weight, _ in terms)
    if total <= 0:
        return heuristic
    return max(0.0, min(1.0, sum(w * v for w, v in terms) / total))


def escalation_triggers() -> tuple[str, ...]:
    return ("crowded", "ambiguous_margin", "low_confidence",
            "multi_mouth", "overlap", "onset", "target_lost",
            "acquire_failed")


def decide_escalation(n_faces: int,
                      best: float,
                      second: float,
                      strong_mouth_count: int = 0,
                      overlap: bool = False,
                      new_onset: bool = False,
                      target_lost: bool = False,
                      acquire_failed: bool = False,
                      trigger_margin: float = 0.12,
                      trigger_confidence: float = 0.60,
                      min_people: int = 4) -> tuple[bool, str]:
    """Heuristic-first gate (§10-11). Returns (escalate, reason).

    Escalate when crowded WITHOUT a clear leader, when the margin is
    thin, when confidence is low, when several mouths move at once, on
    overlap, onset, loss, or failed acquisition. Confident + uncrowded
    windows never pay TalkNet cost.
    """
    margin = best - second
    if n_faces >= min_people and (margin < trigger_margin or best < 0.75):
        return True, "crowded"
    if n_faces >= 2 and margin < trigger_margin:
        return True, "ambiguous_margin"
    if n_faces >= 2 and best < trigger_confidence:
        return True, "low_confidence"
    if strong_mouth_count >= 2:
        return True, "multi_mouth"
    if overlap:
        return True, "overlap"
    if new_onset:
        return True, "onset"
    if target_lost:
        return True, "target_lost"
    if acquire_failed:
        return True, "acquire_failed"
    return False, ""


def overlapping_speech(scores: dict[str, float],
                       threshold: float = 0.5) -> tuple[bool, list[str]]:
    """OVERLAPPING_SPEECH marker (§17): >=2 faces strongly active.

    Returns (is_overlap, speakers). Never a switch command — the Target
    Manager still decides SINGLE / TWO_SPEAKER / GROUP from geometry.
    """
    loud = sorted(tid for tid, score in scores.items()
                  if score >= threshold)
    return (len(loud) >= 2, loud)
