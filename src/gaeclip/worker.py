from __future__ import annotations

import os
import time
import logging

from .config import Settings
from .jobs import JobRepository
from .pipeline import generate_from_url

logger = logging.getLogger(__name__)


def _clip_entry(clip: Any, task_lookup: dict[str, Any]) -> dict[str, Any]:
    """Serializable clips[] entry incl. pipeline-generated pub metadata.

    Readers must use .get() for the metadata keys: clips predating the
    feature (and render-failed clips) don't carry them.
    """
    task = task_lookup.get(clip.candidate_id)
    return {
        "clip_id": clip.candidate_id,
        "path": str(clip.path),
        "title": clip.title,
        "score": clip.score,
        "reason": clip.reason,
        "start": clip.start,
        "end": clip.end,
        "duration_seconds": task.duration_seconds if task else None,
        "size_bytes": task.size_bytes if task else None,
        "description": getattr(clip, "description", "") or "",
        "hashtags": list(getattr(clip, "hashtags", ()) or []),
        "metadata_source": getattr(clip, "metadata_source", "") or "",
        "metadata_updated_at": getattr(clip, "metadata_updated_at", None),
    }


def _redis():
    # Delegates to the runtime factory. Server (default) returns the exact
    # same redis.Redis client as before; desktop returns the SQLite shim.
    from .runtime.factory import make_client

    return make_client()


def _resolve_ai_chain(client, job_settings, job_id: str) -> list[str] | None:
    """User chain (manual) or policy-ranked chain (automatic) for this job.

    Never breaks the job: on any resolution problem falls back to the
    legacy single GEMINI_MODEL so curation still runs.
    """
    try:
        from .ai_models import cached_snapshot, prefs_from_service, resolve_chain
        from .app_settings import AppSettings

        service = AppSettings(client)
        prefs = prefs_from_service(service)
        snapshot = None
        if job_settings.gemini_api_key:
            snapshot = cached_snapshot(client, job_settings.gemini_api_key)
        primary, fallbacks, method = resolve_chain(prefs, snapshot)
        chain = [primary, *fallbacks]
        logger.info(
            "Job %s AI chain resolved: method=%s primary=%s fallbacks=%d",
            job_id, method, primary, len(fallbacks),
        )
        return chain
    except Exception:
        logger.warning("Job %s AI chain resolution failed; using legacy model", job_id, exc_info=True)
        try:
            return [job_settings.gemini_model]
        except Exception:
            return None


def process_job(job_id: str) -> None:
    from .errors import CancelledError

    repository = JobRepository(_redis())
    record = repository.get(job_id)
    if record is None or record.status == "cancelled":
        return
    repository.update_progress(job_id, "processing", 10, "Worker mulai bekerja", 0, record.total_clips or record.max_clips)
    try:
        def update_progress(
            stage: str,
            progress: int,
            detail: str | None = None,
            current: int = 0,
            total: int = 0,
        ) -> None:
            live = repository.get(job_id)
            if live is not None and live.status == "cancelled":
                raise CancelledError("Job dibatalkan pengguna")
            repository.update_progress(job_id, stage, progress, detail, current, total)

        def save_clip_tasks(tasks) -> None:
            live = repository.get(job_id)
            if live is None or live.status in {"completed", "partial", "failed", "cancelled", "expired"}:
                return
            live.clip_tasks = [task.to_dict() for task in tasks]
            live.current_clip = sum(1 for task in tasks if task.status in {"done", "failed"})
            live.total_clips = max(live.total_clips, len(tasks))
            repository.save(live)

        def save_metadata(metadata: dict) -> None:
            from .url import extract_video_id
            from .videos import VideoStore

            live = repository.get(job_id)
            if live is None:
                return
            video_id = live.video_id or extract_video_id(live.source_url)
            if not video_id:
                return
            try:
                source = VideoStore(repository.client).upsert(video_id, live.source_url, metadata)
            except Exception:
                logger.warning("Job %s: failed to persist video metadata", job_id, exc_info=True)
                return
            if live.video_id != video_id or not live.video:
                live.video_id = video_id
                live.video = source.snapshot()
                repository.save(live)

        from .app_settings import AppSettings, apply_log_level

        job_settings = Settings.resolve(repository.client)
        apply_log_level(AppSettings(repository.client).get("LOG_LEVEL"))
        # One-shot recreate flags: consumed here so a retry never inherits them.
        _force = bool(getattr(record, "force_retranscribe", False))
        _adopt_parent = getattr(record, "recreated_from_job_id", None) or None
        if _force and record.force_retranscribe:
            record.force_retranscribe = False
            repository.save(record)
        result = generate_from_url(
            record.source_url,
            job_settings,
            language=record.language,
            progress=update_progress,
            job_id=job_id,
            max_clips=record.max_clips,
            min_duration=record.min_duration,
            max_duration=record.max_duration,
            burn_subtitles=record.burn_subtitles,
            on_clip_task=save_clip_tasks,
            on_metadata=save_metadata,
            subtitle_style=getattr(record, "subtitle_style", None) or "casual_center",
            subtitle_position=getattr(record, "subtitle_position", None) or "bottom_center",
            subtitle_max_words_per_line=getattr(record, "subtitle_max_words_per_line", None),
            branding={
                "enabled": getattr(record, "brand_enabled", False),
                "template": getattr(record, "brand_template", None) or "glass_end",
                "channel_name": getattr(record, "brand_channel_name", None),
                "avatar_asset": getattr(record, "brand_avatar_asset", None),
                "display_mode": getattr(record, "brand_display_mode", None) or "avatar_name",
                "position": getattr(record, "brand_position", None) or "top_left",
                "timing": getattr(record, "brand_timing", None) or "last",
                "duration": getattr(record, "brand_duration", None) or 2.0,
                "blur": getattr(record, "brand_blur", None),
                "dim": getattr(record, "brand_dim", None),
                "animation": getattr(record, "brand_animation", None) or "fade",
                "additional_text": getattr(record, "brand_additional_text", None),
                "channel_id": (record.video or {}).get("channel_id"),
                "channel_url": (record.video or {}).get("channel_url"),
            },
            ai_chain=_resolve_ai_chain(repository.client, job_settings, job_id),
            redis_client=repository.client,
            adopt_transcript_job_id=_adopt_parent,
            force_retranscribe=_force,
        )
        usage = result.curation.usage
        breakdown = result.curation.score_breakdown or {}
        provider_id = getattr(result.curation, "provider_id", None)
        provider_name = None
        if provider_id:
            try:
                from .gemini_pool import ProviderStore

                found = ProviderStore(repository.client).get(provider_id)
                provider_name = found.name if found else None
            except Exception:
                provider_name = None
        curation = {
            "source": result.curation.source,
            "provider": usage.provider if usage else None,
            "provider_id": provider_id,
            "provider_name": provider_name,
            "model": usage.model if usage else None,
            "model_used": result.curation.model_used,
            "selection_method": result.curation.selection_method,
            "fallback_index": result.curation.fallback_index,
            "attempt": result.curation.attempt,
            "attempts_total": getattr(result.curation, "attempts_total", None),
            "ai_used": result.curation.ai_used,
            "fallback_used": result.curation.fallback_used,
            "input_tokens": usage.input_tokens if usage else None,
            "output_tokens": usage.output_tokens if usage else None,
            "attempted_provider": result.curation.attempted_provider,
            "fallback_reason": result.curation.fallback_reason,
            "heatmap_used": result.curation.heatmap_used,
            "curation_method": result.curation.curation_method,
            "ai_used": result.curation.ai_used,
            "fallback_used": result.curation.fallback_used,
            "score_breakdown": breakdown,
            "fallback_chain": [dict(entry) for entry in (result.curation.fallback_chain or ())],
            "algorithm_version": result.curation.algorithm_version,
            "candidate_count": result.curation.candidate_count,
            "runtime_ms": result.curation.runtime_ms,
            "best_score": result.curation.best_score,
            "heatmap_stats": result.curation.heatmap_stats,
            "duplicates_removed": result.curation.duplicates_removed,
            "overlap_rejected": result.curation.overlap_rejected,
            "transcript_hash": result.curation.transcript_hash,
        }
        logger.info(
            "Job %s curation completed: method=%s source=%s provider=%s model=%s "
            "input_tokens=%s output_tokens=%s candidates=%d",
            job_id,
            curation["curation_method"],
            curation["source"],
            curation["provider"] or "-",
            curation["model"] or "-",
            curation["input_tokens"] if curation["input_tokens"] is not None else "-",
            curation["output_tokens"] if curation["output_tokens"] is not None else "-",
            len(result.curation.candidates),
        )
        task_lookup = {
            task.clip_id: task
            for task in (result.clip_tasks or [])
        }
        clips = [_clip_entry(clip, task_lookup) for clip in result.clips]
        clip_tasks = [task.to_dict() for task in (result.clip_tasks or [])]
        failed_tasks = [task for task in (result.clip_tasks or []) if task.status == "failed"]
        final_status = "partial" if failed_tasks else "completed"
        final_stage = "completed" if final_status == "completed" else "partial"
        record = repository.get(job_id)
        if record is None or record.status == "cancelled":
            return
        record.clips = clips
        record.clip_tasks = clip_tasks
        record.error = None
        repository.transition(job_id, final_status, final_stage, 100)
        record = repository.get(job_id)
        if record:
            record.clips = clips
            record.clip_tasks = clip_tasks
            record.curation = curation
            record.transcript_reused = bool(result.transcript_reused)
            record.transcript_cache_id = result.transcript_cache_id
            if result.transcript_reused:
                logger.info(
                    "[JOB_RECREATE] transcript_reused job=%s cache_id=%s engine=%s",
                    job_id, result.transcript_cache_id, result.transcript_engine,
                )
            repository.save(record)
    except Exception as exc:
        from .errors import CancelledError as _Cancelled
        if isinstance(exc, _Cancelled):
            logger.info("Job %s cancelled during processing", job_id)
            return
        logger.exception("Job %s failed: %s", job_id, exc)
        record = repository.get(job_id)
        if record and record.status != "cancelled":
            record.error = {"code": getattr(exc, "code", "JOB_FAILED"), "message": str(exc), "retryable": False}
            repository.save(record)
            repository.transition(job_id, "failed", "failed", record.progress)


def refresh_video_metadata(job_id: str) -> dict:
    """Backfill/refresh video metadata for one job (async celery task).

    Fetches fresh metadata via yt-dlp, upserts the shared VideoSource, and
    attaches the snapshot to the job. Never raises: failures are returned
    as {"ok": False} so queue processing is not disturbed. Jobs without a
    resolvable video ID or with complete metadata are skipped cheaply.
    """
    from .downloader import video_metadata
    from .url import extract_video_id
    from .videos import VideoStore

    repository = JobRepository(_redis())
    record = repository.get(job_id)
    if record is None:
        return {"ok": False, "reason": "job-not-found"}
    video_id = record.video_id or extract_video_id(record.source_url)
    if not video_id:
        return {"ok": False, "reason": "no-video-id"}
    if record.video and record.video.get("title"):
        cached = VideoStore(repository.client).get(video_id)
        if cached and cached.title:
            if record.video_id != video_id or record.video != cached.snapshot():
                record.video_id = video_id
                record.video = cached.snapshot()
                repository.save(record)
            return {"ok": True, "cached": True}
    try:
        metadata = video_metadata(record.source_url)
    except Exception as exc:
        logger.warning("Refresh metadata for %s failed: %s", job_id, exc)
        return {"ok": False, "reason": "fetch-failed"}
    try:
        source = VideoStore(repository.client).upsert(video_id, record.source_url, metadata)
    except Exception as exc:
        logger.warning("Refresh metadata for %s failed to persist: %s", job_id, exc)
        return {"ok": False, "reason": "persist-failed"}
    record.video_id = video_id
    record.video = source.snapshot()
    repository.save(record)
    return {"ok": True, "cached": False}


def celery_task():
    from celery import Celery
    app = Celery("gaeclip", broker=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))

    @app.task(name="gaeclip.process_job")
    def task(job_id: str):
        return process_job(job_id)

    @app.task(name="gaeclip.refresh_metadata")
    def refresh_task(job_id: str):
        return refresh_video_metadata(job_id)

    return app, task


def refresh_task():
    """Return (app, task) for the metadata refresh queue."""
    from celery import Celery
    app = Celery("gaeclip", broker=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))

    @app.task(name="gaeclip.refresh_metadata")
    def task(job_id: str):
        return refresh_video_metadata(job_id)

    return app, task


def process_publish(publish_job_id: str) -> dict:
    """Celery/local entry point for one social publish job (spec §22).

    Runs the SocialPublishService state machine synchronously. Safe to
    re-run (terminal jobs are idempotent no-ops; active jobs resume).
    """
    from .runtime.factory import is_desktop
    from .social.service import SocialPublishService

    service = SocialPublishService(_redis(), runtime="desktop" if is_desktop() else "server")
    job = service.execute(publish_job_id)
    return {"publish_job_id": job.id, "status": job.status,
            "remote_media_id": job.remote_media_id}


def publish_task():
    """Return (app, task) for the social publish queue."""
    from celery import Celery
    app = Celery("gaeclip", broker=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))

    @app.task(name="gaeclip.publish_clip")
    def task(publish_job_id: str):
        return process_publish(publish_job_id)

    return app, task
