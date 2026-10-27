"""Automatic per-clip publication metadata (description + hashtags).

Runs inside the clip pipeline, once per successfully rendered clip, using
the exact same provider machinery as the manual Publish generator
(`social.metadata`: pool-first, then legacy key, then deterministic local
fallback). One structured LLM request yields the whole package; the result
is stored on the clip record so the Publish form reads it without calling
the LLM again.

Guarantees (spec §5-§6):

- Never raises for metadata problems (except ``CancelledError``, which
  always propagates so cancellation stays prompt). The worst case is the
  deterministic local fallback — rendering is never blocked or lost.
- No network-or-key required: with no providers configured the fallback
  path is taken immediately without any LLM attempt.
- No secrets, keys, or full transcripts in logs (only provider/model,
  latency, level, and sanitized error codes).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

METADATA_SOURCE_LLM = "llm"
METADATA_SOURCE_FALLBACK = "deterministic_fallback"


@dataclass
class ClipMetadataContext:
    """Everything the generator may use. Only whitelisted fields reach LLM."""

    job_id: str
    clip_id: str
    source_title: str = ""
    source_channel: str = ""
    clip_title: str = ""
    clip_reason: str = ""
    clip_duration: float | None = None
    language: str = "id"
    span_text: str = ""


def _line_field(line: Any, name: str, default: Any = "") -> Any:
    if isinstance(line, dict):
        return line.get(name, default)
    return getattr(line, name, default)


def slice_span_text(lines: list[Any], start: float | None,
                     end: float | None) -> tuple[str, str]:
    """(span_text, language) from TranscriptLine objects (or dicts)."""
    from .social.metadata import _span_text

    dicts = [
        {"text": _line_field(line, "text", ""),
         "start": _line_field(line, "start", 0),
         "end": _line_field(line, "end", _line_field(line, "start", 0)),
         "language": _line_field(line, "language", None)}
        for line in (lines or [])
    ]
    return _span_text(dicts, start, end)


def generate_clip_metadata_safe(
    ctx: ClipMetadataContext,
    *,
    client: Any,
    settings: Any,
    ai_chain: list[str] | None,
) -> dict[str, Any]:
    """Like :func:`generate_clip_metadata` but never raises for metadata
    problems (only ``CancelledError`` propagates). Ultimate failure yields
    ``source="failed"`` with empty fields so the clip still completes."""
    from .errors import CancelledError

    try:
        return generate_clip_metadata(
            ctx, client=client, settings=settings, ai_chain=ai_chain)
    except CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — render must survive
        logger.info("[CLIP_METADATA] failed job=%s clip=%s (%s); storing empty package",
                    ctx.job_id, ctx.clip_id, type(exc).__name__)
        return {
            "description": "", "hashtags": [], "source": "failed",
            "provider": None, "model": None, "updated_at": time.time(),
        }


def generate_clip_metadata(
    ctx: ClipMetadataContext,
    *,
    client: Any,
    settings: Any,
    ai_chain: list[str] | None,
) -> dict[str, Any]:
    """Generate ``{description, hashtags, source, provider, model, updated_at}``.

    Single LLM request for both fields (structured JSON, validated). Any
    provider/timeout/validation failure resolves to the deterministic local
    fallback. ``CancelledError`` is never swallowed.
    """
    from .errors import CancelledError
    from .social.metadata import (
        DESCRIPTION_MAX_CHARS,
        _generate_via_gemini,
        _normalize_hashtags,
        _parse_metadata_json,
        build_metadata_prompt,
        deterministic_metadata,
    )
    from .social.states import SocialError

    t0 = time.time()
    context = {
        "source_title": ctx.source_title,
        "source_channel": ctx.source_channel,
        "clip_title": ctx.clip_title,
        "clip_reason": ctx.clip_reason,
        "clip_duration": f"{ctx.clip_duration}s" if ctx.clip_duration else "",
        "language": ctx.language or "id",
        "transcript": ctx.span_text,
        "user_title": "",
    }
    prompt = build_metadata_prompt(context)

    source = METADATA_SOURCE_FALLBACK
    provider_id: str | None = None
    model: str | None = None
    fresh: dict[str, Any] | None = None
    try:
        raw, provider_id, model = _generate_via_gemini(
            prompt, client=client, settings=settings,
            job_id=ctx.job_id, ai_chain=ai_chain)
        parsed = _parse_metadata_json(raw)
        # Title already exists from curation; keep it as the anchor and only
        # require description + hashtags from this response.
        parsed["description"] = re.sub(r"\s+", " ", parsed["description"]).strip()
        if len(parsed["description"]) > DESCRIPTION_MAX_CHARS:
            parsed["description"] = parsed["description"][:DESCRIPTION_MAX_CHARS].rsplit(" ", 1)[0] + "…"
        parsed["hashtags"] = _normalize_hashtags(parsed["hashtags"])
        if not parsed["description"] or not parsed["hashtags"]:
            raise SocialError("metadata fields unusable", code="invalid_response",
                              user_message="Respons AI tidak valid.")
        fresh = parsed
        source = METADATA_SOURCE_LLM
        logger.info("[CLIP_METADATA] level=llm ok job=%s clip=%s provider=%s model=%s ms=%.0f",
                    ctx.job_id, ctx.clip_id, provider_id, model, (time.time() - t0) * 1000)
    except SocialError as exc:
        logger.info("[CLIP_METADATA] llm unavailable (%s); deterministic fallback job=%s clip=%s",
                    getattr(exc, "code", "error"), ctx.job_id, ctx.clip_id)
    except CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — fallback must cover everything
        logger.info("[CLIP_METADATA] llm failed (%s); deterministic fallback job=%s clip=%s",
                    type(exc).__name__, ctx.job_id, ctx.clip_id)

    if fresh is None:
        det = deterministic_metadata(
            ctx.clip_title, ctx.clip_reason, ctx.span_text,
            ctx.language or "id", ctx.source_title)
        fresh = {"description": det["description"], "hashtags": det["hashtags"]}
        provider_id, model = None, None

    # Ultimate guard: even a broken fallback helper must not fail the clip.
    description = str(fresh.get("description", "") or "")
    hashtags = [h for h in (fresh.get("hashtags") or []) if isinstance(h, str)][:8]
    if not description:
        description = ctx.clip_title or ctx.source_title or "Cuplikan video."
        source = METADATA_SOURCE_FALLBACK
        provider_id, model = None, None
    if not hashtags:
        hashtags = ["#shorts"]
        if source == METADATA_SOURCE_LLM:
            source = METADATA_SOURCE_FALLBACK
            provider_id, model = None, None

    return {
        "description": description,
        "hashtags": hashtags,
        "source": source,
        "provider": provider_id,
        "model": model,
        "updated_at": time.time(),
    }
