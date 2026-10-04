"""Active-speaker provider abstraction (TalkNet escalation layer).

Separation of concerns (§40):
  Q1 WHO IS SPEAKING?  -> ActiveSpeakerProvider (heuristic and/or TalkNet)
  Q2 SHOULD CAMERA SWITCH? -> Target Manager (never bypassed)
  Q3 CAN THEY FIT?     -> geometry (never bypassed)
  Q4 HOW SHOULD CAMERA MOVE? -> Kalman/trajectory (never bypassed)

Future SyncNet support: implement ActiveSpeakerProvider (validator /
tie-breaker role) without touching this interface (see SYNCNET_FUTURE).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# Confidence levels (§14). TalkNet score alone is never switch permission:
# AMBIGUOUS margins hold the incumbent via the existing Target Manager.
HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"
AMBIGUOUS = "AMBIGUOUS"
UNAVAILABLE = "UNAVAILABLE"


def confidence_level(score: float, margin: float) -> str:
    """HIGH/MEDIUM/LOW/AMBIGUOUS readiness label.

    Thin margins (<0.05) are AMBIGUOUS no matter how high the absolute
    score: A=0.89 vs B=0.86 must not elect on 0.03.
    """
    if margin < 0.05:
        return AMBIGUOUS
    if score >= 0.6 and margin >= 0.15:
        return HIGH
    if score >= 0.4:
        return MEDIUM
    if score >= 0.25:
        return LOW
    return AMBIGUOUS


@dataclass(frozen=True)
class SpeakerScore:
    """One provider's verdict for one tracked face in one window."""

    track_id: str
    track_epoch: int
    timestamp: float  # window center, source timestamps (no CFR games)
    window_start: float
    window_end: float
    score: float  # 0-1 speaking probability
    confidence: str = MEDIUM
    provider: str = "heuristic"  # heuristic | talknet | (future) syncnet
    device: str = "cpu"
    insufficient_data: bool = False


@dataclass
class TalkNetResult:
    """Outcome of scoring a batch of faces for one temporal window."""

    scores: list[SpeakerScore] = field(default_factory=list)
    device: str = "cpu"
    inference_ms: float = 0.0
    preprocess_ms: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0
    batch_size: int = 0
    faces_requested: int = 0
    faces_scored: int = 0
    escalation_reason: str = ""


class ActiveSpeakerProvider(ABC):
    """Q1-only interface: who is speaking, per tracked face per window."""

    name: str = "base"

    @abstractmethod
    def score(self, tracks: list[Any], timestamp: float,
              context: dict[str, Any]) -> list[SpeakerScore]:
        """Return one SpeakerScore per scoreable track (may be empty)."""


class HeuristicSpeakerProvider(ActiveSpeakerProvider):
    """Adapter over the existing lightweight heuristic (mouth/VAD/
    continuity/quality). No model, no device, never fails."""

    name = "heuristic"

    def score(self, tracks: list[Any], timestamp: float,
              context: dict[str, Any]) -> list[SpeakerScore]:
        heuristic = context.get("heuristic_scores") or {}
        out: list[SpeakerScore] = []
        for track in tracks:
            tid = getattr(track, "speaker_id", str(track))
            epoch = getattr(track, "epoch", 0)
            value = heuristic.get(tid)
            if value is None:
                continue
            try:
                score = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                continue
            out.append(SpeakerScore(
                track_id=tid, track_epoch=epoch, timestamp=timestamp,
                window_start=timestamp, window_end=timestamp,
                score=score, confidence=MEDIUM, provider="heuristic",
            ))
        return out


# SYNCNET_FUTURE: SyncNet fits here as an audio-video synchronization
# validator / ambiguous-speaker tie-breaker:
#
#   class SyncNetSpeakerProvider(ActiveSpeakerProvider):
#       name = "syncnet"
#       def score(self, tracks, timestamp, context): ...
#
# consumes the same (tracks, context) inputs, returns SpeakerScore with
# provider="syncnet", and plugs into fuse_scores() with its own weight.
# No SyncNet dependency, model, or inference exists in this phase.
