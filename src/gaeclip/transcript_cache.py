"""Persistent Whisper transcript cache (Redis index + file artifact).

One shared transcript per (source, transcription config): the first job that
transcribes a YouTube video stores its timestamped transcript, later jobs
with a compatible config reuse it instead of running Whisper again.

Persistence (existing infrastructure only, no new database):

- Redis: record ``gaeclip:transcript:<cache_id>`` (JSON), lookup index
  ``gaeclip:transcript:index:<source_key>:<config_hash>`` -> cache_id,
  id set ``gaeclip:transcript:ids``, lock
  ``gaeclip:transcript:lock:<source_key>:<config_hash>``.
- Filesystem: ``<STORAGE_ROOT>/transcripts/<cache_id>.json`` — deliberately
  OUTSIDE ``jobs/<job_id>/`` so deleting a job never deletes shared cache.

Status mapping: only SUCCESS records are indexed. PROCESSING is represented
by the lock key (with expiry so a crashed worker cannot deadlock waiters);
FAILED transcriptions are never stored; INVALID means the index pointed at
a missing/corrupt/incompatible artifact (index entry removed, transcribe
fresh).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import CancelledError
from .schemas import TranscriptLine
from .url import extract_video_id

logger = logging.getLogger(__name__)

TRANSCRIPT_SCHEMA_VERSION = 1

# Lock TTL: a crashed worker must never block waiters forever.
TRANSCRIPT_LOCK_TTL_SECONDS = 3600
# How long a waiter polls for another job's in-flight transcription.
TRANSCRIPT_WAIT_TIMEOUT_SECONDS = 1800
TRANSCRIPT_WAIT_POLL_SECONDS = 5
# Duration drift tolerance when verifying a cached transcript against the
# current source metadata (video may have been re-edited/replaced).
DURATION_TOLERANCE_SECONDS = 10.0


def canonical_youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def source_key_for(video_id: str | None, canonical_url: str) -> tuple[str, str | None]:
    """(source_key, source_type). YouTube video ID is the primary identity so
    watch/youtu.be/shorts/embed URLs for one video share one cache entry."""
    if video_id:
        return f"youtube:{video_id}", "youtube"
    digest = hashlib.sha256(canonical_url.encode()).hexdigest()[:32]
    return f"url:{digest}", "url"


def transcription_config_hash(whisper_model: str, language: str) -> str:
    """Hash of transcription-affecting settings ONLY (model, language, fixed
    task/VAD). Job-level settings (clips, styles, Gemini) never enter it, so
    a recreate with different curation config still hits the cache."""
    canonical = json.dumps(
        {
            "engine": "faster_whisper",
            "task": "transcribe",
            "vad_filter": True,
            "language": (language or "").strip().lower(),
            "whisper_model": (whisper_model or "").strip().lower(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


@dataclass
class TranscriptCacheRecord:
    cache_id: str
    source_key: str
    source_type: str
    youtube_video_id: str | None
    canonical_url: str
    duration: float | None
    language: str
    whisper_model: str
    config_hash: str
    transcript_path: str  # relative to STORAGE_ROOT, e.g. transcripts/<id>.json
    transcript_hash: str
    line_count: int
    char_count: int
    status: str = "success"
    schema_version: int = TRANSCRIPT_SCHEMA_VERSION
    created_at: float = 0
    updated_at: float = 0
    last_used_at: float = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_transcript_payload(payload: Any) -> list[TranscriptLine] | None:
    """Strict validation; returns lines or None (invalid). Never raises."""
    try:
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != TRANSCRIPT_SCHEMA_VERSION:
            return None
        raw_lines = payload.get("lines")
        if not isinstance(raw_lines, list) or not raw_lines:
            return None
        lines = [TranscriptLine.model_validate(item) for item in raw_lines]
        for line in lines:
            if not (line.end > line.start >= 0):
                return None
        return lines
    except Exception:
        return None


class TranscriptCache:
    """Shared transcript store backed by existing Redis + storage volume."""

    def __init__(self, client: Any, storage_root: Path, prefix: str = "gaeclip"):
        self.client = client
        self.storage_root = storage_root
        self.prefix = prefix

    # -- keys ------------------------------------------------------------
    def _record_key(self, cache_id: str) -> str:
        return f"{self.prefix}:transcript:{cache_id}"

    def _index_key(self, source_key: str, config_hash: str) -> str:
        return f"{self.prefix}:transcript:index:{source_key}:{config_hash}"

    def _ids_key(self) -> str:
        return f"{self.prefix}:transcript:ids"

    def _lock_key(self, source_key: str, config_hash: str) -> str:
        return f"{self.prefix}:transcript:lock:{source_key}:{config_hash}"

    # -- lookup ----------------------------------------------------------
    def lookup(
        self,
        source_url: str,
        whisper_model: str,
        language: str,
        duration: float | None = None,
    ) -> tuple[list[TranscriptLine] | None, TranscriptCacheRecord | None, str]:
        """Returns (lines, record, outcome) with outcome in
        hit|miss|invalid. DB errors degrade to miss (cache is an optimization,
        never a single point of failure)."""
        try:
            return self._lookup_inner(source_url, whisper_model, language, duration)
        except Exception as exc:
            logger.warning("[TRANSCRIPT_CACHE] lookup failed, degrading to miss: %s", exc)
            return None, None, "miss"

    def _lookup_inner(
        self,
        source_url: str,
        whisper_model: str,
        language: str,
        duration: float | None,
    ) -> tuple[list[TranscriptLine] | None, TranscriptCacheRecord | None, str]:
        video_id = extract_video_id(source_url)
        canonical = canonical_youtube_url(video_id) if video_id else source_url.strip()
        source_key, _ = source_key_for(video_id, canonical)
        config_hash = transcription_config_hash(whisper_model, language)
        logger.info(
            "[TRANSCRIPT_CACHE] lookup source=%s model=%s language=%s",
            source_key, whisper_model, language,
        )
        raw_id = self.client.get(self._index_key(source_key, config_hash))
        if not raw_id:
            logger.info("[TRANSCRIPT_CACHE] miss source=%s", source_key)
            return None, None, "miss"
        cache_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
        raw_record = self.client.get(self._record_key(cache_id))
        if not raw_record:
            logger.warning("[TRANSCRIPT_CACHE] invalid cache_id=%s (record missing)", cache_id)
            self._drop_index(source_key, config_hash)
            return None, None, "invalid"
        try:
            data = json.loads(raw_record)
            record = TranscriptCacheRecord(**data)
        except (json.JSONDecodeError, TypeError):
            logger.warning("[TRANSCRIPT_CACHE] invalid cache_id=%s (record corrupt)", cache_id)
            self._drop_index(source_key, config_hash)
            return None, None, "invalid"
        if record.status != "success" or record.schema_version != TRANSCRIPT_SCHEMA_VERSION:
            logger.warning(
                "[TRANSCRIPT_CACHE] invalid cache_id=%s status=%s schema=%s",
                cache_id, record.status, record.schema_version,
            )
            self._drop_index(source_key, config_hash)
            return None, None, "invalid"
        if duration is not None and record.duration is not None:
            try:
                if abs(float(duration) - float(record.duration)) > DURATION_TOLERANCE_SECONDS:
                    logger.warning(
                        "[TRANSCRIPT_CACHE] invalid cache_id=%s duration_mismatch "
                        "cached=%s current=%s",
                        cache_id, record.duration, duration,
                    )
                    self._drop_index(source_key, config_hash)
                    return None, None, "invalid"
            except (TypeError, ValueError):
                pass
        artifact = self.storage_root / record.transcript_path
        try:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[TRANSCRIPT_CACHE] invalid cache_id=%s artifact unreadable: %s", cache_id, exc)
            self._drop_index(source_key, config_hash)
            return None, None, "invalid"
        lines = validate_transcript_payload(payload)
        if not lines:
            logger.warning("[TRANSCRIPT_CACHE] invalid cache_id=%s payload failed validation", cache_id)
            self._drop_index(source_key, config_hash)
            return None, None, "invalid"
        self._touch(record)
        logger.info(
            "[TRANSCRIPT_CACHE] hit source=%s cache_id=%s lines=%d model=%s language=%s",
            source_key, cache_id, len(lines), whisper_model, language,
        )
        return lines, record, "hit"

    def _drop_index(self, source_key: str, config_hash: str) -> None:
        try:
            self.client.delete(self._index_key(source_key, config_hash))
        except Exception:
            pass

    def _touch(self, record: TranscriptCacheRecord) -> None:
        try:
            record.last_used_at = time.time()
            self.client.set(self._record_key(record.cache_id), json.dumps(record.to_dict()))
        except Exception:
            pass

    # -- store -----------------------------------------------------------
    def store(
        self,
        source_url: str,
        whisper_model: str,
        language: str,
        lines: list[TranscriptLine],
        duration: float | None = None,
    ) -> TranscriptCacheRecord | None:
        """Persist a successful transcription. Store failures never raise
        (must not fail the job that just transcribed successfully)."""
        try:
            return self._store_inner(source_url, whisper_model, language, lines, duration)
        except Exception as exc:
            logger.warning("[TRANSCRIPT_CACHE] store failed (job unaffected): %s", exc)
            return None

    def _store_inner(
        self,
        source_url: str,
        whisper_model: str,
        language: str,
        lines: list[TranscriptLine],
        duration: float | None,
    ) -> TranscriptCacheRecord:
        import secrets

        if not lines:
            raise ValueError("refusing to cache an empty transcript")
        video_id = extract_video_id(source_url)
        canonical = canonical_youtube_url(video_id) if video_id else source_url.strip()
        source_key, source_type = source_key_for(video_id, canonical)
        config_hash = transcription_config_hash(whisper_model, language)
        cache_id = f"tc_{secrets.token_urlsafe(12)}"
        payload = {
            "schema_version": TRANSCRIPT_SCHEMA_VERSION,
            "cache_id": cache_id,
            "source_key": source_key,
            "youtube_video_id": video_id,
            "canonical_url": canonical,
            "language": language,
            "whisper_model": whisper_model,
            "config_hash": config_hash,
            "created_at": time.time(),
            "line_count": len(lines),
            "lines": [line.model_dump(mode="json") for line in lines],
        }
        serialized = json.dumps(payload, ensure_ascii=False)
        rel_path = f"transcripts/{cache_id}.json"
        dest = self.storage_root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(serialized, encoding="utf-8")
        try:
            dest.chmod(0o600)
        except OSError:
            pass
        now = time.time()
        record = TranscriptCacheRecord(
            cache_id=cache_id,
            source_key=source_key,
            source_type=source_type,
            youtube_video_id=video_id,
            canonical_url=canonical,
            duration=float(duration) if duration is not None else None,
            language=language,
            whisper_model=whisper_model,
            config_hash=config_hash,
            transcript_path=rel_path,
            transcript_hash=hashlib.sha256(serialized.encode()).hexdigest(),
            line_count=len(lines),
            char_count=sum(len(line.text) for line in lines),
            created_at=now,
            updated_at=now,
            last_used_at=now,
        )
        self.client.set(self._record_key(cache_id), json.dumps(record.to_dict()))
        self.client.set(self._index_key(source_key, config_hash), cache_id)
        try:
            self.client.sadd(self._ids_key(), cache_id)
        except (AttributeError, TypeError):
            pass
        logger.info(
            "[TRANSCRIPT_CACHE] store cache_id=%s source=%s lines=%d model=%s language=%s",
            cache_id, source_key, len(lines), whisper_model, language,
        )
        return record

    # -- adopt historical job transcripts ----------------------------------
    def adopt_job_transcript(
        self,
        transcript_path: Path,
        source_url: str,
        whisper_model: str,
        language: str,
        duration: float | None = None,
    ) -> tuple[list[TranscriptLine] | None, TranscriptCacheRecord | None]:
        """Register a pre-cache job's transcript.json into the shared cache.
        Returns (lines, record) or (None, None) when unusable."""
        try:
            payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None
        # Historical files may wrap lines under {"lines": [...]} or be a bare
        # list; normalize before validation.
        if isinstance(payload, list):
            payload = {"schema_version": TRANSCRIPT_SCHEMA_VERSION, "lines": payload}
        lines = validate_transcript_payload(payload)
        if not lines:
            return None, None
        record = self.store(source_url, whisper_model, language, lines, duration)
        if record is None:
            return None, None
        logger.info(
            "[TRANSCRIPT_CACHE] adopted job transcript into cache_id=%s source=%s",
            record.cache_id, record.source_key,
        )
        return lines, record

    # -- distributed lock (deduplicate concurrent transcriptions) -----------
    def acquire_lock(self, source_key: str, config_hash: str, owner: str) -> bool:
        try:
            acquired = self.client.set(
                self._lock_key(source_key, config_hash), owner,
                nx=True, ex=TRANSCRIPT_LOCK_TTL_SECONDS,
            )
            return bool(acquired)
        except TypeError:
            # Minimal Redis shims without nx/ex support.
            try:
                if self.client.get(self._lock_key(source_key, config_hash)):
                    return False
                self.client.set(self._lock_key(source_key, config_hash), owner)
                return True
            except Exception:
                return False
        except Exception:
            return False

    def release_lock(self, source_key: str, config_hash: str, owner: str) -> None:
        try:
            raw = self.client.get(self._lock_key(source_key, config_hash))
            current = raw.decode() if isinstance(raw, bytes) else raw
            if current == owner:
                self.client.delete(self._lock_key(source_key, config_hash))
        except Exception:
            pass

    def wait_for_transcript(
        self,
        source_url: str,
        whisper_model: str,
        language: str,
        duration: float | None = None,
        timeout_seconds: float = TRANSCRIPT_WAIT_TIMEOUT_SECONDS,
        progress=None,
        should_cancel=None,
    ) -> tuple[list[TranscriptLine] | None, TranscriptCacheRecord | None]:
        """Poll for another job's in-flight transcription. Returns (lines,
        record) on hit, (None, None) on timeout/cancel so the caller falls
        back to transcribing itself. Never raises except CancelledError."""
        deadline = time.time() + max(1.0, timeout_seconds)
        while time.time() < deadline:
            if should_cancel is not None:
                try:
                    if should_cancel():
                        raise CancelledError("Transcription wait cancelled by user")
                except CancelledError:
                    raise
                except Exception:
                    pass
            lines, record, outcome = self.lookup(source_url, whisper_model, language, duration)
            if outcome == "hit" and lines:
                return lines, record
            if progress is not None:
                try:
                    progress("transcribing", 36, "Menunggu transkripsi job lain selesai…", 0, 0)  # type: ignore[call-arg]
                except TypeError:
                    try:
                        progress("transcribing", 36)
                    except Exception:
                        pass
                except Exception:
                    pass
            time.sleep(TRANSCRIPT_WAIT_POLL_SECONDS)
        return None, None

    # -- stats -------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """Best-effort cache statistics for ops visibility."""
        try:
            raw_ids = self.client.smembers(self._ids_key())
            ids = [
                item.decode() if isinstance(item, bytes) else str(item)
                for item in (raw_ids or [])
            ]
        except (AttributeError, TypeError):
            ids = []
        success = 0
        lines_total = 0
        for cache_id in ids:
            try:
                raw = self.client.get(self._record_key(cache_id))
                if not raw:
                    continue
                data = json.loads(raw)
                if data.get("status") == "success":
                    success += 1
                    lines_total += int(data.get("line_count") or 0)
            except Exception:
                continue
        storage_bytes = 0
        transcripts_dir = self.storage_root / "transcripts"
        try:
            if transcripts_dir.is_dir():
                for child in transcripts_dir.glob("*.json"):
                    try:
                        storage_bytes += child.stat().st_size
                    except OSError:
                        continue
        except OSError:
            pass
        return {
            "cached_transcripts": len(ids),
            "successful": success,
            "total_lines": lines_total,
            "storage_bytes": storage_bytes,
            "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        }
