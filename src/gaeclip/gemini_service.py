"""Gemini client lifecycle done correctly.

Root cause of the old "Cannot send a request, as the client has been
closed" bug: call sites used a chained temporary::

    genai.Client(api_key=...).models.generate_content(...)

CPython garbage-collects the parent ``Client`` as soon as ``.models`` is
evaluated (``Models`` only retains the inner API client). The collector
runs ``BaseApiClient.__del__ -> close()``, shutting the shared httpx
client *before* the request is sent.

Rules enforced here:

- The client object is always bound to a local variable that outlives the
  whole request (correctness over premature optimization).
- One fresh client per call; explicitly closed in ``finally`` after the
  response is fully materialized. Never cached, never shared, never used
  after close.
- The API key never appears in messages, logs, or responses.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeminiTestResult:
    ok: bool
    model: str
    latency_ms: int | None = None
    error_code: str | None = None
    message: str = ""


def classify_error(exc: BaseException) -> tuple[str, str]:
    """Map raw SDK/transport errors to (error_code, safe_message)."""
    text = str(exc)
    lowered = text.lower()
    code = getattr(exc, "code", None)
    try:
        status = int(code) if code is not None else None
    except (TypeError, ValueError):
        status = None

    if isinstance(exc, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        return "timeout", "Permintaan Gemini melewati batas waktu"
    if "has been closed" in lowered or "closed" in lowered and "client" in lowered:
        return "client_lifecycle_error", "Lifecycle client Gemini bermasalah"
    if status == 429 or "429" in lowered or "rate limit" in lowered or "quota" in lowered or "resource exhausted" in lowered:
        return "rate_limit", "Batas permintaan Gemini terlampaui"
    if status in (401, 403) or "401" in lowered or "403" in lowered or any(
        word in lowered for word in (
            "unauthorized", "unauthenticated", "permission denied",
            "api key", "api_key", "apikey", "forbidden", "invalid_argument",
        )
    ):
        # Distinguish bad key from forbidden project/model access below.
        if "api_key_invalid" in lowered or "api key not valid" in lowered:
            return "invalid_api_key", "Autentikasi Gemini gagal: API key tidak valid"
        return "authentication_error", "Autentikasi Gemini gagal"
    if status == 404 or "not found" in lowered or "not supported" in lowered:
        return "model_not_found", "Model Gemini tidak tersedia"
    if any(word in lowered for word in ("connection", "network", "dns", "unreachable", "failed to resolve", "connecterror")):
        return "network_error", "Gangguan jaringan ke Gemini"
    if "json" in lowered and "invalid" in lowered:
        return "invalid_response", "Gemini mengembalikan respons yang tidak valid"
    return "unknown_error", "Koneksi Gemini gagal"


def _sanitize(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret and len(secret) > 8 and secret in text:
            text = text.replace(secret, "[redacted]")
    return text


def build_client(api_key: str, timeout_seconds: int = 30) -> Any:
    """Create a fresh client. Caller owns its lifecycle (use + close)."""
    from google import genai

    return genai.Client(
        api_key=api_key,
        http_options={"timeout": int(timeout_seconds) * 1000},
    )


def close_client(client: Any) -> None:
    try:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    except Exception:
        pass


def generate_content(
    api_key: str,
    model: str,
    contents: Any,
    timeout_seconds: int = 30,
    max_output_tokens: int | None = None,
    temperature: float = 0.2,
    response_mime_type: str = "application/json",
) -> Any:
    """Generate content with a strictly scoped client.

    Same lifecycle contract as test_connection: the client lives in this
    frame for the whole call and is closed only after the response is
    materialized. Used by AI curation so workers never reuse a dead client.
    ``max_output_tokens=None`` preserves the previous behavior (no cap).
    """
    if not (api_key or "").strip():
        raise ValueError("No Gemini API key configured")
    config: dict[str, Any] = {
        "response_mime_type": response_mime_type,
        "temperature": temperature,
    }
    if max_output_tokens is not None:
        config["max_output_tokens"] = max_output_tokens
    client = build_client(api_key.strip(), timeout_seconds)
    try:
        return client.models.generate_content(
            model=model.strip(),
            contents=contents,
            config=config,
        )
    finally:
        close_client(client)


def test_connection(
    api_key: str,
    model: str,
    timeout_seconds: int = 30,
) -> GeminiTestResult:
    """Lightweight credential+model check with a strictly scoped client."""
    if not (api_key or "").strip():
        return GeminiTestResult(
            ok=False, model=model or "",
            error_code="missing_api_key", message="API Key Gemini belum dikonfigurasi",
        )
    if not (model or "").strip():
        return GeminiTestResult(
            ok=False, model="", error_code="model_not_found",
            message="Model Gemini belum dikonfigurasi",
        )
    client = build_client(api_key.strip(), timeout_seconds)
    started = time.monotonic()
    try:
        response = client.models.generate_content(
            model=model.strip(),
            contents="Reply with exactly: ok",
            config={"max_output_tokens": 8, "temperature": 0},
        )
        _ = getattr(response, "text", "")
        latency_ms = int((time.monotonic() - started) * 1000)
        logger.info("Gemini connection test ok: model=%s latency_ms=%d", model, latency_ms)
        return GeminiTestResult(
            ok=True, model=model.strip(), latency_ms=latency_ms,
            message="Koneksi Gemini berhasil",
        )
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        safe = _sanitize(str(exc), [api_key])[:300]
        error_code, message = classify_error(exc)
        logger.warning("Gemini connection test failed: error_code=%s latency_ms=%d detail=%s",
                       error_code, latency_ms, safe)
        return GeminiTestResult(
            ok=False, model=model.strip(), latency_ms=latency_ms,
            error_code=error_code, message=message,
        )
    finally:
        close_client(client)
