"""Secrets handling per edition. Server behavior is untouched.

Server: SETTINGS_SECRET env (or generated key file) exactly as today.
Desktop: the process secret is DPAPI-protected at rest via Windows
Credential Manager semantics (see below) and injected as SETTINGS_SECRET
for the process, so ``app_settings`` encryption works unchanged.

DPAPI is accessed via ctypes (no new dependency). Outside Windows the
helpers transparently fall back to the existing key-file mechanism.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_NAME = "Gaeclip"
SECRET_NAME = "settings-secret"


def _dpapi_protect(data: bytes) -> bytes | None:
    """Windows DPAPI protect (user scope). None when unavailable."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        buffer_in = ctypes.create_string_buffer(data)
        blob_in = DATA_BLOB(len(data), ctypes.cast(buffer_in, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        if not crypt32.CryptProtectData(
                ctypes.byref(blob_in), None, None, None, None, 0x01,
                ctypes.byref(blob_out)):
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            # LocalFree lives in kernel32, NOT crypt32 (using crypt32
            # raises AttributeError and silently disables DPAPI).
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        logger.warning("DPAPI protect unavailable, using key file", exc_info=True)
        return None


def _dpapi_unprotect(data: bytes) -> bytes | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        buffer_in = ctypes.create_string_buffer(data)
        blob_in = DATA_BLOB(len(data), ctypes.cast(buffer_in, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        if not crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0x01,
                ctypes.byref(blob_out)):
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            # LocalFree lives in kernel32, NOT crypt32 (using crypt32
            # raises AttributeError and silently disables DPAPI).
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        logger.warning("DPAPI unprotect unavailable", exc_info=True)
        return None


def _secret_file() -> Path:
    root = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(root) / "Gaeclip" / ".settings_secret"


def ensure_desktop_secret() -> str:
    """Make SETTINGS_SECRET available for a desktop process.

    Precedence: explicit env (power users) -> DPAPI blob -> legacy key
    file -> generate fresh (DPAPI-protect when possible). Returns the
    secret and exports it into the process environment.
    """
    direct = os.getenv("SETTINGS_SECRET", "").strip()
    if direct:
        return direct
    secret_file = _secret_file()
    if secret_file.is_file():
        try:
            raw = secret_file.read_bytes()
            if raw.startswith(b"DPAPI:"):
                plain = _dpapi_unprotect(raw[len(b"DPAPI:"):])
                if plain is not None:
                    os.environ["SETTINGS_SECRET"] = base64.urlsafe_b64encode(plain).decode()
                    return os.environ["SETTINGS_SECRET"]
            elif len(raw) == 32:
                # Legacy plain key file: adopt it as-is (server-compatible).
                os.environ["SETTINGS_SECRET"] = base64.urlsafe_b64encode(raw).decode()
                return os.environ["SETTINGS_SECRET"]
        except OSError:
            pass
    import secrets as _secrets

    fresh = _secrets.token_bytes(32)
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        protected = _dpapi_protect(fresh)
        secret_file.write_bytes(b"DPAPI:" + protected if protected is not None else fresh)
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
    except OSError:
        logger.warning("Could not persist desktop secret; using ephemeral key")
        return base64.urlsafe_b64encode(fresh).decode()
    # NOTE: app_settings hashes SETTINGS_SECRET with sha256, so the raw
    # 32 bytes must round-trip through base64 to stay stable.
    os.environ["SETTINGS_SECRET"] = base64.urlsafe_b64encode(fresh).decode()
    return os.environ["SETTINGS_SECRET"]
