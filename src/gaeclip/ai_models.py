"""Gemini model discovery, classification, registry, and execution chain.

Architecture::

    GOOGLE GEMINI API (GET /v1beta/models, server-side, user API key)
      -> MODEL DISCOVERY (paginated, defensive)
      -> VALIDATION + NORMALIZATION (ModelInfo; only API metadata, never invented)
      -> AUTOMATIC CLASSIFICATION (capabilities first, names as fallback)
      -> MODEL REGISTRY (shared cache, TTL, last-known-good)
      -> FREE-TIER POLICY (separate from availability)
      -> USER SETTINGS (mode/primary/fallbacks, validated atomically)
      -> AI REQUEST (primary + retry -> fallbacks + retry -> heuristic last)

Nothing here imports the google SDK: discovery uses stdlib ``urllib`` against
the REST API so it works even where ``google-genai`` is not installed.
The API key is used server-side only and never logged, stored, or returned.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field

from .errors import ConfigurationError, ValidationError

logger = logging.getLogger(__name__)

MODELS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_CACHE_TTL_SECONDS = 6 * 3600
REGISTRY_HASH = "gaeclip:ai:model_registry"
MAX_DISCOVERY_PAGES = 10
PAGE_SIZE = 100

# ---------------------------------------------------------------------------
# ModelInfo
# ---------------------------------------------------------------------------

TEXT_GENERATION = "text_generation"
MULTIMODAL = "multimodal"
EMBEDDING = "embedding"
IMAGE_GENERATION = "image_generation"
TEXT_TO_SPEECH = "text_to_speech"
LIVE = "live"
OTHER = "other"
UNSUPPORTED = "unsupported"

ELIGIBLE_CATEGORIES = frozenset({TEXT_GENERATION, MULTIMODAL})
ALWAYS_UNSUPPORTED = frozenset({EMBEDDING, IMAGE_GENERATION, TEXT_TO_SPEECH, LIVE})

STABLE = "stable"
PREVIEW = "preview"
EXPERIMENTAL = "experimental"
LATEST_ALIAS = "latest_alias"
UNKNOWN_CHANNEL = "unknown"

FREE = "free"
PAID = "paid"
PRICING_UNKNOWN = "unknown"


@dataclass
class ModelInfo:
    """Normalized view of one discovered model. All capability fields come
    from Google API metadata; only classification/policy derivations are ours."""

    id: str
    display_name: str
    provider: str = "gemini"
    available: bool = True
    supports_generate_content: bool = False
    family: str = "gemini"
    generation: str = ""
    variant: str = "other"
    tier: str = "other"
    release_channel: str = UNKNOWN_CHANNEL
    is_preview: bool = False
    is_experimental: bool = False
    is_latest_alias: bool = False
    category: str = OTHER
    free_tier: str = PRICING_UNKNOWN
    free_tier_source: str = "local_policy"
    eligible_for_gaeclip: bool = False
    input_token_limit: int | None = None
    output_token_limit: int | None = None
    version: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "ModelInfo":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})


# ---------------------------------------------------------------------------
# Discovery (REST, paginated, defensive)
# ---------------------------------------------------------------------------

class DiscoveryError(Exception):
    """Discovery failed. ``kind`` is auth|transient|invalid (never raw HTTP)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _redact_params(params: dict) -> dict:
    return {k: ("[redacted]" if "key" in k.lower() else v) for k, v in params.items()}


def _fetch_models_page(api_key: str, page_token: str | None, timeout: int) -> dict:
    query = {"pageSize": str(PAGE_SIZE)}
    if page_token:
        query["pageToken"] = page_token
    url = MODELS_ENDPOINT + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={"x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise DiscoveryError("auth", "Gemini authentication failed") from exc
        if exc.code == 404:
            raise DiscoveryError("invalid", "Gemini models endpoint not found") from exc
        if exc.code in (408, 429, 500, 502, 503, 504):
            raise DiscoveryError("transient", f"Gemini models API transient error ({exc.code})") from exc
        raise DiscoveryError("invalid", f"Gemini models API error ({exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DiscoveryError("transient", "Gemini models API unreachable") from exc


def discover_raw_models(api_key: str, timeout: int = 30) -> list[dict]:
    """Call Google Models API with the configured key; follow pages."""
    if not (api_key or "").strip():
        raise DiscoveryError("auth", "No Gemini API key configured")
    models: list[dict] = []
    page_token: str | None = None
    for _ in range(MAX_DISCOVERY_PAGES):
        page = _fetch_models_page(api_key.strip(), page_token, timeout)
        batch = page.get("models", []) or []
        if not isinstance(batch, list):
            raise DiscoveryError("invalid", "Unexpected models API response")
        models.extend(m for m in batch if isinstance(m, dict))
        page_token = page.get("nextPageToken") or None
        if not page_token:
            break
    return models


# ---------------------------------------------------------------------------
# Normalization + classification (metadata first, names as fallback)
# ---------------------------------------------------------------------------

def _model_id(raw: dict) -> str:
    name = str(raw.get("name") or "")
    return name.split("/", 1)[1] if "/" in name else name


def _parse_generation(model_id: str) -> str:
    """'gemini-3.8-flash' -> '3.8'. Unparseable -> '' (never guessed)."""
    import re

    match = re.search(r"gemini-(\d+(?:\.\d+)?)", model_id)
    return match.group(1) if match else ""


def _variant(model_id: str) -> str:
    lowered = model_id.lower()
    if "flash-lite" in lowered or "flash_light" in lowered or lowered.endswith("-lite"):
        return "flash-lite"
    if "flash" in lowered:
        return "flash"
    if "pro" in lowered:
        return "pro"
    if "ultra" in lowered:
        return "ultra"
    return "other"


def generation_tuple(generation: str) -> tuple[int, ...]:
    """Semantic-ish compare so 3.10 > 3.9 (never naive lexical sort)."""
    parts: list[int] = []
    for piece in generation.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            break
    return tuple(parts) if parts else (0,)


def release_channel(model_id: str) -> tuple[str, bool, bool, bool]:
    """(channel, is_preview, is_experimental, is_latest_alias)."""
    lowered = model_id.lower()
    is_latest = lowered.endswith("-latest") or lowered == "latest" or "-latest-" in lowered
    is_preview = "preview" in lowered
    is_experimental = "experimental" in lowered or "-exp" in lowered
    if is_experimental:
        return EXPERIMENTAL, is_preview, True, is_latest
    if is_preview:
        return PREVIEW, True, False, is_latest
    if is_latest:
        return LATEST_ALIAS, False, False, True
    import re

    if re.search(r"gemini-\d+(\.\d+)?-", lowered):
        return STABLE, False, False, False
    return UNKNOWN_CHANNEL, False, False, False


def classify_category(model_id: str, methods: list[str]) -> str:
    """Capabilities first; naming only as fallback; UNKNOWN-ish -> OTHER."""
    normalized = {m.lower().replace("_", "") for m in methods}
    lowered = model_id.lower()
    if any("embed" in m or "embed" in lowered for m in normalized) or "embedding" in lowered:
        return EMBEDDING
    if "imagen" in lowered or "image" in lowered and "generatecontent" not in normalized:
        return IMAGE_GENERATION
    if "tts" in lowered or "text-to-speech" in lowered or "speech" in lowered and "generatecontent" not in normalized:
        return TEXT_TO_SPEECH
    if "live" in lowered and "generatecontent" not in normalized:
        return LIVE
    if "generatecontent" in normalized:
        # Every current Gemini generateContent model is multimodal; the API
        # exposes no finer modality flag, so family+capability decides.
        return MULTIMODAL if lowered.startswith("gemini-") or lowered.startswith("gemini") else TEXT_GENERATION
    if not methods:
        # No capability metadata: naming fallback, conservative.
        if any(k in lowered for k in ("embed", "imagen", "tts", "live")):
            return UNSUPPORTED
        if any(k in lowered for k in ("flash", "pro", "ultra", "nano", "gemini", "gpt", "Muse")):
            return TEXT_GENERATION
        return OTHER
    return OTHER


def free_tier_status(variant: str, channel: str) -> str:
    """Local policy (source recorded). UNKNOWN unless the tier is known-free
    or known-paid; never claim free without a policy hit."""
    if channel in (PREVIEW, EXPERIMENTAL):
        return PRICING_UNKNOWN
    if variant in ("flash", "flash-lite"):
        return FREE
    if variant in ("pro", "ultra"):
        return PAID
    return PRICING_UNKNOWN


def normalize_model(raw: dict) -> ModelInfo:
    model_id = _model_id(raw)
    methods = [str(m) for m in (raw.get("supportedGenerationMethods") or [])]
    supports_gc = any(m.lower() in ("generatecontent", "generate_content") for m in methods)
    channel, is_preview, is_experimental, is_latest = release_channel(model_id)
    category = classify_category(model_id, methods)
    variant = _variant(model_id)
    generation = _parse_generation(model_id)

    def _int(value: object) -> int | None:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    info = ModelInfo(
        id=model_id,
        display_name=str(raw.get("displayName") or model_id),
        supports_generate_content=supports_gc,
        family="gemini" if model_id.lower().startswith("gemini") else "other",
        generation=generation,
        variant=variant,
        tier=variant,
        release_channel=channel,
        is_preview=is_preview,
        is_experimental=is_experimental,
        is_latest_alias=is_latest,
        category=category,
        free_tier=free_tier_status(variant, channel),
        input_token_limit=_int(raw.get("inputTokenLimit")),
        output_token_limit=_int(raw.get("outputTokenLimit")),
        version=str(raw.get("version") or generation),
        description=str(raw.get("description") or "")[:500],
    )
    info.eligible_for_gaeclip = bool(
        info.available
        and info.supports_generate_content
        and info.category in ELIGIBLE_CATEGORIES
    )
    return info


# ---------------------------------------------------------------------------
# Ranking / automatic selection (internal scores, never shown to users)
# ---------------------------------------------------------------------------

def _internal_score(info: ModelInfo, free_tier_only: bool) -> float:
    if info.category in ALWAYS_UNSUPPORTED or not info.eligible_for_gaeclip:
        return -1000.0
    if free_tier_only and info.free_tier != FREE:
        return -1000.0
    score = 0.0
    if info.release_channel == STABLE:
        score += 100.0
    elif info.release_channel == LATEST_ALIAS:
        score += 40.0
    elif info.release_channel == PREVIEW:
        score -= 20.0
    elif info.release_channel == EXPERIMENTAL:
        score -= 50.0
    if info.variant == "flash":
        score += 80.0
    elif info.variant == "flash-lite":
        score += 70.0
    elif info.variant == "pro":
        score += 60.0
    else:
        score += 40.0
    gen = generation_tuple(info.generation)
    if gen:
        # Recency tiebreak only: newer generation wins within the same band,
        # so a future Gemini 4.x Flash is preferred without hardcoding names.
        score += (gen[0] * 10 + (gen[1] if len(gen) > 1 else 0)) * 0.5
    return score


def rank_models(models: list[ModelInfo], free_tier_only: bool) -> list[ModelInfo]:
    """Eligible first, then internal score, then version-aware id order."""
    def key(info: ModelInfo) -> tuple:
        return (
            0 if (info.eligible_for_gaeclip and _internal_score(info, free_tier_only) > -1000) else 1,
            -_internal_score(info, free_tier_only),
            tuple(-p for p in generation_tuple(info.generation)),
            info.id,
        )

    return sorted(models, key=key)


def automatic_chain(models: list[ModelInfo], free_tier_only: bool, depth: int = 4) -> list[str]:
    """Primary + fallback ids from policy ranking (future-proof: no names)."""
    ranked = [m for m in rank_models(models, free_tier_only) if _internal_score(m, free_tier_only) > -1000]
    return [m.id for m in ranked[: max(1, depth)]]


# ---------------------------------------------------------------------------
# Registry (shared cache; last-known-good; per-key scoping)
# ---------------------------------------------------------------------------

def cache_ttl_seconds() -> int:
    try:
        return max(60, int(os.getenv("MODEL_DISCOVERY_CACHE_TTL", str(DEFAULT_CACHE_TTL_SECONDS))))
    except ValueError:
        return DEFAULT_CACHE_TTL_SECONDS


def key_fingerprint(api_key: str) -> str:
    return hashlib.sha256((api_key or "").encode()).hexdigest()[:16]


@dataclass
class RegistrySnapshot:
    models: list[ModelInfo] = field(default_factory=list)
    source: str = "live"  # live|cache|cache-stale|error
    discovered_at: float = 0.0
    expires_at: float = 0.0
    key_fingerprint: str = ""
    error: dict | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["models"] = [m.to_dict() for m in self.models]
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "RegistrySnapshot":
        models = [ModelInfo.from_dict(m) for m in payload.get("models", []) if isinstance(m, dict)]
        return cls(
            models=models,
            source=str(payload.get("source", "cache")),
            discovered_at=float(payload.get("discovered_at", 0) or 0),
            expires_at=float(payload.get("expires_at", 0) or 0),
            key_fingerprint=str(payload.get("key_fingerprint", "")),
            error=payload.get("error"),
        )

    def by_id(self) -> dict[str, ModelInfo]:
        return {m.id: m for m in self.models}

    def counts(self) -> dict[str, int]:
        models = self.models
        return {
            "discovered": len(models),
            "compatible": sum(1 for m in models if m.eligible_for_gaeclip),
            "free_tier": sum(1 for m in models if m.eligible_for_gaeclip and m.free_tier == FREE),
            "unsupported": sum(1 for m in models if not m.eligible_for_gaeclip),
        }


_memory_cache: dict[str, RegistrySnapshot] = {}


def _read_cache(client: object | None, fingerprint: str) -> RegistrySnapshot | None:
    if client is not None:
        try:
            raw = client.hget(REGISTRY_HASH, fingerprint)  # type: ignore[union-attr]
            if raw is None:
                return None
            text = raw.decode() if isinstance(raw, bytes) else str(raw)
            snap = RegistrySnapshot.from_dict(json.loads(text))
            snap.source = "cache"
            return snap
        except Exception:
            logger.warning("model_registry_cache_read_failed", exc_info=True)
            return None
    snap = _memory_cache.get(fingerprint)
    if snap is not None:
        return RegistrySnapshot(
            models=snap.models, source="cache", discovered_at=snap.discovered_at,
            expires_at=snap.expires_at, key_fingerprint=snap.key_fingerprint, error=snap.error,
        )
    return None


def _write_cache(client: object | None, snap: RegistrySnapshot) -> None:
    if client is not None:
        try:
            client.hset(REGISTRY_HASH, snap.key_fingerprint, json.dumps(snap.to_dict()))  # type: ignore[union-attr]
            return
        except Exception:
            logger.warning("model_registry_cache_write_failed", exc_info=True)
    _memory_cache[snap.key_fingerprint] = snap


def cached_snapshot(client: object | None, api_key: str) -> RegistrySnapshot | None:
    """Cache-only read (no network). Used by atomic save validation so a
    save never blocks on Google and never fails just because Google is down."""
    try:
        snap = _read_cache(client, key_fingerprint(api_key or ""))
    except Exception:
        return None
    if snap is None or not snap.models:
        return None
    return snap


def invalidate_registry(client: object | None, api_key: str = "") -> None:
    """Drop cached discovery (used on API key change; scoped per key)."""
    if api_key:
        fingerprint = key_fingerprint(api_key)
        try:
            if client is not None:
                client.hdel(REGISTRY_HASH, fingerprint)  # type: ignore[union-attr]
            _memory_cache.pop(fingerprint, None)
        except Exception:
            pass
        logger.info("model_registry_invalidated")
        return
    try:
        if client is not None:
            for key in client.hgetall(REGISTRY_HASH):  # type: ignore[union-attr]
                client.hdel(REGISTRY_HASH, key)  # type: ignore[union-attr]
    except Exception:
        pass
    _memory_cache.clear()
    logger.info("model_registry_invalidated")


def refresh_registry(
    client: object | None,
    api_key: str,
    timeout: int = 30,
    ttl: int | None = None,
) -> RegistrySnapshot:
    """Force discovery via Google Models API; persist on success only."""
    started = time.monotonic()
    fingerprint = key_fingerprint(api_key)
    try:
        raw = discover_raw_models(api_key, timeout)
        models = [normalize_model(item) for item in raw]
        now = time.time()
        snap = RegistrySnapshot(
            models=models,
            source="live",
            discovered_at=now,
            expires_at=now + (ttl if ttl is not None else cache_ttl_seconds()),
            key_fingerprint=fingerprint,
        )
        _write_cache(client, snap)
        counts = snap.counts()
        logger.info(
            "model_discovery_success provider=gemini discovered=%d compatible=%d "
            "free_tier=%d duration_ms=%d",
            counts["discovered"], counts["compatible"], counts["free_tier"],
            int((time.monotonic() - started) * 1000),
        )
        logger.info("model_registry_updated provider=gemini models=%d", len(models))
        return snap
    except DiscoveryError as exc:
        logger.warning("model_discovery_failed kind=%s", exc.kind)
        raise


def get_registry(
    client: object | None,
    api_key: str,
    timeout: int = 30,
    force_refresh: bool = False,
    ttl: int | None = None,
) -> RegistrySnapshot:
    """Cached registry: live when fresh, refresh when stale, last-known-good
    when Google fails. Never wipes a good cache on transient failure."""
    logger.info("model_discovery_started provider=gemini force=%s", force_refresh)
    fingerprint = key_fingerprint(api_key)
    now = time.time()
    cached = None if force_refresh else _read_cache(client, fingerprint)
    if cached is not None and cached.key_fingerprint == fingerprint and cached.expires_at > now:
        return cached
    try:
        return refresh_registry(client, api_key, timeout, ttl=ttl)
    except DiscoveryError as exc:
        stale = _read_cache(client, fingerprint)
        if stale is not None and stale.models:
            stale.source = "cache-stale"
            stale.error = {"code": "MODEL_DISCOVERY_FAILED", "kind": exc.kind}
            logger.warning("model_registry_stale_cache_used models=%d", len(stale.models))
            return stale
        return RegistrySnapshot(
            models=[], source="error", key_fingerprint=fingerprint,
            error={"code": "MODEL_DISCOVERY_FAILED", "kind": exc.kind, "message": str(exc)},
        )


# ---------------------------------------------------------------------------
# User preferences (persisted) + validation
# ---------------------------------------------------------------------------

AI_PREF_KEYS = ("AI_MODEL_MODE", "AI_PRIMARY_MODEL", "AI_FALLBACK_MODELS", "AI_FREE_TIER_ONLY")


@dataclass
class AIPrefs:
    mode: str = "automatic"  # automatic|manual
    primary_model: str = ""
    fallback_models: list[str] = field(default_factory=list)
    free_tier_only: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def parse_fallbacks(raw: object) -> list[str]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw.strip() else []
        except ValueError:
            parsed = [raw]
        items = parsed if isinstance(parsed, list) else [parsed]
    else:
        items = []
    return [str(m).strip() for m in items if str(m).strip()]


def prefs_from_service(service: object) -> AIPrefs:
    """Read user preference keys; unknown/missing -> safe defaults."""
    get = service.get  # type: ignore[union-attr]
    try:
        mode = str(get("AI_MODEL_MODE")).strip().lower()
    except Exception:
        mode = "automatic"
    try:
        primary = str(get("AI_PRIMARY_MODEL")).strip()
    except Exception:
        primary = ""
    try:
        fallbacks = parse_fallbacks(get("AI_FALLBACK_MODELS"))
    except Exception:
        fallbacks = []
    try:
        free_only = bool(get("AI_FREE_TIER_ONLY"))
    except Exception:
        free_only = True
    # Backward compat: legacy single GEMINI_MODEL acts as manual primary.
    if not primary:
        try:
            legacy = str(service.get("GEMINI_MODEL")).strip()  # type: ignore[union-attr]
            if legacy:
                primary = legacy
        except Exception:
            pass
    return AIPrefs(
        mode=mode if mode in ("automatic", "manual") else "automatic",
        primary_model=primary,
        fallback_models=fallbacks,
        free_tier_only=free_only,
    )


def normalize_chain(primary: str, fallbacks: list[str]) -> tuple[str, list[str]]:
    """Dedupe preserving order; primary wins over duplicate fallback entries."""
    seen: set[str] = set()
    clean: list[str] = []
    for model in [primary, *fallbacks]:
        name = (model or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        clean.append(name)
    if not clean:
        raise ValidationError("Model utama wajib diisi")
    return clean[0], clean[1:]


def validate_prefs(prefs: AIPrefs, snapshot: RegistrySnapshot | None) -> tuple[str, list[str], list[str]]:
    """Validate + normalize user chain. Returns (primary, fallbacks, warnings).
    Unknown models are kept but warned when no registry; unavailable registry
    models are kept and marked (never silently deleted)."""
    primary, fallbacks = normalize_chain(prefs.primary_model, prefs.fallback_models)
    warnings: list[str] = []
    if snapshot is None or not snapshot.models:
        warnings.append("Model registry unavailable; selection not validated against discovery.")
        logger.info("model_selection_changed mode=%s validated=false", prefs.mode)
        return primary, fallbacks, warnings
    by_id = snapshot.by_id()
    for model in [primary, *fallbacks]:
        info = by_id.get(model)
        if info is None or not info.available:
            warnings.append(f"Model tidak tersedia: {model}")
        elif not info.eligible_for_gaeclip:
            raise ValidationError(f"Model tidak layak untuk kurasi: {model}")
        elif prefs.free_tier_only and info.free_tier != FREE:
            raise ValidationError(f"Model tidak diizinkan dengan Hanya Free Tier: {model}")
    logger.info("model_selection_changed mode=%s primary=%s fallbacks=%d", prefs.mode, primary, len(fallbacks))
    return primary, fallbacks, warnings


def resolve_chain(prefs: AIPrefs, snapshot: RegistrySnapshot | None) -> tuple[str, list[str], str]:
    """Effective (primary, fallbacks, method). Automatic ranks from policy;
    manual validates the user chain."""
    if prefs.mode == "manual" and prefs.primary_model:
        primary, fallbacks, _ = validate_prefs(prefs, snapshot)
        return primary, fallbacks, "manual"
    if snapshot is not None and snapshot.models:
        chain = automatic_chain(snapshot.models, prefs.free_tier_only)
        if chain:
            return chain[0], chain[1:], "automatic"
    # No registry: legacy single-model behavior (backward compatible).
    legacy = prefs.primary_model
    if not legacy:
        raise ValidationError("Tidak ada model tersedia: jalankan discovery model dulu")
    return legacy, [], "legacy"


# ---------------------------------------------------------------------------
# Execution chain (retry + fallback; heuristic stays outside, last resort)
# ---------------------------------------------------------------------------

TRANSIENT_CODES = {"timeout", "rate_limit", "network_error", "client_lifecycle_error", "unknown_error"}
FATAL_CODES = {"invalid_api_key", "authentication_error"}


def error_code_of(exc: BaseException) -> tuple[str, bool, bool]:
    """(code, retryable, fatal). 5xx/408/429/timeout/network retry with
    backoff; 401/403/invalid-key abort the whole chain; 404 skips the model."""
    from .gemini_service import classify_error

    code, _ = classify_error(exc)
    text = str(exc).lower()
    status = getattr(exc, "code", None)
    try:
        status_num = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_num = None
    if status_num in (401, 403) or code in FATAL_CODES:
        return code, False, True
    if status_num == 404 or code == "model_not_found":
        return code, False, False
    if status_num in (408, 429, 500, 502, 503, 504) or code in TRANSIENT_CODES:
        return code, True, False
    return code, False, False


class AIChainExhausted(Exception):
    def __init__(self, failures: list[dict], last_error: BaseException | None = None):
        super().__init__("All Gemini models in the chain failed")
        self.failures = failures
        self.last_error = last_error


def backoff_delays(max_retries: int, base: float = 1.0) -> list[float]:
    return [base * (2**attempt) + random.uniform(0, base * 0.5) for attempt in range(max_retries)]


def run_ai_chain(
    models: list[str],
    request_fn,
    max_retries: int = 3,
    base_delay: float = 1.0,
    sleep_fn=time.sleep,
    skip_unavailable: set[str] | None = None,
) -> tuple[object, dict]:
    """Execute primary -> retry -> fallback -> retry. Returns (response, meta)
    with provider/model_used/selection_method/fallback_index/attempt.
    Raises AIChainExhausted (caller falls back to heatmap/heuristic) or the
    fatal error itself for auth/config problems."""
    unavailable = skip_unavailable or set()
    failures: list[dict] = []
    attempts_total = 0
    last_error: BaseException | None = None
    for index, model in enumerate(models):
        if model in unavailable:
            logger.info("model_execution_skip model=%s reason=known_unavailable", model)
            failures.append({"model": model, "code": "model_unavailable", "skipped": True})
            continue
        logger.info(
            "model_execution_started provider=gemini model=%s fallback_index=%d", model, index,
        )
        for attempt in range(max_retries + 1):
            attempts_total += 1
            try:
                response = request_fn(model, attempt)
                method = "gemini_primary" if index == 0 else "gemini_fallback"
                logger.info(
                    "model_execution_success provider=gemini model=%s method=%s attempt=%d",
                    model, method, attempt,
                )
                return response, {
                    "provider": "gemini",
                    "model_used": model,
                    "selection_method": method,
                    "fallback_index": index,
                    "attempt": attempt,
                    "attempts_total": attempts_total,
                }
            except Exception as exc:
                code, retryable, fatal = error_code_of(exc)
                last_error = exc
                if fatal:
                    logger.warning("model_execution_fatal model=%s code=%s", model, code)
                    raise
                failures.append({"model": model, "code": code, "attempt": attempt})
                if retryable and attempt < max_retries:
                    delay = backoff_delays(max_retries, base_delay)[attempt]
                    logger.info("model_execution_retry model=%s attempt=%d delay_s=%.2f", model, attempt, delay)
                    sleep_fn(delay)
                    continue
                logger.info("model_execution_fallback model=%s code=%s", model, code)
                break
    raise AIChainExhausted(failures, last_error)


def describe_selection(prefs: AIPrefs, snapshot: RegistrySnapshot | None) -> dict:
    """Effective chain + per-model availability for the Settings UI."""
    try:
        primary, fallbacks, method = resolve_chain(prefs, snapshot)
    except ValidationError as exc:
        return {"mode": prefs.mode, "primary": None, "fallbacks": [],
                "method": "none", "error": str(exc), "models": []}
    by_id = snapshot.by_id() if snapshot is not None else {}
    chain = [primary, *fallbacks]

    def entry(model: str) -> dict:
        info = by_id.get(model)
        if info is None:
            return {"id": model, "available": None, "eligible": None,
                    "warning": "Not in discovery registry"}
        return {"id": model, "available": info.available,
                "eligible": info.eligible_for_gaeclip,
                "warning": None if info.available else "Unavailable"}

    return {"mode": prefs.mode, "primary": primary, "fallbacks": fallbacks,
            "method": method, "models": [entry(m) for m in chain]}
