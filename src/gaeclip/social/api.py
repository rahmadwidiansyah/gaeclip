"""Social publishing HTTP layer (spec §44).

Routes follow project conventions (/api/v1/..., HTTPException details
with {code, message}). Responses NEVER contain tokens, codes, or secrets —
only metadata, statuses, and user-facing messages.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class PublishCreateBody(BaseModel):
    clip_id: str
    job_id: str = ""
    provider: str
    account_id: str = ""
    title: str = ""
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)
    privacy: str = "public"
    scheduled_at: float | None = None
    idempotency_key: str = ""


class ConnectBody(BaseModel):
    provider: str
    # Frontend callback page origin, e.g. http://127.0.0.1:3000/oauth/callback.
    # Must be loopback /oauth/callback (validated server-side). When present
    # the authorization URL returns synchronously and no listener is bound.
    redirect_uri: str = ""


class OAuthCallbackBody(BaseModel):
    state: str
    code: str = ""
    error: str = ""


class UpdateAccountBody(BaseModel):
    facebook_page_id: str = ""


class MetadataGenerateBody(BaseModel):
    clip_id: str
    job_id: str = ""
    platforms: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=lambda: ["title", "description", "hashtags"])
    title_override: str | None = None
    description_override: str | None = None
    hashtags_override: list[str] | None = None


def _service(client: Any):
    from .service import SocialPublishService
    from ..runtime.factory import is_desktop

    return SocialPublishService(client, runtime="desktop" if is_desktop() else "server")


def _social_error(exc: Exception):
    from fastapi import HTTPException

    from .states import SocialError

    if isinstance(exc, SocialError):
        status = {"not_found": 404, "invalid_input": 422, "invalid_state": 409,
                  "unknown_provider": 422}.get(exc.code, 400)
        raise HTTPException(status_code=status,
                            detail={"code": exc.code, "message": exc.user_message}) from exc
    raise exc


def _account_public(account) -> dict[str, Any]:
    return {"id": account.id, "provider": account.provider,
            "external_account_id": account.external_account_id,
            "display_name": account.display_name, "status": account.status,
            "scopes": list(account.scopes or ()),
            "expires_at": account.expires_at,
            "created_at": account.created_at, "updated_at": account.updated_at,
            "meta": dict(account.meta or {})}


def _job_public(job, attempts: list | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"id": job.id, "clip_id": job.clip_id,
                             "job_id": job.job_id, "provider": job.provider,
                             "social_account_id": job.social_account_id,
                             "status": job.status, "title": job.title,
                             "caption": job.caption,
                             "hashtags": list(job.hashtags or ()),
                             "privacy": job.privacy,
                             "scheduled_at": job.scheduled_at,
                             "started_at": job.started_at,
                             "completed_at": job.completed_at,
                             "attempts": job.attempts,
                             "error_code": job.error_code,
                             "error_message": job.error_message,
                             "remote_media_id": job.remote_media_id,
                             "remote_url": job.remote_url,
                             # provider_url mirrors remote_url when the provider
                             # supports it; UI shows [Open …] only when present.
                             "provider_url": job.remote_url,
                             "created_at": job.created_at,
                             "updated_at": job.updated_at,
                             # Lifecycle detail: phase + honest progress.
                             # progress is 0..1 during UPLOADING only;
                             # None = indeterminate (no fake percentages).
                             "phase": getattr(job, "phase", "") or "",
                             "progress": getattr(job, "progress", None),
                             "status_message": getattr(job, "status_message", "") or "",
                             "upload_started_at": getattr(job, "upload_started_at", None),
                             "upload_completed_at": getattr(job, "upload_completed_at", None),
                             "processing_started_at": getattr(job, "processing_started_at", None),
                             "published_at": getattr(job, "published_at", None),
                             "failed_at": getattr(job, "failed_at", None),
                             "last_checked_at": getattr(job, "last_checked_at", None),
                             "check_count": getattr(job, "check_count", 0) or 0}
    if attempts is not None:
        body["attempt_history"] = [attempt.to_dict() for attempt in attempts]
    return body


def _clip_summary(service, clip_id: str) -> dict[str, Any]:
    """Global per-clip view for the UI (counts, never averaged %)."""
    from .states import summarize_jobs

    jobs = service.history(clip_id=clip_id, limit=200) if clip_id else []
    summary = summarize_jobs([j.status for j in jobs])
    return summary


def register_social_routes(app, client: Any) -> None:
    """Attach /api/v1/social/* to a FastAPI app built by create_app."""
    from fastapi import HTTPException  # noqa: F401 (re-export guard)

    from .service import provider_capabilities

    @app.get("/api/v1/social/providers")
    def list_providers():
        return {"providers": provider_capabilities()}

    @app.get("/api/v1/social/accounts")
    def list_accounts(provider: str = ""):
        service = _service(client)
        if provider:
            from .service import publisher_for

            try:
                publisher_for(provider)
            except Exception as exc:
                _social_error(exc)
        return {"accounts": [_account_public(item)
                             for item in service.accounts.list(provider)]}

    @app.post("/api/v1/social/accounts/connect", status_code=202)
    def connect_account(body: ConnectBody):
        service = _service(client)
        try:
            from .service import publisher_for

            publisher_for(body.provider)
            return service.connect_start(body.provider,
                                         redirect_uri=body.redirect_uri or "")
        except Exception as exc:
            _social_error(exc)

    @app.post("/api/v1/social/oauth/callback")
    def oauth_callback(body: OAuthCallbackBody):
        """Frontend /oauth/callback lands here with Google's code+state."""
        service = _service(client)
        try:
            return service.complete_oauth_callback(body.state, body.code,
                                                   body.error)
        except Exception as exc:
            _social_error(exc)

    @app.get("/api/v1/social/oauth/session/{session_id}")
    def poll_oauth_session(session_id: str):
        service = _service(client)
        return service.connect_poll(session_id)

    @app.post("/api/v1/social/accounts/{account_id}/reconnect", status_code=202)
    def reconnect_account(account_id: str, body: dict | None = None):
        service = _service(client)
        account = service.accounts.get(account_id)
        if account is None:
            raise HTTPException(status_code=404,
                                detail={"code": "not_found", "message": "Akun tidak ditemukan."})
        try:
            redirect_uri = (body or {}).get("redirect_uri", "") if isinstance(body, dict) else ""
            return service.connect_start(account.provider,
                                         redirect_uri=redirect_uri or "")
        except Exception as exc:
            _social_error(exc)

    @app.put("/api/v1/social/accounts/{account_id}")
    def update_account(account_id: str, body: UpdateAccountBody):
        service = _service(client)
        try:
            account = service.select_facebook_page(
                account_id, body.facebook_page_id or "")
        except Exception as exc:
            _social_error(exc)
        return _account_public(account)

    @app.delete("/api/v1/social/accounts/{account_id}")
    def disconnect_account(account_id: str):
        service = _service(client)
        try:
            return service.disconnect(account_id)
        except Exception as exc:
            _social_error(exc)

    @app.post("/api/v1/social/publish", status_code=202)
    def create_publish(body: PublishCreateBody):
        service = _service(client)
        service.run_due(limit=5)
        try:
            job = service.create_publish(
                clip_id=body.clip_id, job_id=body.job_id,
                provider=body.provider, account_id=body.account_id,
                title=body.title, caption=body.caption,
                hashtags=body.hashtags, privacy=body.privacy,
                scheduled_at=body.scheduled_at,
                idempotency_key=body.idempotency_key)
        except Exception as exc:
            _social_error(exc)
        return _job_public(job)

    @app.get("/api/v1/social/publish")
    def list_publish(clip_id: str = "", limit: int = 100):
        service = _service(client)
        service.run_due(limit=5)
        try:
            service.refresh_processing(limit=5)
        except Exception:
            pass
        jobs = service.history(clip_id=clip_id, limit=max(1, min(200, limit)))
        body: dict[str, Any] = {"jobs": [_job_public(job) for job in jobs]}
        if clip_id:
            body["summary"] = _clip_summary(service, clip_id)
        return body

    @app.get("/api/v1/social/publish/{publish_job_id}")
    def get_publish(publish_job_id: str):
        service = _service(client)
        job = service.jobs.get_job(publish_job_id)
        if job is None:
            raise HTTPException(status_code=404,
                                detail={"code": "not_found",
                                        "message": "Publish job tidak ditemukan."})
        # Opportunistic single-job refresh: PROCESSING/UPLOADED/RECONCILING
        # advances without a dedicated scheduler.
        if job.status in ("processing", "uploaded", "reconciling") and job.remote_media_id:
            try:
                job = service.reconcile_job(job.id)
            except Exception:
                pass
        return _job_public(job, service.jobs.list_attempts(job.id))

    @app.post("/api/v1/social/publish/{publish_job_id}/refresh")
    def refresh_publish(publish_job_id: str):
        """Reconcile one job: provider lookup before any retry (no duplicate)."""
        service = _service(client)
        try:
            job = service.reconcile_job(publish_job_id)
        except Exception as exc:
            _social_error(exc)
        return _job_public(job, service.jobs.list_attempts(job.id))

    @app.post("/api/v1/social/publish/{publish_job_id}/retry", status_code=202)
    def retry_publish(publish_job_id: str):
        service = _service(client)
        try:
            return _job_public(service.retry_job(publish_job_id))
        except Exception as exc:
            _social_error(exc)

    @app.post("/api/v1/social/publish/{publish_job_id}/cancel")
    def cancel_publish(publish_job_id: str):
        service = _service(client)
        try:
            return _job_public(service.cancel_job(publish_job_id))
        except Exception as exc:
            _social_error(exc)

    @app.get("/api/v1/social/publish/{publish_job_id}/media")
    def get_publish_media(publish_job_id: str, token: str = ""):
        """Serve a job's clip file to its job-scoped bearer token.

        Used as Instagram video_url: Meta fetches this URL server-side.
        No session and no account credential — the unguessable,
        single-job, expiring token IS the auth. Logs job id + outcome
        only, never the token. 404 on any mismatch (no oracle).
        """
        from fastapi.responses import FileResponse

        from .service import clip_file_path

        service = _service(client)
        job_id = service.resolve_media_token(token)
        job = service.jobs.get_job(publish_job_id) if job_id else None
        if job is None or job_id != job.id:
            raise HTTPException(status_code=404,
                                detail={"code": "not_found",
                                        "message": "Media tidak ditemukan."})
        clip_path = clip_file_path(service._storage_root(),
                                    job.job_id, job.clip_id)
        if not clip_path.is_file():
            logger.info("[SOCIAL_MEDIA] job=%s media missing", job.id)
            raise HTTPException(status_code=404,
                                detail={"code": "not_found",
                                        "message": "Media tidak ditemukan."})
        logger.info("[SOCIAL_MEDIA] job=%s serving clip bytes", job.id)
        return FileResponse(str(clip_path), media_type="video/mp4",
                            filename=f"{job.clip_id}.mp4")

    @app.post("/api/v1/social/metadata/generate")
    def generate_metadata(body: MetadataGenerateBody):
        from .metadata import generate_metadata

        try:
            return generate_metadata(
                client, job_id=body.job_id, clip_id=body.clip_id,
                platforms=body.platforms, fields=body.fields,
                title_override=body.title_override,
                description_override=body.description_override,
                hashtags_override=body.hashtags_override)
        except Exception as exc:
            _social_error(exc)
