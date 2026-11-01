"""Social publishing core: states, capabilities, retry policy, errors.

Platform-independent. No network, no secrets here — only data + policy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


# -- Publish state machine ------------------------------------------------
# Lifecycle (upload vs processing are DISTINCT):
#   QUEUED/PENDING -> PREPARING -> UPLOADING -> UPLOADED -> PROCESSING -> PUBLISHED
#   PENDING -> SCHEDULED -> (at time) UPLOADING ...
#   UPLOADING timeout after provider accepted -> RECONCILING -> UPLOADED/PROCESSING/...
#   any active state -> FAILED | CANCELLED
#   DRAFT is terminal-success for draft-only providers (TikTok w/o approval).
# PENDING is kept as the legacy queued state (retry re-enters at PENDING);
# QUEUED is the explicit new queued state. Both are active, never terminal.
PENDING = "pending"
QUEUED = "queued"
PREPARING = "preparing"
AUTHORIZING = "authorizing"
UPLOADING = "uploading"
UPLOADED = "uploaded"
PROCESSING = "processing"
RECONCILING = "reconciling"
PUBLISHED = "published"
DRAFT = "draft"
SCHEDULED = "scheduled"
FAILED = "failed"
CANCELLED = "cancelled"

ACTIVE_STATES = frozenset({PENDING, QUEUED, PREPARING, AUTHORIZING, UPLOADING, UPLOADED, PROCESSING, RECONCILING, SCHEDULED})
TERMINAL_STATES = frozenset({PUBLISHED, DRAFT, FAILED, CANCELLED})

_ALLOWED = {
    PENDING: {QUEUED, PREPARING, AUTHORIZING, UPLOADING, SCHEDULED, FAILED, CANCELLED, RECONCILING},
    QUEUED: {PREPARING, AUTHORIZING, UPLOADING, SCHEDULED, FAILED, CANCELLED},
    PREPARING: {UPLOADING, FAILED, CANCELLED},
    AUTHORIZING: {UPLOADING, PREPARING, FAILED, CANCELLED},
    UPLOADING: {UPLOADED, PROCESSING, PUBLISHED, DRAFT, FAILED, CANCELLED, RECONCILING},
    UPLOADED: {PROCESSING, PUBLISHED, DRAFT, FAILED, CANCELLED, RECONCILING},
    PROCESSING: {PUBLISHED, DRAFT, FAILED, CANCELLED, RECONCILING},
    RECONCILING: {UPLOADED, PROCESSING, PUBLISHED, DRAFT, FAILED, CANCELLED, UPLOADING, PENDING},
    SCHEDULED: {PENDING, QUEUED, UPLOADING, FAILED, CANCELLED},
    PUBLISHED: set(),
    DRAFT: set(),
    FAILED: {PENDING},  # retry re-enters at PENDING
    CANCELLED: set(),
}


def can_transition(from_state: str, to_state: str) -> bool:
    """True when the state move is legal (FAILED->PENDING = retry)."""
    return to_state in _ALLOWED.get(from_state, set())


# -- Capabilities ----------------------------------------------------------
@dataclass(frozen=True)
class ProviderCapabilities:
    """Honest capability flags per provider. Never claim what the official
    API does not offer; gate UI + service behavior on these."""

    direct_publish: bool = False
    draft_upload: bool = False
    scheduling: bool = False  # native platform scheduling
    thumbnail: bool = False
    title: bool = True
    description: bool = True
    caption: bool = True
    hashtags: bool = True
    privacy: bool = False
    status_polling: bool = False
    multiple_accounts: bool = True
    # Lifecycle honesty (spec: upload vs processing never conflated).
    supports_upload_progress: bool = False  # 0..1 fraction during UPLOADING
    supports_processing_status: bool = False  # UPLOADED vs PROCESSING distinct
    supports_remote_status_polling: bool = False  # get_publish_status usable
    supports_provider_url: bool = True  # remote_url safe to show Open button
    supports_cancel: bool = False  # remote cancel beyond local state flip
    supports_publish_verification: bool = False  # PUBLISHED means verified
    # Human-readable caveat shown in UI when a headline feature is gated,
    # e.g. "Direct posting requires TikTok audit approval".
    approval_note: str = ""


def is_terminal(status: str) -> bool:
    """Terminal = no further automatic transitions (retry is manual)."""
    return status in TERMINAL_STATES


def is_active(status: str) -> bool:
    """Active = worker/polling may still advance the job."""
    return status in ACTIVE_STATES


# -- Global (multi-platform) status --------------------------------------
# Derived per clip_id, never stored. One platform failing never marks the
# others failed.
GLOBAL_QUEUED = "queued"
GLOBAL_PUBLISHING = "publishing"
GLOBAL_PROCESSING = "processing"
GLOBAL_COMPLETED = "completed"
GLOBAL_PARTIAL_SUCCESS = "partial_success"
GLOBAL_FAILED = "failed"
GLOBAL_CANCELLED = "cancelled"
GLOBAL_SCHEDULED = "scheduled"
GLOBAL_READY = "ready"


def summarize_jobs(statuses: list[str]) -> dict:
    """Aggregate per-platform job statuses into a global view.

    Rules (spec §11/§12):
    - empty -> ready
    - all cancelled -> cancelled
    - any uploading/preparing/authorizing/pending/queued/reconciling -> publishing
    - all processing/uploaded (+ no uploading) -> processing
    - all scheduled -> scheduled
    - all published/draft (+scheduled mixed counts as completed when no active/failed)
    - all failed -> failed
    - some completed + no active -> partial_success (even if rest failed/cancelled)
    - otherwise publishing (mixed active).
    Never averages percentages; counts are the honest global progress.
    """
    total = len(statuses)
    if total == 0:
        return {"total": 0, "completed": 0, "processing": 0, "failed": 0,
                "active": 0, "global_status": GLOBAL_READY}
    completed = sum(1 for s in statuses if s in (PUBLISHED, DRAFT))
    processing = sum(1 for s in statuses if s in (PROCESSING, UPLOADED))
    failed = sum(1 for s in statuses if s == FAILED)
    cancelled = sum(1 for s in statuses if s == CANCELLED)
    scheduled = sum(1 for s in statuses if s == SCHEDULED)
    active_upload = sum(1 for s in statuses if s in (
        PENDING, QUEUED, PREPARING, AUTHORIZING, UPLOADING, RECONCILING))
    active = active_upload + processing + sum(
        1 for s in statuses if s == SCHEDULED)
    if cancelled == total:
        global_status = GLOBAL_CANCELLED
    elif active_upload > 0:
        global_status = GLOBAL_PUBLISHING
    elif processing > 0 and (processing + scheduled == total or processing == total):
        global_status = GLOBAL_PROCESSING
    elif scheduled == total:
        global_status = GLOBAL_SCHEDULED
    elif completed == total or (completed + scheduled == total and completed > 0):
        global_status = GLOBAL_COMPLETED
    elif failed == total:
        global_status = GLOBAL_FAILED
    elif completed > 0 and active == 0:
        global_status = GLOBAL_PARTIAL_SUCCESS
    elif active > 0:
        # Mixed active (e.g. processing + failed) still reads as publishing
        # so the UI keeps polling instead of declaring a final FAILED.
        global_status = GLOBAL_PUBLISHING if (active_upload > 0 or processing > 0) else GLOBAL_PARTIAL_SUCCESS
    else:
        global_status = GLOBAL_PARTIAL_SUCCESS if completed > 0 else GLOBAL_FAILED
    return {"total": total, "completed": completed, "processing": processing,
            "failed": failed, "cancelled": cancelled, "scheduled": scheduled,
            "active": active_upload + processing,
            "global_status": global_status}


# -- Errors -----------------------------------------------------------------
class SocialError(Exception):
    """Base error. `retryable` drives the bounded retry policy; `user_message`
    is shown in UI (never contains tokens); `code` is stable for clients."""

    def __init__(self, message: str, *, code: str = "social_error",
                 retryable: bool = False, user_message: str = ""):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.user_message = user_message or "Publikasi gagal. Coba lagi."


class AuthError(SocialError):
    def __init__(self, message: str = "authentication failed", **kwargs):
        kwargs.setdefault("code", "auth_failed")
        kwargs.setdefault("retryable", False)
        kwargs.setdefault("user_message", "Kredensial tidak valid. Hubungkan ulang akun.")
        super().__init__(message, **kwargs)


class PermissionError(SocialError):
    def __init__(self, message: str = "permission denied", **kwargs):
        kwargs.setdefault("code", "permission_denied")
        kwargs.setdefault("retryable", False)
        kwargs.setdefault("user_message", "Akun tidak memiliki permission yang diperlukan.")
        super().__init__(message, **kwargs)


class ApprovalRequiredError(SocialError):
    def __init__(self, message: str = "platform approval required", **kwargs):
        kwargs.setdefault("code", "approval_required")
        kwargs.setdefault("retryable", False)
        kwargs.setdefault("user_message", "Fitur ini membutuhkan persetujuan/audit dari platform.")
        super().__init__(message, **kwargs)


class AccountRequirementError(SocialError):
    def __init__(self, message: str = "account requirements unmet", **kwargs):
        kwargs.setdefault("code", "account_requirement")
        kwargs.setdefault("retryable", False)
        kwargs.setdefault("user_message", "Akun tidak memenuhi syarat publish platform ini.")
        super().__init__(message, **kwargs)


class TransientError(SocialError):
    def __init__(self, message: str = "transient failure", **kwargs):
        kwargs.setdefault("code", "transient")
        kwargs.setdefault("retryable", True)
        kwargs.setdefault("user_message", "Gangguan jaringan sementara. Mencoba lagi…")
        super().__init__(message, **kwargs)


class MediaError(SocialError):
    def __init__(self, message: str = "invalid media", **kwargs):
        kwargs.setdefault("code", "invalid_media")
        kwargs.setdefault("retryable", False)
        kwargs.setdefault("user_message", "File clip tidak valid untuk platform ini.")
        super().__init__(message, **kwargs)


class QuotaError(SocialError):
    def __init__(self, message: str = "quota exceeded", **kwargs):
        kwargs.setdefault("code", "quota_exceeded")
        kwargs.setdefault("retryable", False)
        kwargs.setdefault("user_message", "Kuota upload platform habis. Coba lagi nanti.")
        super().__init__(message, **kwargs)


def classify_http_status(status: int, message: str = "") -> SocialError:
    """Map HTTP failures to retry policy. 401/403 refresh-or-fail upstream."""
    if status == 401:
        return AuthError(f"http 401: {message}")
    if status == 403:
        return PermissionError(f"http 403: {message}")
    if status == 429 or status in (500, 502, 503, 504):
        return TransientError(f"http {status}: {message}")
    if status == 400:
        return SocialError(f"http 400: {message}", code="invalid_request",
                           user_message="Permintaan tidak valid untuk platform ini.")
    return SocialError(f"http {status}: {message}", code="provider_error",
                       user_message="Platform mengembalikan error.")


# -- Retry policy ------------------------------------------------------------
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 120.0

    def delay_for(self, attempt_number: int) -> float:
        """Bounded exponential backoff: 2, 4, 8, ... capped. Attempt is 1-based."""
        delay = self.base_delay_seconds * (2.0 ** max(0, attempt_number - 1))
        return min(delay, self.max_delay_seconds)


# -- Publish payload/result ---------------------------------------------------
@dataclass(frozen=True)
class PublishRequest:
    clip_id: str
    job_id: str
    provider: str  # youtube | tiktok | instagram | facebook
    account_id: str
    title: str = ""
    caption: str = ""
    hashtags: tuple[str, ...] = ()
    privacy: str = "public"
    scheduled_at: float | None = None  # epoch seconds; None = now
    idempotency_key: str = ""


@dataclass
class PublishResult:
    status: str  # PUBLISHED | DRAFT | SCHEDULED | PROCESSING ...
    remote_media_id: str = ""
    remote_url: str = ""
    detail: dict = field(default_factory=dict)


def now_seconds() -> float:
    return time.time()
