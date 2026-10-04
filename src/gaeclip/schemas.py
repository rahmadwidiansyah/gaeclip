from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ValidationError


class Word(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def valid_range(self):
        if not math.isfinite(self.start) or not math.isfinite(self.end) or self.start >= self.end:
            raise ValueError("word timestamps must be finite and start before end")
        return self


class TranscriptLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    line_id: str = Field(pattern=r"^l\d{6}$")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1)
    words: list[Word] = Field(default_factory=list)
    language: str = Field(default="id", min_length=2, max_length=16)
    source: Literal["youtube_auto", "faster_whisper", "manual"]
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def valid_range(self):
        if not math.isfinite(self.start) or not math.isfinite(self.end) or self.start >= self.end:
            raise ValueError("line timestamps must be finite and start before end")
        return self


class CandidateClip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    candidate_id: str = Field(pattern=r"^c\d{6}$")
    start_line_id: str = Field(pattern=r"^l\d{6}$")
    end_line_id: str = Field(pattern=r"^l\d{6}$")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=500)
    score: float = Field(ge=0, le=1)
    # Optional Gemini semantic breakdown (curation-v2 discovery). Absent
    # for heatmap/heuristic/deterministic sources; backend computes
    # semantic_score from these when present, else falls back to score.
    hook_strength: float | None = Field(default=None, ge=0, le=1)
    payoff_strength: float | None = Field(default=None, ge=0, le=1)
    context_completeness: float | None = Field(default=None, ge=0, le=1)
    standalone_clarity: float | None = Field(default=None, ge=0, le=1)
    information_density: float | None = Field(default=None, ge=0, le=1)
    novelty: float | None = Field(default=None, ge=0, le=1)
    clipability: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def valid_range(self):
        if not math.isfinite(self.start) or not math.isfinite(self.end) or self.start >= self.end:
            raise ValueError("candidate timestamps must be finite and start before end")
        return self


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    artifact_id: str
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0)
    mime_type: Literal["video/mp4", "text/plain", "application/json"]


def validate_candidates(
    candidates: list[CandidateClip],
    lines: list[TranscriptLine],
    min_duration: float,
    max_duration: float,
    max_count: int,
) -> list[CandidateClip]:
    if len(candidates) > max_count:
        raise ValidationError("Candidate count exceeds configured limit")
    line_ids = {line.line_id for line in lines}
    seen: set[str] = set()
    result: list[CandidateClip] = []
    for candidate in sorted(candidates, key=lambda item: item.start):
        if candidate.candidate_id in seen:
            raise ValidationError(f"Duplicate candidate ID: {candidate.candidate_id}")
        if candidate.start_line_id not in line_ids or candidate.end_line_id not in line_ids:
            raise ValidationError(f"Unknown line ID in {candidate.candidate_id}")
        duration = candidate.end - candidate.start
        if not min_duration <= duration <= max_duration:
            raise ValidationError(f"Invalid duration for {candidate.candidate_id}")
        if result and candidate.start < result[-1].end:
            raise ValidationError("Candidate intervals overlap")
        seen.add(candidate.candidate_id)
        result.append(candidate)
    return result
