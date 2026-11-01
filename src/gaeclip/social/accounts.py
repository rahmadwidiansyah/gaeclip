"""Social account + publish-job persistence over the existing kv client.

Works unchanged on SQLite (desktop) and Redis (server): only get/set/
delete/scan_iter are used. Secrets are NEVER stored here — only a
token_reference that the SecretStore resolves.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SocialAccount:
    id: str
    provider: str  # youtube | tiktok | instagram | facebook
    external_account_id: str = ""
    display_name: str = ""
    status: str = "connected"  # connected | needs_reconnect | disconnected
    token_reference: str = ""
    scopes: tuple[str, ...] = ()
    expires_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scopes"] = list(self.scopes)
        return data

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SocialAccount":
        scopes = data.get("scopes") or []
        return SocialAccount(
            id=data["id"], provider=data.get("provider", ""),
            external_account_id=data.get("external_account_id", ""),
            display_name=data.get("display_name", ""),
            status=data.get("status", "connected"),
            token_reference=data.get("token_reference", ""),
            scopes=tuple(scopes),
            expires_at=data.get("expires_at"),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            meta=data.get("meta") or {},
        )


@dataclass
class SocialPublishJob:
    id: str
    clip_id: str
    job_id: str = ""
    provider: str = ""
    social_account_id: str = ""
    status: str = "pending"
    title: str = ""
    caption: str = ""
    hashtags: tuple[str, ...] = ()
    privacy: str = "public"
    scheduled_at: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    attempts: int = 0
    error_code: str = ""
    error_message: str = ""  # user-facing only, never secrets
    remote_media_id: str = ""
    remote_url: str = ""
    idempotency_key: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    # Lifecycle detail (upload vs processing never conflated; all optional
    # so old rows decode cleanly). progress is a REAL 0..1 fraction during
    # UPLOADING only; None = indeterminate (no fake percentages).
    phase: str = ""
    progress: float | None = None
    status_message: str = ""
    upload_started_at: float | None = None
    upload_completed_at: float | None = None
    processing_started_at: float | None = None
    published_at: float | None = None
    failed_at: float | None = None
    last_checked_at: float | None = None
    check_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["hashtags"] = list(self.hashtags)
        return data

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SocialPublishJob":
        hashtags = data.get("hashtags") or []
        progress = data.get("progress")
        try:
            progress = float(progress) if progress is not None else None
        except (TypeError, ValueError):
            progress = None
        if progress is not None:
            progress = max(0.0, min(1.0, progress))
        return SocialPublishJob(
            id=data["id"], clip_id=data.get("clip_id", ""),
            job_id=data.get("job_id", ""), provider=data.get("provider", ""),
            social_account_id=data.get("social_account_id", ""),
            status=data.get("status", "pending"),
            title=data.get("title", ""), caption=data.get("caption", ""),
            hashtags=tuple(hashtags), privacy=data.get("privacy", "public"),
            scheduled_at=data.get("scheduled_at"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            attempts=data.get("attempts", 0),
            error_code=data.get("error_code", ""),
            error_message=data.get("error_message", ""),
            remote_media_id=data.get("remote_media_id", ""),
            remote_url=data.get("remote_url", ""),
            idempotency_key=data.get("idempotency_key", ""),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            phase=data.get("phase", "") or "",
            progress=progress,
            status_message=data.get("status_message", "") or "",
            upload_started_at=data.get("upload_started_at"),
            upload_completed_at=data.get("upload_completed_at"),
            processing_started_at=data.get("processing_started_at"),
            published_at=data.get("published_at"),
            failed_at=data.get("failed_at"),
            last_checked_at=data.get("last_checked_at"),
            check_count=int(data.get("check_count", 0) or 0),
        )


@dataclass
class SocialPublishAttempt:
    id: str
    publish_job_id: str
    attempt_number: int
    started_at: float = 0.0
    completed_at: float | None = None
    status: str = "uploading"
    error_code: str = ""
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decode(raw: Any) -> str | None:
    if raw is None:
        return None
    return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)


class SocialAccountStore:
    """CRUD for accounts. Keyspace: gaeclip:social:account:<id>."""

    def __init__(self, client: Any, prefix: str = "gaeclip"):
        self.client = client
        self.prefix = prefix

    def _key(self, account_id: str) -> str:
        return f"{self.prefix}:social:account:{account_id}"

    def save(self, account: SocialAccount) -> SocialAccount:
        account.updated_at = time.time()
        if not account.created_at:
            account.created_at = account.updated_at
        self.client.set(self._key(account.id), json.dumps(account.to_dict()))
        return account

    def get(self, account_id: str) -> SocialAccount | None:
        raw = _decode(self.client.get(self._key(account_id)))
        if not raw:
            return None
        try:
            return SocialAccount.from_dict(json.loads(raw))
        except (ValueError, KeyError, TypeError):
            return None

    def list(self, provider: str = "") -> list[SocialAccount]:
        out: list[SocialAccount] = []
        for key in self.client.scan_iter(match=f"{self.prefix}:social:account:*", count=100):
            raw = _decode(self.client.get(key if isinstance(key, str) else key))
            if not raw:
                continue
            try:
                account = SocialAccount.from_dict(json.loads(raw))
            except (ValueError, KeyError, TypeError):
                continue
            if provider and account.provider != provider:
                continue
            out.append(account)
        out.sort(key=lambda item: item.created_at)
        return out

    def delete(self, account_id: str) -> bool:
        return bool(self.client.delete(self._key(account_id)))

    @staticmethod
    def new_id(provider: str) -> str:
        return f"social_{provider}_{uuid.uuid4().hex[:12]}"


class SocialPublishStore:
    """CRUD + idempotency for publish jobs and attempts."""

    def __init__(self, client: Any, prefix: str = "gaeclip"):
        self.client = client
        self.prefix = prefix

    def _job_key(self, job_id: str) -> str:
        return f"{self.prefix}:social:publish:{job_id}"

    def _idem_key(self, idem: str) -> str:
        return f"{self.prefix}:social:publish:idem:{idem}"

    def _attempt_key(self, attempt_id: str) -> str:
        return f"{self.prefix}:social:attempt:{attempt_id}"

    def save_job(self, job: SocialPublishJob) -> SocialPublishJob:
        job.updated_at = time.time()
        if not job.created_at:
            job.created_at = job.updated_at
        self.client.set(self._job_key(job.id), json.dumps(job.to_dict()))
        return job

    def get_job(self, job_id: str) -> SocialPublishJob | None:
        raw = _decode(self.client.get(self._job_key(job_id)))
        if not raw:
            return None
        try:
            return SocialPublishJob.from_dict(json.loads(raw))
        except (ValueError, KeyError, TypeError):
            return None

    def find_by_idempotency(self, idem: str) -> SocialPublishJob | None:
        if not idem:
            return None
        raw = _decode(self.client.get(self._idem_key(idem)))
        return self.get_job(raw) if raw else None

    def claim_idempotency(self, idem: str, job_id: str) -> None:
        if idem:
            self.client.set(self._idem_key(idem), job_id)

    def list_jobs(self, clip_id: str = "", limit: int = 100) -> list[SocialPublishJob]:
        out: list[SocialPublishJob] = []
        for key in self.client.scan_iter(match=f"{self.prefix}:social:publish:*", count=200):
            key_str = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
            if ":idem:" in key_str:
                continue
            raw = _decode(self.client.get(key_str))
            if not raw:
                continue
            try:
                job = SocialPublishJob.from_dict(json.loads(raw))
            except (ValueError, KeyError, TypeError):
                continue
            if clip_id and job.clip_id != clip_id:
                continue
            out.append(job)
        out.sort(key=lambda item: item.created_at, reverse=True)
        return out[:limit]

    def save_attempt(self, attempt: SocialPublishAttempt) -> SocialPublishAttempt:
        self.client.set(self._attempt_key(attempt.id), json.dumps(attempt.to_dict()))
        return attempt

    def list_attempts(self, publish_job_id: str) -> list[SocialPublishAttempt]:
        out: list[SocialPublishAttempt] = []
        for key in self.client.scan_iter(match=f"{self.prefix}:social:attempt:*", count=200):
            key_str = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
            raw = _decode(self.client.get(key_str))
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if data.get("publish_job_id") != publish_job_id:
                continue
            out.append(SocialPublishAttempt(**data))
        out.sort(key=lambda item: item.attempt_number)
        return out

    @staticmethod
    def new_id() -> str:
        return f"spub_{uuid.uuid4().hex[:12]}"
