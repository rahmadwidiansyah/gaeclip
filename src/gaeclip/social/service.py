"""SocialPublishService: application layer for social publishing.

Owns the publish state machine, idempotency, bounded retry, scheduling,
history, and queue dispatch — on BOTH runtimes:

- server: Celery task ``gaeclip.publish_clip`` (see celery_app + worker).
- desktop: daemon thread (same execute path; LocalJobRunner stays job-only).

Publishers stay pure (network only); stores stay dumb (kv only).
Structured logs: [SOCIAL_AUTH] [SOCIAL_PUBLISH] [SOCIAL_STATUS]
[SOCIAL_RETRY] [SOCIAL_FAILURE] — never with credentials.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .accounts import (SocialAccount, SocialAccountStore, SocialPublishJob,
                       SocialPublishStore)
from .publishers.base import PublishContext, SocialPublisher
from .publishers.instagram_login import InstagramLoginPublisher
from .publishers.meta import FacebookPublisher
from .publishers.tiktok import TikTokPublisher
from .publishers.youtube import YouTubePublisher
from .secrets import credentials_expired, make_secret_store
from .states import (ACTIVE_STATES, CANCELLED, DRAFT, FAILED, PENDING,
                     PROCESSING, PUBLISHED, SCHEDULED, PublishRequest,
                     RetryPolicy, SocialError, TransientError,
                     can_transition, now_seconds)

logger = logging.getLogger(__name__)

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CLIP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _oauth_client_config(client: Any) -> dict[str, dict[str, str]]:
    """Provider OAuth client credentials from Settings (secrets stay sealed)."""
    from ..app_settings import AppSettings

    settings = AppSettings(client)
    get = lambda key: str(settings.get(key) or "")  # noqa: E731
    return {
        "youtube": {"client_id": get("SOCIAL_YOUTUBE_CLIENT_ID"),
                    "client_secret": get("SOCIAL_YOUTUBE_CLIENT_SECRET")},
        "tiktok": {"client_id": get("SOCIAL_TIKTOK_CLIENT_KEY"),
                   "client_secret": get("SOCIAL_TIKTOK_CLIENT_SECRET")},
        # Instagram Login uses its own Instagram App credentials
        # (App Dashboard > Instagram > API setup with Instagram login),
        # independent from the Facebook Meta App.
        "instagram": {"client_id": get("SOCIAL_INSTAGRAM_APP_ID"),
                      "client_secret": get("SOCIAL_INSTAGRAM_APP_SECRET")},
        "facebook": {"client_id": get("SOCIAL_META_APP_ID"),
                     "client_secret": get("SOCIAL_META_APP_SECRET")},
    }


def _meta_app_id(client: Any) -> str:
    """Public Meta App ID for app-scoped calls (never the secret)."""
    try:
        return str(_oauth_client_config(client).get("facebook", {}).get("client_id", "") or "")
    except Exception:
        return ""


MEDIA_TOKEN_TTL_SECONDS = 2 * 3600


def _public_media_base_url(client: Any) -> str:
    """Public https base serving job clip files (for provider video_url).

    Only an https, non-loopback base is accepted — Meta must fetch it
    from the public internet (typically the Cloudflare Tunnel domain).
    Empty when unconfigured (providers keep their byte-upload flows).
    """
    try:
        from ..app_settings import AppSettings

        base = str(AppSettings(client).get("SOCIAL_PUBLIC_MEDIA_BASE_URL") or "").strip().rstrip("/")
    except Exception:
        return ""
    try:
        import urllib.parse as _parse

        parsed = _parse.urlparse(base)
    except Exception:
        return ""
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    if parsed.hostname.lower() in ("localhost", "127.0.0.1", "::1"):
        return ""
    return base


def _publishers(allow_tiktok_direct: bool = False) -> dict[str, SocialPublisher]:
    return {
        "youtube": YouTubePublisher(),
        "tiktok": TikTokPublisher(allow_direct_post=allow_tiktok_direct),
        "instagram": InstagramLoginPublisher(),
        "facebook": FacebookPublisher(),
    }


def publisher_for(provider: str, **kwargs: Any) -> SocialPublisher:
    try:
        return _publishers(**kwargs)[provider]
    except KeyError:
        raise SocialError(f"unknown provider: {provider}", code="unknown_provider",
                          user_message="Platform tidak didukung.") from None


def provider_capabilities() -> dict[str, dict[str, Any]]:
    """Public capability map for the UI (no secrets, no accounts)."""
    out = {}
    for name, publisher in _publishers().items():
        caps = publisher.capabilities
        out[name] = {
            "direct_publish": caps.direct_publish,
            "draft_upload": caps.draft_upload,
            "scheduling": caps.scheduling,
            "thumbnail": caps.thumbnail,
            "title": caps.title,
            "description": caps.description,
            "caption": caps.caption,
            "hashtags": caps.hashtags,
            "privacy": caps.privacy,
            "status_polling": caps.status_polling,
            "multiple_accounts": caps.multiple_accounts,
            "approval_note": caps.approval_note,
            "supports_upload_progress": caps.supports_upload_progress,
            "supports_processing_status": caps.supports_processing_status,
            "supports_remote_status_polling": caps.supports_remote_status_polling,
            "supports_provider_url": caps.supports_provider_url,
            "supports_cancel": caps.supports_cancel,
            "supports_publish_verification": caps.supports_publish_verification,
        }
    return out


def clip_file_path(storage_root: str | Path, job_id: str, clip_id: str) -> Path:
    """Final GAEClip artifact location. Rejects traversal (mirrors JobStorage)."""
    if not _JOB_ID_RE.match(job_id or "") or not _CLIP_ID_RE.match(clip_id or ""):
        raise SocialError("invalid job/clip id", code="invalid_input",
                          user_message="Job/clip tidak valid.")
    return Path(storage_root).expanduser() / "jobs" / job_id / "clips" / f"{clip_id}.mp4"


class SocialPublishService:
    """Stateless facade; all state lives in the kv client."""

    def __init__(self, client: Any, runtime: str = "server",
                 retry: RetryPolicy | None = None):
        self.client = client
        self.runtime = runtime
        self.retry = retry or RetryPolicy()
        self.accounts = SocialAccountStore(client)
        self.jobs = SocialPublishStore(client)
        self.secrets = make_secret_store(client, runtime)

    # -- accounts ------------------------------------------------------
    def connect_start(self, provider: str,
                      redirect_uri: str = "") -> dict[str, Any]:
        """Begin OAuth: returns {authorization_url?, session_id}.

        With a valid frontend redirect_uri the URL comes back
        synchronously (no listener). Otherwise the loopback-listener flow
        runs and the UI polls for the URL. Raises when the provider is
        not configured (missing client credentials)."""
        from . import oauth_flow

        cfg = _oauth_client_config(self.client).get(provider) or {}
        if not cfg.get("client_id"):
            raise SocialError(f"{provider} OAuth not configured",
                              code="oauth_not_configured",
                              user_message="Isi Client ID provider di Settings → Social Publishing dulu.")
        return oauth_flow.start(self.client, provider, cfg["client_id"],
                                cfg.get("client_secret", ""),
                                redirect_uri=redirect_uri)

    def complete_oauth_callback(self, state: str, code: str = "",
                                error: str = "") -> dict[str, Any]:
        """Finish a frontend-callback flow (see oauth_flow.complete_by_state)."""
        from . import oauth_flow

        return oauth_flow.complete_by_state(self.client, state, code, error)

    def connect_poll(self, session_id: str) -> dict[str, Any]:
        from . import oauth_flow

        return oauth_flow.poll(self, session_id)

    def select_facebook_page(self, account_id: str, facebook_page_id: str) -> Any:
        """Switch a connected Facebook account to another owned Page.

        Validates against a fresh GET me/accounts (never trusts stored
        page_choices alone), then swaps facebook_page_id + page_token +
        display name. Default stays items[0] for old single-Page rows.
        """
        from .publishers.meta import _GraphClient

        account = self.accounts.get(account_id)
        if account is None:
            raise SocialError("account not found", code="not_found",
                              user_message="Akun tidak ditemukan.")
        if account.provider != "facebook":
            raise SocialError("only facebook accounts have pages", code="invalid_input",
                              user_message="Hanya akun Facebook yang punya Page.")
        target = str(facebook_page_id or "").strip()
        if not target:
            raise SocialError("missing page id", code="invalid_input",
                              user_message="Pilih Page dulu.")
        publisher = publisher_for(account.provider)
        token = self._access_token(account, publisher)
        pages = _GraphClient.call("GET", "me/accounts", token,
                                  {"fields": "id,name,access_token"})
        items = (pages.get("data") or []) if isinstance(pages, dict) else []
        match = next((p for p in items if str(p.get("id", "")) == target), None)
        if match is None:
            raise SocialError("page not found for this account", code="not_found",
                              user_message="Page tidak ditemukan di akun ini. Klik Reconnect dan pilih Page-nya.")
        account.external_account_id = str(match.get("id", ""))
        account.display_name = str(match.get("name", "")) or account.display_name
        meta = dict(account.meta or {})
        meta["facebook_page_id"] = str(match.get("id", ""))
        if match.get("access_token"):
            meta["page_token"] = str(match.get("access_token") or "")
        meta["page_choices"] = [{"id": p.get("id"), "name": p.get("name")}
                                for p in items]
        account.meta = meta
        account.status = "connected"
        self.accounts.save(account)
        logger.info("[SOCIAL_AUTH] provider=facebook account=%s page=%s selected",
                    account.id, account.external_account_id)
        return account

    def disconnect(self, account_id: str) -> dict[str, Any]:
        account = self.accounts.get(account_id)
        if account is None:
            raise SocialError("account not found", code="not_found",
                              user_message="Akun tidak ditemukan.")
        publisher = publisher_for(account.provider)
        try:
            credentials = self.secrets.get(account.token_reference) or {}
            token = str(credentials.get("access_token", ""))
            if token:
                publisher.cancel_publish(token, "")  # placeholder no-op hook
        except Exception:
            logger.info("[SOCIAL_AUTH] provider=%s revoke best-effort failed",
                        account.provider)
        try:
            from .publishers.youtube import YouTubePublisher as _Y

            if isinstance(publisher, _Y):
                _revoke_google(self.secrets.get(account.token_reference) or {})
        except Exception:
            pass
        self.secrets.delete(account.token_reference)
        self.accounts.delete(account_id)
        logger.info("[SOCIAL_AUTH] provider=%s account=%s disconnected",
                    account.provider, account_id)
        return {"disconnected": account_id}

    # -- publish jobs ---------------------------------------------------
    def create_publish(self, *, clip_id: str, job_id: str, provider: str,
                       account_id: str, title: str = "", caption: str = "",
                       hashtags: list[str] | tuple = (), privacy: str = "public",
                       scheduled_at: float | None = None,
                       idempotency_key: str = "") -> SocialPublishJob:
        from .states import SCHEDULED

        publisher = publisher_for(provider)  # validates provider name
        account = self.accounts.get(account_id)
        if account is None or account.provider != provider:
            raise SocialError("account not found for provider", code="not_found",
                              user_message="Akun tidak ditemukan.")
        if account.status != "connected":
            raise SocialError("account not connected", code="account_disconnected",
                              user_message="Hubungkan ulang akun dulu.")
        norm_tags = _normalize_hashtags(hashtags)
        key = idempotency_key or (
            f"{clip_id}:{provider}:{account_id}:{title}:{caption}:"
            f"{','.join(norm_tags)}:{privacy}:{scheduled_at or ''}")
        existing = self.jobs.find_by_idempotency(key)
        if existing is not None and existing.status in (
                *ACTIVE_STATES, PUBLISHED, DRAFT, SCHEDULED):
            logger.info("[SOCIAL_PUBLISH] provider=%s clip_id=%s dedup=%s",
                        provider, clip_id, existing.id)
            return existing  # duplicate Publish click -> same job, no re-upload
        job = SocialPublishJob(
            id=SocialPublishStore.new_id(), clip_id=clip_id, job_id=job_id,
            provider=provider, social_account_id=account_id,
            status=PENDING, title=(title or "")[:100],
            caption=(caption or "")[:5000], hashtags=tuple(norm_tags),
            privacy=privacy if privacy in ("public", "unlisted", "private") else "public",
            scheduled_at=scheduled_at, idempotency_key=key,
            created_at=now_seconds(), updated_at=now_seconds())
        if scheduled_at and scheduled_at > now_seconds() + 1 and not publisher.capabilities.scheduling:
            job.status = SCHEDULED  # GAEClip-side wait (runtime must stay alive)
        self.jobs.save_job(job)
        self.jobs.claim_idempotency(key, job.id)
        logger.info("[SOCIAL_PUBLISH] provider=%s clip_id=%s job=%s status=%s",
                    provider, clip_id, job.id, job.status)
        if job.status == PENDING:
            self.enqueue(job.id)
        return job

    def enqueue(self, publish_job_id: str) -> str:
        """Dispatch to the runtime queue. Returns a runner task handle.

        Server + future schedule → celery ETA countdown (native wait).
        Desktop → daemon thread; GAEClip-side SCHEDULED jobs additionally
        run via run_due() while the app lives (documented limitation).
        """
        if self.runtime == "desktop":
            thread = threading.Thread(target=self._thread_entry,
                                      args=(publish_job_id,),
                                      name=f"gaeclip-publish-{publish_job_id}",
                                      daemon=True)
            thread.start()
            return f"thread-{publish_job_id}"
        from ..worker import publish_task

        _, task = publish_task()
        job = self.jobs.get_job(publish_job_id)
        countdown = None
        if job is not None and job.status == SCHEDULED and job.scheduled_at:
            countdown = max(0.0, job.scheduled_at - now_seconds())
        if countdown:
            result = task.apply_async(args=[publish_job_id], countdown=countdown)
        else:
            result = task.apply_async(args=[publish_job_id])
        return getattr(result, "id", None) or publish_job_id

    def _thread_entry(self, publish_job_id: str) -> None:
        try:
            self.execute(publish_job_id)
        except Exception:
            logger.exception("[SOCIAL_FAILURE] desktop publish thread job=%s",
                             publish_job_id)

    def retry_job(self, publish_job_id: str) -> SocialPublishJob:
        job = self._require_job(publish_job_id)
        if job.status != FAILED:
            raise SocialError("only failed jobs can be retried", code="invalid_state",
                              user_message="Hanya job yang gagal bisa di-retry.")
        if not can_transition(job.status, PENDING):
            raise SocialError("retry not allowed", code="invalid_state",
                              user_message="Retry tidak diizinkan.")
        job.status = PENDING
        job.error_code = ""
        job.error_message = ""
        job.attempts = 0
        self.jobs.save_job(job)
        self.enqueue(job.id)
        return job

    def cancel_job(self, publish_job_id: str) -> SocialPublishJob:
        job = self._require_job(publish_job_id)
        if job.status in (PUBLISHED, DRAFT, CANCELLED):
            return job
        job.status = CANCELLED
        job.completed_at = now_seconds()
        self.jobs.save_job(job)
        logger.info("[SOCIAL_PUBLISH] job=%s cancelled", job.id)
        return job

    def history(self, clip_id: str = "", limit: int = 100) -> list[SocialPublishJob]:
        return self.jobs.list_jobs(clip_id=clip_id, limit=limit)

    def run_due(self, limit: int = 20) -> list[str]:
        """Execute SCHEDULED jobs whose time arrived (GAEClip-side scheduling).
        Called opportunistically (publish endpoints) so desktop needs no
        always-on scheduler; server additionally uses celery ETA."""
        started: list[str] = []
        for job in self.jobs.list_jobs(limit=200):
            if job.status != SCHEDULED:
                continue
            if job.scheduled_at and job.scheduled_at > now_seconds():
                continue
            if len(started) >= limit:
                break
            job.status = PENDING
            self.jobs.save_job(job)
            self.enqueue(job.id)
            started.append(job.id)
        return started

    # -- execution ------------------------------------------------------
    def execute(self, publish_job_id: str) -> SocialPublishJob:
        """Run the state machine synchronously (worker thread / celery task).

        Lifecycle: PREPARING -> UPLOADING -> UPLOADED -> PROCESSING -> PUBLISHED.
        Upload completion and provider processing are DISTINCT: UPLOADED means
        the bytes were accepted (remote id persisted immediately), PROCESSING
        means the provider is still working. Timeouts after accept become
        RECONCILING (status lookup first), never a silent duplicate.
        """
        from .states import PREPARING, UPLOADING

        job = self._require_job(publish_job_id)
        if job.status in (PUBLISHED, DRAFT, CANCELLED):
            return job  # idempotent re-entry
        if job.status == FAILED:
            # Crash-recovery (e.g. celery acks_late requeue): re-enter cleanly.
            # Keep remote_media_id: reconcile first instead of re-uploading.
            if job.remote_media_id:
                reconciled = self.reconcile_job(job.id)
                if reconciled.status in (PUBLISHED, DRAFT, PROCESSING,
                                         UPLOADED, RECONCILING):
                    return reconciled
            job.status = PENDING
            job.error_code = ""
            job.error_message = ""
            self.jobs.save_job(job)
        if job.status == SCHEDULED and (job.scheduled_at or 0) > now_seconds():
            return job  # not due yet
        storage_root = self._storage_root()
        clip_path = clip_file_path(storage_root, job.job_id, job.clip_id)
        account = self.accounts.get(job.social_account_id)
        if account is None:
            return self._fail(job, "account_not_found", "Akun telah dihapus.")
        tiktok_direct = bool((account.meta or {}).get("tiktok_direct_post"))
        publisher = publisher_for(job.provider, allow_tiktok_direct=tiktok_direct)
        attempt_no = job.attempts or 0
        while True:
            attempt_no += 1
            if job.status == CANCELLED or self._cancelled(job.id):
                job.status = CANCELLED
                job.phase = "cancelled"
                job.progress = None
                job.status_message = "Publikasi dibatalkan."
                job.completed_at = now_seconds()
                return self.jobs.save_job(job)
            if job.status == SCHEDULED and (job.scheduled_at or 0) > now_seconds():
                wait_for = min(job.scheduled_at - now_seconds(), 60.0)
                logger.info("[SOCIAL_PUBLISH] job=%s waiting %.0fs for schedule",
                            job.id, wait_for)
                time.sleep(max(0.0, wait_for))
                continue
            self._transition(job, PREPARING)
            job.status_message = "Menyiapkan upload…"
            job.phase = "preparing"
            self.jobs.save_job(job)
            self._transition(job, UPLOADING)
            attempt = self._begin_attempt(job, attempt_no)
            try:
                token = self._access_token(account, publisher)
                ctx = PublishContext(
                    request=self._as_request(job), clip_path=clip_path,
                    access_token=lambda: self._access_token(account, publisher),
                    on_progress=lambda frac, stage: self._progress(job, frac, stage),
                    on_uploaded=lambda rid, url: self._on_uploaded(job, rid, url),
                    should_cancel=lambda: self._cancelled(job.id),
                    account_meta={**(account.meta or {}),
                                   "external_account_id": account.external_account_id,
                                   "display_name": account.display_name,
                                   "meta_app_id": _meta_app_id(self.client)
                                   or (account.meta or {}).get("meta_app_id", ""),
                                   **self._media_context(job)},
                )
                result = publisher.publish(ctx)
                # Publishers that skip on_uploaded (old fakes): persist now.
                if result.remote_media_id and not job.remote_media_id:
                    self._on_uploaded(job, result.remote_media_id,
                                      result.remote_url or "")
                return self._handle_publisher_result(job, attempt, result,
                                                     publisher, account)
            except SocialError as exc:
                # Technical detail (HTTP status + provider reason, never
                # credentials) goes to the SERVER LOG for diagnostics; the
                # stored job keeps only the user-facing message.
                logger.info("[SOCIAL_FAILURE] job=%s code=%s detail=%s",
                            job.id, exc.code, str(exc)[:300])
                # Timeout/unknown AFTER the provider accepted the bytes:
                # reconcile instead of failing + duplicating on retry.
                if job.remote_media_id and _is_unknown_failure(exc):
                    self._finish_attempt(attempt, "failed", exc)
                    return self.reconcile_job(job.id)
                if isinstance(exc, TransientError) or getattr(exc, "retryable", False):
                    self._finish_attempt(attempt, "failed", exc)
                    if attempt_no >= self.retry.max_attempts:
                        return self._fail(job, exc.code, exc.user_message, attempt)
                    delay = self.retry.delay_for(attempt_no)
                    logger.info("[SOCIAL_RETRY] job=%s attempt=%d delay=%.0fs code=%s",
                                job.id, attempt_no, delay, exc.code)
                    time.sleep(delay)
                    continue
                if exc.code in ("auth_failed",):
                    refreshed = self._try_refresh(account, publisher)
                    if refreshed and attempt_no == 1:
                        logger.info("[SOCIAL_AUTH] job=%s refreshed once, retrying",
                                    job.id)
                        self._finish_attempt(attempt, "failed", exc)
                        continue
                    self._mark_reconnect(account)
                self._finish_attempt(attempt, "failed", exc)
                return self._fail(job, exc.code, exc.user_message, attempt)
            except Exception:  # never leak internals/tokens
                logger.exception("[SOCIAL_FAILURE] job=%s unexpected", job.id)
                if job.remote_media_id:
                    self._finish_attempt(attempt, "failed", None)
                    return self.reconcile_job(job.id)
                err = SocialError("unexpected publish failure", code="internal",
                                  user_message="Kesalahan internal. Coba lagi.")
                self._finish_attempt(attempt, "failed", err)
                return self._fail(job, err.code, err.user_message, attempt)

    # -- internals ------------------------------------------------------
    def _require_job(self, publish_job_id: str) -> SocialPublishJob:
        job = self.jobs.get_job(publish_job_id)
        if job is None:
            raise SocialError("publish job not found", code="not_found",
                              user_message="Publish job tidak ditemukan.")
        return job

    def _storage_root(self) -> str:
        import os

        return os.getenv("STORAGE_ROOT", "./storage")

    # -- job-scoped public media (provider video_url fetching) --------
    def _media_context(self, job) -> dict:
        """PublishContext extras for URL-based provider uploads.

        When SOCIAL_PUBLIC_MEDIA_BASE_URL is configured, issues a short
        job-scoped bearer token and the public file URL Meta will fetch.
        The token is unguessable, single-job, 2h expiry, and never logged
        or stored on the job row. Empty dict when unconfigured.
        """
        base = _public_media_base_url(self.client)
        if not base:
            return {}
        token = self.issue_media_token(job.id)
        return {"media_base_url": base,
                "media_token": token,
                "media_url": (f"{base}/api/v1/social/publish/{job.id}"
                              f"/media?token={token}")}

    def issue_media_token(self, publish_job_id: str) -> str:
        import json as _json
        import secrets as _secrets

        from .states import now_seconds

        token = _secrets.token_urlsafe(32)
        payload = _json.dumps({"job_id": publish_job_id,
                               "expires_at": now_seconds() + MEDIA_TOKEN_TTL_SECONDS})
        self.client.set(f"gaeclip:social:media:{token}", payload)
        return token

    def resolve_media_token(self, token: str) -> str | None:
        """Validate a media bearer token -> publish job id, else None."""
        import json as _json

        from .states import now_seconds

        if not token or len(token) < 16:
            return None
        try:
            raw = self.client.get(f"gaeclip:social:media:{token}")
        except Exception:
            return None
        if raw is None:
            return None
        try:
            data = _json.loads(raw.decode("utf-8")
                               if isinstance(raw, (bytes, bytearray))
                               else str(raw))
            if float(data.get("expires_at", 0)) <= now_seconds():
                return None
            return str(data.get("job_id", "") or "") or None
        except (ValueError, TypeError, AttributeError):
            return None

    def _transition(self, job: SocialPublishJob, to: str) -> None:
        from .states import (CANCELLED, DRAFT, FAILED, PREPARING, PROCESSING,
                             PUBLISHED, RECONCILING, UPLOADED, UPLOADING)

        if can_transition(job.status, to):
            job.status = to
        now = now_seconds()
        if job.started_at is None and job.status in (
                PREPARING, UPLOADING, UPLOADED, PROCESSING, RECONCILING):
            job.started_at = now
        if job.status == UPLOADING and job.upload_started_at is None:
            job.upload_started_at = now
            job.phase = "uploading"
        elif job.status == UPLOADED:
            if job.upload_completed_at is None:
                job.upload_completed_at = now
            job.phase = "uploaded"
            job.progress = 1.0
        elif job.status == PROCESSING:
            if job.processing_started_at is None:
                job.processing_started_at = now
            job.phase = "processing"
            job.progress = None  # indeterminate: never fake a percentage
        elif job.status == RECONCILING:
            job.phase = "reconciling"
            job.status_message = "Checking upload status…"
        self.jobs.save_job(job)

    def _begin_attempt(self, job: SocialPublishJob, attempt_no: int):
        from .accounts import SocialPublishAttempt

        job.attempts = attempt_no
        self.jobs.save_job(job)
        attempt = SocialPublishAttempt(
            id=f"{job.id}-a{attempt_no}", publish_job_id=job.id,
            attempt_number=attempt_no, started_at=now_seconds(), status="uploading")
        return self.jobs.save_attempt(attempt)

    def _finish_attempt(self, attempt, status: str, exc: SocialError | None) -> None:
        attempt.status = status
        attempt.completed_at = now_seconds()
        if exc is not None:
            attempt.error_code = exc.code
            attempt.error_message = exc.user_message
        self.jobs.save_attempt(attempt)

    def _progress(self, job: SocialPublishJob, frac: float, stage: str) -> None:
        """Persist honest progress. Only UPLOADING carries a real fraction;
        processing/uploaded/published stages are indeterminate (None)."""
        from .states import PROCESSING, UPLOADED, UPLOADING

        try:
            frac = max(0.0, min(1.0, float(frac)))
        except (TypeError, ValueError):
            frac = 0.0
        stage = (stage or "").lower()
        if stage in ("uploading", "upload"):
            if can_transition(job.status, UPLOADING) or job.status == UPLOADING:
                job.status = UPLOADING
            job.phase = "uploading"
            job.progress = frac
            job.status_message = f"Uploading · {int(frac * 100)}%"
            if job.upload_started_at is None:
                job.upload_started_at = now_seconds()
        elif stage in ("finalizing", "finalize", "finish", "finishing"):
            # Bytes fully sent, provider finalize in flight. Status stays
            # UPLOADING — never UPLOADED (nothing is published yet, no
            # remote id exists). Indeterminate progress so the UI shows
            # "Finalizing upload…" instead of a fake 100%/Published.
            if job.status != UPLOADING and can_transition(job.status, UPLOADING):
                job.status = UPLOADING
            job.phase = "finalizing"
            job.progress = None
            job.status_message = "Finalizing upload…"
        elif stage in ("uploaded", "upload_complete", "upload-complete"):
            self._transition(job, UPLOADED)
            job.status_message = "Upload complete · waiting for processing"
        elif stage in ("processing", "process"):
            if job.status != PROCESSING:
                self._transition(job, PROCESSING)
            job.status_message = _processing_message(job.provider)
        elif stage in ("published", "complete", "done"):
            job.phase = "published"
            job.progress = 1.0
        job.updated_at = now_seconds()
        self.jobs.save_job(job)
        logger.info("[SOCIAL_STATUS] job=%s stage=%s progress=%s",
                    job.id, stage,
                    f"{frac * 100:.0f}%" if job.progress is not None else "—")

    def _on_uploaded(self, job: SocialPublishJob, remote_id: str,
                     remote_url: str) -> None:
        """Persist the provider object the instant bytes are accepted."""
        from .states import UPLOADED

        if remote_id and not job.remote_media_id:
            job.remote_media_id = remote_id
        if remote_url and not job.remote_url:
            job.remote_url = remote_url
        elif remote_id and not job.remote_url and job.provider == "youtube":
            job.remote_url = f"https://youtu.be/{remote_id}"
        self._transition(job, UPLOADED)
        job.status_message = "Upload complete · waiting for processing"
        job.updated_at = now_seconds()
        self.jobs.save_job(job)
        logger.info("[SOCIAL_PUBLISH] job=%s uploaded remote=%s",
                    job.id, job.remote_media_id)

    def _handle_publisher_result(self, job: SocialPublishJob, attempt,
                                 result, publisher, account) -> SocialPublishJob:
        """Route a publisher return through polling when PROCESSING."""
        from .states import DRAFT, PROCESSING, PUBLISHED, SCHEDULED, UPLOADED

        if result.status == PROCESSING:
            # Bytes accepted; provider still working. Poll (bounded) before
            # declaring the durable PROCESSING state the UI explains.
            if result.remote_media_id and not job.remote_media_id:
                self._on_uploaded(job, result.remote_media_id,
                                  result.remote_url or "")
            self._transition(job, PROCESSING)
            job.status_message = _processing_message(job.provider)
            self.jobs.save_job(job)
            polled = self._poll_remote(job, publisher, account)
            if polled in (PUBLISHED, DRAFT):
                result.status = polled
                return self._succeed(job, attempt, result)
            if polled == UPLOADED:
                self._transition(job, UPLOADED)
                job.status_message = "Upload complete · waiting for processing"
                self.jobs.save_job(job)
                attempt.status = "processing"
                attempt.completed_at = None
                self.jobs.save_attempt(attempt)
                return job
            # Still processing after bounded polling: durable PROCESSING,
            # never a fake FAILED. Frontend refreshes via reconcile/refresh.
            attempt.status = "processing"
            attempt.completed_at = None
            self.jobs.save_attempt(attempt)
            logger.info("[SOCIAL_PUBLISH] job=%s still processing remote=%s",
                        job.id, job.remote_media_id)
            return job
        return self._succeed(job, attempt, result)

    def _poll_remote(self, job: SocialPublishJob, publisher,
                     account, max_attempts: int = 6) -> str | None:
        """Bounded provider status polling. Returns normalized state or None.

        Delays: 5,10,15,30,30,60s. Never flips to FAILED on timeout — the
        caller keeps PROCESSING. Auth/refresh handled via _access_token.
        """
        from .states import FAILED

        if not job.remote_media_id:
            return None
        caps = getattr(publisher, "capabilities", None)
        if caps is not None and not getattr(caps, "supports_remote_status_polling", False):
            return None
        delays = [5.0, 10.0, 15.0, 30.0, 30.0, 60.0][:max_attempts]
        last: str | None = None
        for index in range(max_attempts):
            try:
                token = self._access_token(account, publisher)
                state = publisher.get_publish_status(token, job.remote_media_id)
            except SocialError as exc:
                logger.info("[SOCIAL_STATUS] job=%s poll=%d code=%s",
                            job.id, index + 1, exc.code)
                if exc.code in ("auth_failed",):
                    if self._try_refresh(account, publisher):
                        continue
                last = None
            except Exception:
                logger.info("[SOCIAL_STATUS] job=%s poll=%d unexpected", job.id, index + 1)
                last = None
            else:
                last = state
                job.last_checked_at = now_seconds()
                job.check_count = (job.check_count or 0) + 1
                self.jobs.save_job(job)
                if state in ("published", "draft", "failed", "uploaded", "processing"):
                    if state != "processing":
                        return state
            if index < len(delays) - 1:
                # Sleep between polls; tests patch time.sleep.
                time.sleep(delays[index])
        if last == FAILED:
            return FAILED
        return last

    def reconcile_job(self, publish_job_id: str) -> SocialPublishJob:
        """UPLOAD_REQUEST_UNKNOWN handler: lookup before re-upload.

        1. provider status lookup when remote_media_id exists
        2. else RECONCILING (UI: "Checking upload status...")
        Never creates a duplicate upload from this path.
        """
        from .states import (DRAFT, FAILED, PROCESSING, PUBLISHED, RECONCILING,
                             UPLOADED)

        job = self._require_job(publish_job_id)
        if job.status in (PUBLISHED, DRAFT, CANCELLED):
            return job
        if not job.remote_media_id:
            # No provider object to look up: stay honest, don't fake FAILED.
            if job.status not in (FAILED,):
                self._transition(job, RECONCILING)
                job.status_message = "Checking upload status…"
                self.jobs.save_job(job)
            return job
        account = self.accounts.get(job.social_account_id)
        if account is None:
            return self._fail(job, "account_not_found", "Akun telah dihapus.")
        tiktok_direct = bool((account.meta or {}).get("tiktok_direct_post"))
        try:
            publisher = publisher_for(job.provider, allow_tiktok_direct=tiktok_direct)
        except SocialError as exc:
            return self._fail(job, exc.code, exc.user_message)
        if job.status != RECONCILING:
            self._transition(job, RECONCILING)
            self.jobs.save_job(job)
        try:
            token = self._access_token(account, publisher)
            state = publisher.get_publish_status(token, job.remote_media_id)
        except SocialError as exc:
            logger.info("[SOCIAL_STATUS] job=%s reconcile code=%s", job.id, exc.code)
            job.last_checked_at = now_seconds()
            job.check_count = (job.check_count or 0) + 1
            job.status_message = "Checking upload status…"
            self.jobs.save_job(job)
            return job
        except Exception:
            logger.exception("[SOCIAL_STATUS] job=%s reconcile unexpected", job.id)
            return job
        job.last_checked_at = now_seconds()
        job.check_count = (job.check_count or 0) + 1
        if state == PUBLISHED:
            return self._succeed(job, self._latest_attempt(job) or self._begin_attempt(job, (job.attempts or 0) + 1),
                                 _result(job, PUBLISHED))
        if state == DRAFT:
            return self._succeed(job, self._latest_attempt(job) or self._begin_attempt(job, (job.attempts or 0) + 1),
                                 _result(job, DRAFT))
        if state == FAILED:
            return self._fail(job, "processing_failed",
                              "Provider could not process this video.")
        if state == UPLOADED:
            self._transition(job, UPLOADED)
            job.status_message = "Upload complete · waiting for processing"
        else:
            self._transition(job, PROCESSING)
            job.status_message = _processing_message(job.provider)
        self.jobs.save_job(job)
        return job

    def refresh_processing(self, limit: int = 20) -> list[str]:
        """Opportunistic polling for PROCESSING/UPLOADED/RECONCILING jobs.

        Called from publish API reads so Docker needs no extra scheduler and
        desktop refreshes while the app lives. Bounded per call.
        """
        refreshed: list[str] = []
        for job in self.jobs.list_jobs(limit=200):
            if job.status not in ("processing", "uploaded", "reconciling"):
                continue
            if not job.remote_media_id:
                continue
            if len(refreshed) >= limit:
                break
            try:
                before = job.status
                after = self.reconcile_job(job.id)
                if after.status != before or after.remote_media_id:
                    refreshed.append(job.id)
            except Exception:
                continue
        return refreshed

    def summarize_clip(self, clip_id: str) -> dict:
        """Global per-clip view: per-platform truth + honest counts."""
        from .states import summarize_jobs

        jobs = self.history(clip_id=clip_id, limit=200)
        summary = summarize_jobs([j.status for j in jobs])
        return {"clip_id": clip_id, "jobs": jobs, **summary}

    def _latest_attempt(self, job: SocialPublishJob):
        attempts = self.jobs.list_attempts(job.id)
        return attempts[-1] if attempts else None

    def _cancelled(self, publish_job_id: str) -> bool:
        current = self.jobs.get_job(publish_job_id)
        return current is not None and current.status == CANCELLED

    def _fail(self, job: SocialPublishJob, code: str, message: str,
              attempt=None) -> SocialPublishJob:
        from .states import FAILED

        job.status = FAILED
        job.phase = "failed"
        job.progress = None
        job.error_code = code
        job.error_message = message
        job.status_message = message
        job.completed_at = now_seconds()
        job.failed_at = now_seconds()
        self.jobs.save_job(job)
        logger.info("[SOCIAL_FAILURE] job=%s code=%s", job.id, code)
        return job

    def _succeed(self, job: SocialPublishJob, attempt, result) -> SocialPublishJob:
        from .states import DRAFT, PROCESSING, PUBLISHED, SCHEDULED, UPLOADED

        job.status = result.status
        if result.remote_media_id:
            job.remote_media_id = result.remote_media_id
        if result.remote_url:
            job.remote_url = result.remote_url
        job.error_code = ""
        job.error_message = ""
        now = now_seconds()
        if result.status == PUBLISHED:
            job.phase = "published"
            job.progress = 1.0
            job.status_message = "Published"
            job.published_at = now
            job.completed_at = now
        elif result.status == DRAFT:
            job.phase = "draft"
            job.progress = 1.0
            job.status_message = "Saved as draft"
            job.completed_at = now
        elif result.status == SCHEDULED:
            job.phase = "scheduled"
            job.progress = None
            job.status_message = "Scheduled"
            job.completed_at = now
        elif result.status in (PROCESSING, UPLOADED):
            # Handled by _handle_publisher_result; direct succeed keeps honest.
            job.phase = "processing" if result.status == PROCESSING else "uploaded"
            job.progress = None if result.status == PROCESSING else 1.0
            job.status_message = _processing_message(job.provider)
            if job.processing_started_at is None and result.status == PROCESSING:
                job.processing_started_at = now
        else:
            job.phase = result.status
            job.status_message = ""
            job.completed_at = now
        self.jobs.save_job(job)
        if attempt is not None:
            attempt.status = "succeeded" if result.status in (
                PUBLISHED, DRAFT, SCHEDULED) else result.status
            if result.status in (PUBLISHED, DRAFT, SCHEDULED):
                attempt.completed_at = now
            self.jobs.save_attempt(attempt)
        logger.info("[SOCIAL_PUBLISH] job=%s status=%s remote=%s",
                    job.id, result.status, job.remote_media_id)
        return job

    def _as_request(self, job: SocialPublishJob):
        from .states import PublishRequest

        return PublishRequest(
            clip_id=job.clip_id, job_id=job.job_id, provider=job.provider,
            account_id=job.social_account_id, title=job.title,
            caption=job.caption, hashtags=tuple(job.hashtags),
            privacy=job.privacy, scheduled_at=job.scheduled_at,
            idempotency_key=job.idempotency_key)

    def _access_token(self, account: SocialAccount, publisher: SocialPublisher) -> str:
        credentials = self.secrets.get(account.token_reference) or {}
        token = str(credentials.get("access_token", ""))
        if token and not credentials_expired(credentials):
            return token
        if credentials.get("refresh_token"):
            refreshed = self._try_refresh(account, publisher)
            if refreshed:
                credentials = self.secrets.get(account.token_reference) or {}
                token = str(credentials.get("access_token", ""))
                if token:
                    return token
        from .states import AuthError

        self._mark_reconnect(account)
        raise AuthError("stored credentials expired and refresh failed")

    def _try_refresh(self, account: SocialAccount, publisher: SocialPublisher) -> bool:
        # Instagram Login has no refresh_token grant: the long-lived token
        # itself is refreshed via GET /refresh_access_token (token must be
        # unexpired and >= 24h old). Handled here, isolated from the
        # generic refresh_token flow used by the other providers.
        if account.provider == "instagram":
            return self._try_refresh_instagram(account)
        try:
            from . import oauth as _oauth

            credentials = self.secrets.get(account.token_reference) or {}
            refresh_token = str(credentials.get("refresh_token", ""))
            if not refresh_token:
                return False
            cfg = _oauth_client_config(self.client).get(account.provider) or {}
            provider_cfg = _provider_oauth_config(account.provider)
            response = _oauth.refresh_access_token(
                token_url=provider_cfg.token_url, refresh_token=refresh_token,
                client_id=cfg.get("client_id", ""),
                client_secret=cfg.get("client_secret", ""),
                client_id_param=provider_cfg.client_id_param)
            merged = dict(credentials)
            fresh = _oauth.normalize_credentials(
                response, tuple(credentials.get("scopes", [])))
            if not fresh.get("refresh_token"):
                fresh["refresh_token"] = refresh_token  # providers often omit it
            merged.update(fresh)
            self.secrets.put(account.token_reference, merged)
            account.expires_at = merged.get("expires_at")
            self.accounts.save(account)
            logger.info("[SOCIAL_AUTH] provider=%s account=%s token refreshed",
                        account.provider, account.id)
            return True
        except Exception:
            logger.info("[SOCIAL_AUTH] provider=%s account=%s refresh failed",
                        account.provider, account.id)
            return False

    def _try_refresh_instagram(self, account: SocialAccount) -> bool:
        try:
            from . import oauth as _oauth
            from .publishers.instagram_login import refresh_long_lived

            credentials = self.secrets.get(account.token_reference) or {}
            current = str(credentials.get("access_token", ""))
            if not current:
                return False
            fresh = refresh_long_lived(current)
            merged = dict(credentials)
            merged.update(_oauth.normalize_credentials(
                fresh, tuple(credentials.get("scopes", []))))
            self.secrets.put(account.token_reference, merged)
            account.expires_at = merged.get("expires_at")
            self.accounts.save(account)
            logger.info("[SOCIAL_AUTH] provider=instagram account=%s token refreshed",
                        account.id)
            return True
        except Exception:
            logger.info("[SOCIAL_AUTH] provider=instagram account=%s refresh failed",
                        account.id)
            return False

    def _mark_reconnect(self, account: SocialAccount) -> None:
        try:
            account.status = "needs_reconnect"
            self.accounts.save(account)
        except Exception:
            pass


def _provider_oauth_config(provider: str):
    from . import oauth as _oauth
    from .publishers.instagram_login import INSTAGRAM_LOGIN_OAUTH
    from .publishers.meta import FACEBOOK_OAUTH
    from .publishers.tiktok import TIKTOK_OAUTH
    from .publishers.youtube import YOUTUBE_OAUTH

    # Facebook (Facebook Login, graph.facebook.com) and Instagram
    # (Instagram Login / Business Login, www.instagram.com +
    # graph.instagram.com) are INDEPENDENT OAuth connections with
    # independent endpoints, credentials, and scopes. The legacy
    # meta.INSTAGRAM_OAUTH (Facebook-Login era) is retired from this map;
    # old rows are detected as legacy at publish time and must reconnect.
    return {"youtube": YOUTUBE_OAUTH, "tiktok": TIKTOK_OAUTH,
            "instagram": INSTAGRAM_LOGIN_OAUTH, "facebook": FACEBOOK_OAUTH}[provider]


def _revoke_google(credentials: dict) -> None:
    import urllib.parse
    import urllib.request

    token = str(credentials.get("refresh_token") or credentials.get("access_token") or "")
    if not token:
        return
    body = urllib.parse.urlencode({"token": token}).encode()
    request = urllib.request.Request("https://oauth2.googleapis.com/revoke", data=body)
    try:
        with urllib.request.urlopen(request, timeout=15):
            pass
    except Exception:
        pass


def _normalize_hashtags(tags) -> list[str]:
    out: list[str] = []
    for tag in tags or []:
        cleaned = str(tag).strip().lstrip("#").replace(" ", "")
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out[:30]


def _is_unknown_failure(exc: Exception) -> bool:
    """Timeout/connection loss where the provider may already have the bytes.

    Any retryable/transient failure with an existing remote id, plus explicit
    timeout/connection signals, routes to RECONCILING instead of FAILED.
    """
    from .states import TransientError

    if isinstance(exc, TransientError) or getattr(exc, "retryable", False):
        return True
    text = str(exc).lower()
    return any(key in text for key in (
        "timeout", "timed out", "deadline", "connection",
        "unreachable", "reset by peer", "broken pipe", "temporarily"))


def _processing_message(provider: str) -> str:
    names = {"youtube": "YouTube", "tiktok": "TikTok",
             "instagram": "Instagram", "facebook": "Facebook"}
    label = names.get(provider, provider)
    if provider == "youtube":
        return "YouTube is processing your video"
    if provider == "tiktok":
        return "TikTok is processing your video"
    if provider in ("instagram", "facebook"):
        return f"{label} is processing your video"
    return f"{label} is processing"


def _result(job, status: str):
    """Synthetic PublishResult preserving the already-saved remote."""
    from .states import PublishResult

    return PublishResult(status=status, remote_media_id=job.remote_media_id,
                         remote_url=job.remote_url)
