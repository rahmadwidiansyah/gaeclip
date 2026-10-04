from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any

from .errors import ConflictError, ValidationError

TERMINAL_STATES = {"completed", "partial", "failed", "cancelled", "expired"}
STATES = {"accepted", "queued", "running", "retrying", *TERMINAL_STATES}


@dataclass
class JobRecord:
    job_id: str
    source_url: str
    language: str
    status: str = "accepted"
    stage: str = "accepted"
    progress: int = 0
    error: dict[str, Any] | None = None
    clips: list[dict[str, Any]] | None = None
    curation: dict[str, Any] | None = None
    fingerprint: str | None = None
    created_at: float = 0
    updated_at: float = 0
    max_clips: int = 3
    min_duration: float = 30
    max_duration: float = 60
    burn_subtitles: bool = True
    subtitle_style: str = "casual_center"
    subtitle_position: str = "bottom_center"
    stage_detail: str | None = None
    current_clip: int = 0
    total_clips: int = 0
    clip_tasks: list[dict[str, Any]] | None = None
    video_id: str | None = None
    video: dict[str, Any] | None = None
    # Recreate lineage: new job created from this parent (None = original).
    recreated_from_job_id: str | None = None
    # Transcript reuse visibility: True when curation ran on a cached/shared
    # transcript instead of a fresh download/transcription in this job.
    transcript_reused: bool = False
    transcript_cache_id: str | None = None
    # One-shot flag set by Recreate-with-retranscribe; worker clears it.
    force_retranscribe: bool = False
    # Celery task UUID (NOT the job id): used to revoke/terminate the
    # exact worker task on cancel. None for records created before this
    # field existed (cancel then falls back to polling only).
    worker_task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("fingerprint", None)
        if self.error and "fingerprint" in self.error:
            result["error"] = None
        return result


class JobRepository:
    """Redis repository with atomic idempotency ownership."""

    def __init__(self, client: Any, prefix: str = "gaeclip"):
        self.client = client
        self.prefix = prefix

    def _key(self, job_id: str) -> str:
        return f"{self.prefix}:job:{job_id}"

    def _idem_key(self, idem: str) -> str:
        return f"{self.prefix}:idempotency:{idem}"

    @staticmethod
    def request_fingerprint(
        source_url: str,
        language: str,
        max_clips: int = 3,
        min_duration: float = 30,
        max_duration: float = 60,
        burn_subtitles: bool = True,
        subtitle_style: str = "casual_center",
        subtitle_position: str = "bottom_center",
        transcript_source: str = "youtube_first",
    ) -> str:
        raw = (
            f"{source_url}\0{language}\0{max_clips}\0{min_duration}\0{max_duration}\0"
            f"{burn_subtitles}\0{subtitle_style}\0{subtitle_position}\0{transcript_source}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def create(self, record: JobRecord, idempotency_key: str | None, fingerprint: str) -> tuple[JobRecord, bool]:
        if idempotency_key:
            key = self._idem_key(idempotency_key)
            existing = self.client.get(key)
            if existing:
                existing_id = existing.decode() if isinstance(existing, bytes) else existing
                existing_record = self.get(existing_id)
                if existing_record is None:
                    raise ValidationError("Idempotency record points to a missing job")
                if existing_record.fingerprint != fingerprint:
                    raise ConflictError("Idempotency-Key was reused for a different request")
                return existing_record, True
            record.fingerprint = fingerprint
            serialized = {**record.to_dict(), "fingerprint": record.fingerprint}
            pipe = self.client.pipeline()
            pipe.set(self._key(record.job_id), json.dumps(serialized))
            pipe.set(self._idem_key(idempotency_key), record.job_id, ex=86400, nx=True)
            results = pipe.execute()
            if not results[-1]:
                return self.create(record, idempotency_key, fingerprint)
            return record, False
        self.client.set(self._key(record.job_id), json.dumps({**record.to_dict(), "fingerprint": record.fingerprint}))
        return record, False

    def get(self, job_id: str) -> JobRecord | None:
        raw = self.client.get(self._key(job_id))
        if not raw:
            return None
        data = json.loads(raw)
        try:
            return JobRecord(**data)
        except TypeError:
            # Tolerate records written by older/newer versions: keep known fields.
            import dataclasses
            known = {field.name for field in dataclasses.fields(JobRecord)}
            return JobRecord(**{k: v for k, v in data.items() if k in known})

    def list(self, limit: int = 50) -> list[JobRecord]:
        records: list[JobRecord] = []
        for key in self.client.scan_iter(match=f"{self.prefix}:job:*", count=100):
            raw = self.client.get(key)
            if not raw:
                continue
            try:
                records.append(JobRecord(**json.loads(raw)))
            except (json.JSONDecodeError, TypeError):
                continue
        records.sort(key=lambda record: record.updated_at or record.created_at, reverse=True)
        return records[:limit]

    def save(self, record: JobRecord) -> JobRecord:
        record.updated_at = time.time()
        self.client.set(self._key(record.job_id), json.dumps({**record.to_dict(), "fingerprint": record.fingerprint}))
        return record

    def transition(self, job_id: str, status: str, stage: str, progress: int) -> JobRecord:
        if status not in STATES:
            raise ValidationError(f"Unknown job status: {status}")
        record = self.get(job_id)
        if record is None:
            raise ValidationError("Job not found")
        if record.status in TERMINAL_STATES:
            return record
        record.status, record.stage, record.progress = status, stage, max(0, min(100, progress))
        return self.save(record)

    def update_progress(
        self,
        job_id: str,
        stage: str,
        progress: int,
        stage_detail: str | None = None,
        current_clip: int | None = None,
        total_clips: int | None = None,
    ) -> JobRecord | None:
        record = self.get(job_id)
        if record is None or record.status in TERMINAL_STATES:
            return record
        record.status = "running"
        record.stage = stage
        record.progress = max(0, min(100, progress))
        if stage_detail is not None:
            record.stage_detail = stage_detail
        if current_clip is not None:
            record.current_clip = current_clip
        if total_clips is not None:
            record.total_clips = total_clips
        return self.save(record)

    def delete(self, job_id: str) -> None:
        self.client.delete(self._key(job_id))
