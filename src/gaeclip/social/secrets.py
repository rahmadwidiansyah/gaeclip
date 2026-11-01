"""Secret storage for social credentials.

Two backends behind one interface (spec §8/§9):

- WindowsSecretStore: OS-protected via DPAPI (existing runtime.secrets
  helpers). Ciphertext (base64) is what lands in kv; DPAPI ties it to the
  Windows user profile. Falls back to Fernet when DPAPI is unavailable —
  still encrypted, never plaintext.
- ServerSecretStore: Fernet via app_settings (SETTINGS_SECRET-derived key),
  same mechanism as Gemini provider keys.

SQLite/Redis store ONLY metadata (provider, account_id, display name,
token_reference, expiry, status). Tokens never appear in logs, errors,
or job records.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class SecretStore:
    """token_reference <-> credential JSON (access/refresh/expiry/scopes)."""

    kind = "base"

    def put(self, token_reference: str, credentials: dict[str, Any]) -> None:
        raise NotImplementedError

    def get(self, token_reference: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def delete(self, token_reference: str) -> None:
        raise NotImplementedError

    @staticmethod
    def new_reference(provider: str) -> str:
        import uuid

        return f"social_secret_{provider}_{uuid.uuid4().hex[:12]}"


class ServerSecretStore(SecretStore):
    """Fernet envelope (SETTINGS_SECRET-derived). Same trust model as the
    Gemini provider pool: whoever holds the deployment secret (or the key
    file in STORAGE_ROOT) can decrypt. Appropriate for server/Docker."""

    kind = "server"

    def __init__(self, client: Any, prefix: str = "gaeclip"):
        self.client = client
        self.prefix = prefix

    def _key(self, token_reference: str) -> str:
        return f"{self.prefix}:social:secret:{token_reference}"

    def put(self, token_reference: str, credentials: dict[str, Any]) -> None:
        import json

        from ..app_settings import encrypt_secret

        self.client.set(self._key(token_reference),
                        encrypt_secret(json.dumps(credentials)))

    def get(self, token_reference: str) -> dict[str, Any] | None:
        import json

        from ..app_settings import decrypt_secret

        raw = self.client.get(self._key(token_reference))
        if raw is None:
            return None
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        try:
            return json.loads(decrypt_secret(text))
        except Exception:
            logger.warning("[SOCIAL_AUTH] sealed credential unreadable ref=%s",
                           _safe_ref(token_reference))
            return None

    def delete(self, token_reference: str) -> None:
        self.client.delete(self._key(token_reference))


class WindowsSecretStore(SecretStore):
    """DPAPI envelope. Ciphertext is stored in kv (base64) so the existing
    stores work unchanged; only this Windows user profile can decrypt it."""

    kind = "windows"

    def __init__(self, client: Any, prefix: str = "gaeclip",
                 fallback: SecretStore | None = None):
        self.client = client
        self.prefix = prefix
        self.fallback = fallback

    def _key(self, token_reference: str) -> str:
        return f"{self.prefix}:social:secret:{token_reference}"

    def _dpapi(self) -> tuple[Any, Any] | None:
        try:
            from ..runtime.secrets import _dpapi_protect, _dpapi_unprotect
        except Exception:
            return None
        import sys

        if sys.platform != "win32":
            return None
        return _dpapi_protect, _dpapi_unprotect

    def put(self, token_reference: str, credentials: dict[str, Any]) -> None:
        import json

        helpers = self._dpapi()
        if helpers is not None:
            protect, _ = helpers
            blob = protect(json.dumps(credentials).encode("utf-8"))
            if blob is not None:
                envelope = "DPAPI:" + base64.b64encode(blob).decode("ascii")
                self.client.set(self._key(token_reference), envelope)
                return
        if self.fallback is not None:
            logger.info("[SOCIAL_AUTH] DPAPI unavailable, Fernet fallback ref=%s",
                        _safe_ref(token_reference))
            self.fallback.put(token_reference, credentials)
            return
        raise RuntimeError("no usable secret backend (DPAPI unavailable, no fallback)")

    def get(self, token_reference: str) -> dict[str, Any] | None:
        import json

        raw = self.client.get(self._key(token_reference))
        if raw is None:
            return None
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        if text.startswith("DPAPI:"):
            helpers = self._dpapi()
            if helpers is None:
                return None
            _, unprotect = helpers
            try:
                plain = unprotect(base64.b64decode(text[len("DPAPI:"):]))
            except Exception:
                return None
            if plain is None:
                return None
            try:
                return json.loads(plain.decode("utf-8"))
            except ValueError:
                return None
        if self.fallback is not None:
            return self.fallback.get(token_reference)
        return None

    def delete(self, token_reference: str) -> None:
        self.client.delete(self._key(token_reference))
        if self.fallback is not None:
            try:
                self.fallback.delete(token_reference)
            except Exception:
                pass


def _safe_ref(token_reference: str) -> str:
    """Log-safe handle: suffix only, no credential material."""
    return (token_reference or "")[-6:] or "?"


def make_secret_store(client: Any, runtime: str = "server") -> SecretStore:
    """Factory honoring deployment parity (§41): desktop prefers DPAPI with
    Fernet fallback; server uses Fernet directly."""
    server = ServerSecretStore(client)
    if runtime == "desktop":
        return WindowsSecretStore(client, fallback=server)
    return server


def credentials_expired(credentials: dict[str, Any], skew_seconds: float = 60) -> bool:
    """True when a stored access token should be refreshed first."""
    expires_at = credentials.get("expires_at")
    if not expires_at:
        return False
    try:
        return float(expires_at) <= time.time() + skew_seconds
    except (TypeError, ValueError):
        return True
