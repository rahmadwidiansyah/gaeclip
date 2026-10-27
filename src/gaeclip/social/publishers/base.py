"""SocialPublisher interface + shared publish context.

UI talks to SocialPublishService; the service drives these providers.
Providers never touch the database, the queue, or secrets storage —
they receive everything through PublishContext.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..states import ProviderCapabilities, PublishRequest, PublishResult

logger = logging.getLogger(__name__)


@dataclass
class AccountInfo:
    external_account_id: str = ""
    display_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishContext:
    """Everything a provider may use during one publish attempt."""

    request: PublishRequest
    clip_path: Path
    clip_duration_seconds: float = 0.0
    # Fresh (refreshed if needed) bearer token, resolved by the service.
    access_token: Callable[[], str] = lambda: ""
    # Fraction 0..1 + stage label ("uploading"/"finalizing"/"processing").
    # Stage "uploading" carries a real 0..1 fraction; "finalizing" (bytes
    # sent, provider finish in flight) and "processing" are indeterminate
    # (fraction ignored, UI shows spinner + message). "uploaded" must only
    # be emitted after the provider returns a remote id (via on_uploaded),
    # never merely because local bytes hit 100%.
    on_progress: Callable[[float, str], None] = lambda _f, _s: None
    # Called the moment the provider accepts the bytes and returns an id.
    # The service persists remote_media_id/url IMMEDIATELY so a later
    # timeout/status-check failure becomes RECONCILING, never a silent
    # duplicate on retry. Default no-op keeps old fakes working.
    on_uploaded: Callable[[str, str], None] = lambda _i, _u: None
    should_cancel: Callable[[], bool] = lambda: False
    account_meta: dict[str, Any] = field(default_factory=dict)

    def check_cancelled(self) -> None:
        if self.should_cancel():
            from ..states import SocialError

            raise SocialError("publish cancelled by user", code="cancelled",
                              user_message="Publikasi dibatalkan.")


class SocialPublisher(ABC):
    """One official platform API. Capability flags are HONEST — the UI
    and service gate behavior on them."""

    provider: str = ""
    capabilities: ProviderCapabilities = ProviderCapabilities()

    # -- accounts --------------------------------------------------------

    @abstractmethod
    def account_info(self, access_token: str) -> AccountInfo:
        """Who authorized? Used right after OAuth to label the account."""

    @abstractmethod
    def validate_account(self, access_token: str, account_meta: dict) -> None:
        """Raise AccountRequirementError when the account cannot publish
        (wrong account type, missing Page link, ...)."""

    # -- publishing ------------------------------------------------------

    @abstractmethod
    def publish(self, ctx: PublishContext) -> PublishResult:
        """Upload (and publish/draft/schedule) one clip file."""

    def get_publish_status(self, access_token: str, remote_media_id: str) -> str:
        """Poll a remote id -> publish state. Default: unsupported."""
        from ..states import SocialError

        raise SocialError("status polling not supported",
                          code="unsupported",
                          user_message="Platform ini tidak mendukung pelacakan status.")

    def cancel_publish(self, access_token: str, remote_media_id: str) -> None:
        """Best-effort remote cancel. Default: no-op (local state flips)."""


def validate_clip_file(clip_path: Path, *, max_bytes: int,
                       min_duration: float = 0.5) -> dict[str, Any]:
    """Pre-upload QC on the FINAL GAEClip artifact (spec §26-27).

    Reuses media.verify_clip (exists, size, video+audio streams); adds the
    provider size cap and a resolution sanity check. No re-encode here.
    Raises MediaError with a user-facing message when unpublishable.
    """
    from ..states import MediaError
    from ...errors import ExternalCommandError
    from ...media import probe as _probe
    from ...media import verify_clip as _verify

    if not clip_path.is_file():
        raise MediaError(f"clip file missing: {clip_path.name}")
    try:
        size = clip_path.stat().st_size
    except OSError as exc:
        raise MediaError(f"clip file unreadable: {exc}") from exc
    if size > max_bytes:
        raise MediaError(
            f"clip file too large ({size / 1048576:.1f} MiB, limit {max_bytes / 1048576:.0f} MiB)")
    try:
        info = dict(_verify(clip_path))
    except ExternalCommandError as exc:
        raise MediaError(str(exc)) from exc
    try:
        meta = _probe(clip_path)
        width = int(float(meta.get("width", 0) or 0))
        height = int(float(meta.get("height", 0) or 0))
        duration = float(info.get("duration", 0) or 0)
    except (ValueError, TypeError, AttributeError) as exc:
        raise MediaError(f"clip failed media probe: {exc}") from exc
    if duration < min_duration:
        raise MediaError("clip duration is too short")
    if width <= 0 or height <= 0:
        raise MediaError("clip has no video stream")
    info.update({"size_bytes": size, "duration": duration,
                 "width": width, "height": height})
    return info
