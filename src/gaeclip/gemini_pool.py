"""Gemini multi-provider pool: resilient routing across API keys x models.

Architecture::

    GeminiService.generate(task_type, prompt, options)
      -> GeminiProviderPool.execute(...)
        -> Scheduler (priority | round_robin | health_first)
        -> per (provider, model): Attempt -> ErrorClassifier ->
           RETRY_SAME (bounded backoff) | NEXT_MODEL | NEXT_PROVIDER |
           PERMANENT (fatal/config/cancelled, no retry)
        -> HealthManager (cooldown, per-pair circuit breaker, probe)
        -> global attempt budget -> PoolExhausted -> local heuristic fallback

Principles (spec binding):

- NEVER RETRY FOREVER: every loop is bounded (per-pair retries, global budget).
- NEVER HIDE FAILURES: structured [GEMINI_*] logs + failure history/report.
- NEVER EXPOSE API KEYS: secrets stay server-side; UI sees masked suffix only.
- A provider is NOT just a key: id/name/enabled/priority/models/health.
- Multiple keys are REDUNDANCY, never promised quota multiplication.
- Model priority is user-configured; discovery only supplies candidates
  (never auto-picks "newest" by string, never infers free tier).
- Existing local/heuristic fallback semantics are unchanged: the pool raises
  PoolExhausted (carrying failures + last error) and callers fall back.

Health runtime state lives in Redis (shared by API + worker processes);
per-process semaphores bound in-flight requests per provider locally, with
a coarse Redis counter as the cross-process backstop.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Task types (§12). Uniform execution today; per-task tuning is an
# explicit extension point (options travel in RequestContext).
# ---------------------------------------------------------------------------

TASK_TYPES = (
    "subtitle_cleanup",
    "subtitle_formatting",
    "clip_title",
    "clip_description",
    "transcript_cleanup",
    "content_analysis",
    "metadata_generation",
    "curation",
)

# ---------------------------------------------------------------------------
# Health / circuit states (§23, §24, §53)
# ---------------------------------------------------------------------------

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
RATE_LIMITED = "RATE_LIMITED"
UNAVAILABLE = "UNAVAILABLE"
INVALID = "INVALID"
DISABLED = "DISABLED"
UNKNOWN = "UNKNOWN"

CIRCUIT_CLOSED = "CLOSED"
CIRCUIT_OPEN = "OPEN"
CIRCUIT_HALF_OPEN = "HALF_OPEN"

# Error actions (§7)
RETRY_SAME = "RETRY_SAME"
NEXT_MODEL = "NEXT_MODEL"
NEXT_PROVIDER = "NEXT_PROVIDER"
PERMANENT = "PERMANENT"  # fatal / invalid config / cancelled: never retry

# Error categories (§8)
CAT_INVALID_CONFIG = "INVALID_CONFIGURATION"
CAT_AUTH = "AUTH"
CAT_MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
CAT_RATE_LIMIT = "RATE_LIMIT"
CAT_SERVER = "SERVER"
CAT_TIMEOUT = "TIMEOUT"
CAT_NETWORK = "NETWORK"
CAT_CANCELLED = "CANCELLED"
CAT_CLIENT_STATE = "CLIENT_STATE"
CAT_UNKNOWN = "UNKNOWN"

# Redis keys (§14: reuse existing Redis, no new database)
PROVIDERS_HASH = "gaeclip:gemini:providers"
HEALTH_HASH = "gaeclip:gemini:health"
METRICS_HASH = "gaeclip:gemini:metrics"
INFLIGHT_PREFIX = "gaeclip:gemini:inflight:"
RR_COUNTER = "gaeclip:gemini:rr"

SUCCESS_WINDOW = 100  # last-N outcomes for UI success rate


# ---------------------------------------------------------------------------
# Error classification (§7, §8)
# ---------------------------------------------------------------------------

def parse_retry_after(exc: BaseException) -> float | None:
    """Best-effort Retry-After in seconds from message/headers (capped later)."""
    text = str(exc)
    match = re.search(r"retry[_\s-]*after[:\s]+(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if match:
        try:
            return max(0.0, float(match.group(1)))
        except ValueError:
            pass
    match = re.search(r"try again in (\d+(?:\.\d+)?)\s*s", text, re.IGNORECASE)
    if match:
        try:
            return max(0.0, float(match.group(1)))
        except ValueError:
            pass
    headers = getattr(exc, "headers", None)
    if isinstance(headers, dict):
        for key in ("retry-after", "Retry-After"):
            if key in headers:
                try:
                    return max(0.0, float(headers[key]))
                except (TypeError, ValueError):
                    pass
    return None


def _is_cancelled(exc: BaseException) -> bool:
    if isinstance(exc, (KeyboardInterrupt,)):
        return True
    name = type(exc).__name__
    if name in ("CancelledError", "CancelRequest"):
        return True
    lowered = str(exc).lower()
    return ("cancelled" in lowered or "canceled" in lowered) and "client" not in lowered


def classify_action(exc: BaseException) -> tuple[str, str, float | None]:
    """Map an exception to (category, action, retry_after_s|None).

    Reuses the project's classifiers (gemini_service.classify_error for
    SDK/transport text, ai_models.error_code_of for status semantics) and
    extends them to the six-way action routing. Secrets never inspected.
    """
    from .ai_models import error_code_of

    if _is_cancelled(exc):
        return CAT_CANCELLED, PERMANENT, None
    code, retryable, fatal = error_code_of(exc)
    lowered = str(exc).lower()
    status = getattr(exc, "code", None)
    try:
        status_num = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_num = None

    if fatal or status_num in (401, 403):
        return CAT_AUTH, NEXT_PROVIDER, None
    if status_num == 400 or code == "invalid_response":
        return CAT_INVALID_CONFIG, PERMANENT, None
    if status_num == 404 or code == "model_not_found":
        return CAT_MODEL_NOT_FOUND, NEXT_MODEL, None
    if status_num == 429 or code == "rate_limit":
        # Quota is per model/key: retrying the same pair burns quota that
        # will not recover in seconds. Fail over immediately to the next
        # model (fresh per-model quota) or provider (fresh project quota);
        # the cooldown is still recorded below for future requests.
        return CAT_RATE_LIMIT, NEXT_MODEL, parse_retry_after(exc)
    if status_num in (500, 502, 503, 504):
        return CAT_SERVER, RETRY_SAME, parse_retry_after(exc)
    if code == "timeout":
        return CAT_TIMEOUT, RETRY_SAME, None
    if code in ("network_error", "client_lifecycle_error"):
        # Lifecycle errors recreate a fresh client per attempt (§73-74);
        # network blips prefer the next provider after bounded retries.
        action = RETRY_SAME
        category = CAT_NETWORK if code == "network_error" else CAT_CLIENT_STATE
        return category, action, None
    if retryable:
        return CAT_UNKNOWN, RETRY_SAME, parse_retry_after(exc)
    if "closed" in lowered and "client" in lowered:
        return CAT_CLIENT_STATE, RETRY_SAME, None
    return CAT_UNKNOWN, NEXT_MODEL, None


def compute_backoff(attempt: int, base_ms: int, max_ms: int,
                    jitter_ratio: float, retry_after_s: float | None = None) -> float:
    """Bounded exponential backoff in seconds with bounded jitter.

    Retry-After is honored but capped at max_ms (safe + bounded, §TEST 7).
    jitter_ratio bounds the randomness: delay * U(1-j, 1+j).
    """
    delay_ms = min(base_ms * (2 ** max(0, attempt)), max_ms)
    if retry_after_s is not None:
        delay_ms = min(retry_after_s * 1000.0, max_ms)
    jitter = max(0.0, min(1.0, jitter_ratio))
    factor = 1.0 if jitter == 0 else random.uniform(1.0 - jitter, 1.0 + jitter)
    return max(0.0, (delay_ms * factor) / 1000.0)


# ---------------------------------------------------------------------------
# Pool configuration (SettingDefs live in app_settings; this is the runtime)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PoolConfig:
    # Total request budget. Must comfortably exceed ONE provider's full sweep
    # (models x (1 + retry_max_attempts)) or providers #2..N starve: e.g. with
    # 3 models x 3 tries = 9, a 9-budget dies inside provider #1 (seen live:
    # 9 attempts on gemini_api_1, P2-P5 never attempted). Still bounded.
    max_attempts: int = 25
    retry_max_attempts: int = 2
    retry_base_delay_ms: int = 500
    retry_max_delay_ms: int = 5000
    retry_jitter_ratio: float = 0.25
    request_timeout_ms: int = 30000
    circuit_failure_threshold: int = 3
    circuit_cooldown_ms: int = 30000
    auth_cooldown_ms: int = 300000
    max_concurrent_per_provider: int = 2
    strategy: str = "priority"  # priority|round_robin|health_first
    discovery_ttl_seconds: int = 21600


def _svc_int(service: object, key: str, default: int) -> int:
    try:
        return int(service.get(key))  # type: ignore[union-attr]
    except Exception:
        return default


def _svc_float(service: object, key: str, default: float) -> float:
    try:
        return float(service.get(key))  # type: ignore[union-attr]
    except Exception:
        return default


def pool_config_from_service(service: object) -> PoolConfig:
    strategy = "priority"
    try:
        raw = str(service.get("GEMINI_ROUTING_STRATEGY")).strip().lower()  # type: ignore[union-attr]
        if raw in ("priority", "round_robin", "health_first"):
            strategy = raw
    except Exception:
        pass
    return PoolConfig(
        max_attempts=_svc_int(service, "GEMINI_MAX_ATTEMPTS", 25),
        retry_max_attempts=_svc_int(service, "GEMINI_RETRY_MAX_ATTEMPTS", 2),
        retry_base_delay_ms=_svc_int(service, "GEMINI_RETRY_BASE_DELAY_MS", 500),
        retry_max_delay_ms=_svc_int(service, "GEMINI_RETRY_MAX_DELAY_MS", 5000),
        retry_jitter_ratio=_svc_float(service, "GEMINI_RETRY_JITTER_RATIO", 0.25),
        request_timeout_ms=_svc_int(service, "GEMINI_REQUEST_TIMEOUT_MS", 30000),
        circuit_failure_threshold=_svc_int(service, "GEMINI_CIRCUIT_FAILURE_THRESHOLD", 3),
        circuit_cooldown_ms=_svc_int(service, "GEMINI_CIRCUIT_COOLDOWN_MS", 30000),
        auth_cooldown_ms=_svc_int(service, "GEMINI_AUTH_COOLDOWN_MS", 300000),
        max_concurrent_per_provider=_svc_int(service, "GEMINI_MAX_CONCURRENT_PER_PROVIDER", 2),
        strategy=strategy,
    )


# ---------------------------------------------------------------------------
# Provider model (§2, §13)
# ---------------------------------------------------------------------------

@dataclass
class ProviderModel:
    id: str
    enabled: bool = True
    priority: int = 1


@dataclass
class GeminiProvider:
    id: str
    name: str
    enabled: bool = True
    priority: int = 1
    api_key: str = ""  # plaintext in memory only; encrypted at rest
    models: list[ProviderModel] = field(default_factory=list)
    unavailable_models: list[str] = field(default_factory=list)
    # Model source: "manual" uses the explicit list above; "automatic"
    # resolves the chain at runtime from this key's discovery registry
    # (policy-ranked, same as global automatic mode), falling back to
    # the manual list when discovery is unavailable.
    model_mode: str = "manual"
    auto_depth: int = 4
    auto_free_tier_only: bool = True

    def ordered_models(self) -> list[ProviderModel]:
        return sorted(
            [m for m in self.models if m.enabled and m.id not in set(self.unavailable_models)],
            key=lambda m: (m.priority, m.id),
        )

    def masked(self) -> dict:
        key = self.api_key or ""
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "key_suffix": key[-4:] if len(key) >= 4 else "",
            "key_configured": bool(key.strip()),
            "models": [{"id": m.id, "enabled": m.enabled, "priority": m.priority}
                       for m in sorted(self.models, key=lambda m: (m.priority, m.id))],
            "unavailable_models": list(self.unavailable_models),
            "model_mode": self.model_mode,
            "auto_depth": self.auto_depth,
            "auto_free_tier_only": self.auto_free_tier_only,
        }

    def to_store(self) -> dict:
        from .app_settings import encrypt_secret

        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "api_key": encrypt_secret(self.api_key) if self.api_key else "",
            "models": [{"id": m.id, "enabled": m.enabled, "priority": m.priority}
                       for m in self.models],
            "unavailable_models": list(self.unavailable_models),
            "model_mode": self.model_mode,
            "auto_depth": self.auto_depth,
            "auto_free_tier_only": self.auto_free_tier_only,
        }

    @classmethod
    def from_store(cls, payload: dict) -> "GeminiProvider":
        from .app_settings import decrypt_secret

        stored_key = str(payload.get("api_key") or "")
        try:
            key = decrypt_secret(stored_key) if stored_key else ""
        except Exception:
            key = ""
        models = [ProviderModel(id=str(m.get("id", "")), enabled=bool(m.get("enabled", True)),
                                priority=int(m.get("priority", i + 1)))
                  for i, m in enumerate(payload.get("models") or []) if m.get("id")]
        mode = str(payload.get("model_mode") or "manual").strip().lower()
        try:
            depth = max(1, min(10, int(payload.get("auto_depth", 4))))
        except (TypeError, ValueError):
            depth = 4
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name") or payload.get("id", "")),
            enabled=bool(payload.get("enabled", True)),
            priority=int(payload.get("priority", 1)),
            api_key=key,
            models=models,
            unavailable_models=[str(m) for m in (payload.get("unavailable_models") or [])],
            model_mode=mode if mode in ("manual", "automatic") else "manual",
            auto_depth=depth,
            auto_free_tier_only=bool(payload.get("auto_free_tier_only", True)),
        )


# ---------------------------------------------------------------------------
# Provider store (§14, §45, §46): Redis hash, encrypted keys, legacy migration
# ---------------------------------------------------------------------------

class ProviderStore:
    """CRUD for provider configs. Secrets encrypted via app_settings helpers."""

    def __init__(self, client: object | None):
        self.client = client

    def _hgetall(self) -> dict:
        if self.client is None:
            return {}
        try:
            raw = self.client.hgetall(PROVIDERS_HASH)  # type: ignore[union-attr]
        except Exception:
            logger.warning("provider_store_read_failed", exc_info=True)
            return {}
        out: dict = {}
        for key, value in (raw or {}).items():
            k = key.decode() if isinstance(key, bytes) else str(key)
            text = value.decode() if isinstance(value, bytes) else str(value)
            try:
                out[k] = json.loads(text)
            except ValueError:
                continue
        return out

    def list(self) -> list[GeminiProvider]:
        providers = []
        for pid, payload in self._hgetall().items():
            if not isinstance(payload, dict):
                continue
            try:
                provider = GeminiProvider.from_store(payload)
            except Exception:
                logger.warning("provider_store_decode_failed provider=%s", pid)
                continue
            if provider.id:
                providers.append(provider)
        return sorted(providers, key=lambda p: (p.priority, p.id))

    def get(self, provider_id: str) -> GeminiProvider | None:
        for provider in self.list():
            if provider.id == provider_id:
                return provider
        return None

    def has_usable(self) -> bool:
        """True when at least one enabled provider has a key and one
        executable model. Lets the pipeline use the pool even when the
        legacy single GEMINI_API_KEY is empty (pool-only setups)."""
        try:
            for provider in self.list():
                if not provider.enabled or not provider.api_key.strip():
                    continue
                if provider.ordered_models():
                    return True
        except Exception:
            return False
        return False

    def save(self, provider: GeminiProvider) -> None:
        if self.client is None:
            raise ValueError("Settings storage is not available")
        if not provider.id.strip():
            raise ValueError("Provider id must not be empty")
        self.client.hset(PROVIDERS_HASH, provider.id, json.dumps(provider.to_store()))  # type: ignore[union-attr]
        logger.info("gemini_provider_saved provider=%s models=%d", provider.id, len(provider.models))

    def delete(self, provider_id: str) -> bool:
        if self.client is None:
            raise ValueError("Settings storage is not available")
        removed = self.client.hdel(PROVIDERS_HASH, provider_id)  # type: ignore[union-attr]
        logger.info("gemini_provider_deleted provider=%s", provider_id)
        return bool(removed)

    def mark_model_unavailable(self, provider_id: str, model_id: str) -> None:
        """Persist §22 unavailable marking (never auto-deletes user config)."""
        provider = self.get(provider_id)
        if provider is None or model_id in provider.unavailable_models:
            return
        provider.unavailable_models.append(model_id)
        try:
            self.save(provider)
        except Exception:
            logger.warning("provider_model_mark_failed", exc_info=True)

    def clear_model_unavailable(self, provider_id: str, model_id: str | None = None) -> None:
        provider = self.get(provider_id)
        if provider is None:
            return
        if model_id is None:
            provider.unavailable_models = []
        else:
            provider.unavailable_models = [m for m in provider.unavailable_models if m != model_id]
        try:
            self.save(provider)
        except Exception:
            logger.warning("provider_model_unmark_failed", exc_info=True)

    def migrate_legacy(self, service: object) -> list[GeminiProvider]:
        """Seed Provider 1 from GEMINI_API_KEY + model chain when pool empty.

        Never overwrites existing providers. Returns the resulting list.
        """
        existing = self.list()
        if existing:
            return existing
        try:
            key = str(service.get("GEMINI_API_KEY") or "").strip()  # type: ignore[union-attr]
        except Exception:
            key = ""
        if not key:
            return []
        try:
            from .ai_models import parse_fallbacks

            primary = str(service.get("AI_PRIMARY_MODEL") or "").strip()  # type: ignore[union-attr]
            fallbacks = parse_fallbacks(service.get("AI_FALLBACK_MODELS"))  # type: ignore[union-attr]
            if not primary:
                primary = str(service.get("GEMINI_MODEL") or "gemini-3.8-flash").strip()  # type: ignore[union-attr]
        except Exception:
            primary, fallbacks = "gemini-3.8-flash", []
        chain = [primary, *[m for m in fallbacks if m != primary]]
        provider = GeminiProvider(
            id="gemini_api_1", name="Gemini API 1", enabled=True, priority=1,
            api_key=key,
            models=[ProviderModel(id=mid, enabled=True, priority=i + 1) for i, mid in enumerate(chain)],
        )
        try:
            self.save(provider)
            logger.info("gemini_provider_migrated_legacy models=%d", len(chain))
        except Exception:
            logger.warning("gemini_provider_migration_save_failed", exc_info=True)
            return [provider]
        return [provider]


# ---------------------------------------------------------------------------
# Health manager (§23-29, §53-55): Redis-shared state, atomic counters,
# per-provider semaphores, per-pair circuit breakers, probes, reset.
# ---------------------------------------------------------------------------

def _health_key(provider_id: str, model_id: str | None = None) -> str:
    return provider_id if not model_id else f"{provider_id}:{model_id}"


@dataclass
class HealthSnapshot:
    state: str = UNKNOWN
    failures: int = 0
    successes: int = 0
    cooldown_until: float = 0.0
    circuit: str = CIRCUIT_CLOSED
    last_success: float | None = None
    last_error: str | None = None
    last_error_category: str | None = None
    last_update: float = 0.0


class HealthManager:
    def __init__(self, client: object | None, config: PoolConfig):
        self.client = client
        self.config = config
        self._lock = threading.Lock()
        self._semaphores: dict[str, threading.Semaphore] = {}
        self._rr = 0
        self._outcomes: dict[str, deque] = {}

    # -- state IO ------------------------------------------------------
    def _read(self, key: str) -> HealthSnapshot:
        if self.client is None:
            return HealthSnapshot()
        try:
            raw = self.client.hget(HEALTH_HASH, key)  # type: ignore[union-attr]
        except Exception:
            return HealthSnapshot()
        if not raw:
            return HealthSnapshot()
        try:
            text = raw.decode() if isinstance(raw, bytes) else str(raw)
            data = json.loads(text)
        except ValueError:
            return HealthSnapshot()
        snap = HealthSnapshot()
        for field_name in ("state", "failures", "successes", "cooldown_until",
                           "circuit", "last_success", "last_error",
                           "last_error_category", "last_update"):
            if field_name in data:
                try:
                    setattr(snap, field_name, data[field_name])
                except (TypeError, ValueError):
                    pass
        return snap

    def _write(self, key: str, snap: HealthSnapshot) -> None:
        if self.client is None:
            return
        try:
            self.client.hset(HEALTH_HASH, key, json.dumps(asdict(snap)))  # type: ignore[union-attr]
        except Exception:
            logger.warning("provider_health_write_failed", exc_info=True)

    def snapshot(self, provider_id: str, model_id: str | None = None) -> HealthSnapshot:
        return self._read(_health_key(provider_id, model_id))

    def is_cooled_down(self, provider_id: str, model_id: str | None = None,
                       now: float | None = None) -> bool:
        moment = now if now is not None else time.time()
        return self._read(_health_key(provider_id, model_id)).cooldown_until <= moment

    # -- success / failure (§29 atomicity via timestamp guard) ---------
    def record_success(self, provider_id: str, model_id: str,
                       latency_ms: int, at: float | None = None) -> None:
        moment = at if at is not None else time.time()
        for key in (_health_key(provider_id), _health_key(provider_id, model_id)):
            snap = self._read(key)
            if moment < (snap.last_update or 0):
                continue  # stale success must not resurrect over newer state
            snap.state = HEALTHY
            snap.successes += 1
            snap.failures = 0
            snap.cooldown_until = 0.0
            snap.circuit = CIRCUIT_CLOSED
            snap.last_success = moment
            snap.last_error = None
            snap.last_error_category = None
            snap.last_update = moment
            self._write(key, snap)
        with self._lock:
            outcomes = self._outcomes.setdefault(provider_id, deque(maxlen=SUCCESS_WINDOW))
            outcomes.append(1)
        logger.info("[GEMINI_SUCCESS] provider=%s model=%s latency_ms=%d",
                    provider_id, model_id, latency_ms)

    def record_failure(self, provider_id: str, model_id: str, category: str,
                       detail: str, at: float | None = None) -> None:
        moment = at if at is not None else time.time()
        pair_key = _health_key(provider_id, model_id)
        pair = self._read(pair_key)
        if moment < (pair.last_update or 0):
            return  # stale failure must not clobber newer success (§26 race)
        pair.failures += 1
        pair.last_error = detail[:200]
        pair.last_error_category = category
        pair.last_update = moment
        threshold = max(1, self.config.circuit_failure_threshold)
        if category == CAT_RATE_LIMIT:
            pair.state = RATE_LIMITED
            pair.cooldown_until = moment + self.config.circuit_cooldown_ms / 1000.0
            logger.info("[GEMINI_RATE_LIMIT] provider=%s model=%s cooldown_ms=%d",
                        provider_id, model_id, self.config.circuit_cooldown_ms)
        elif category == CAT_AUTH:
            pair.state = INVALID
            pair.cooldown_until = moment + self.config.auth_cooldown_ms / 1000.0
            logger.info("[GEMINI_COOLDOWN] provider=%s model=%s reason=auth cooldown_ms=%d",
                        provider_id, model_id, self.config.auth_cooldown_ms)
        elif pair.failures >= threshold:
            pair.state = UNAVAILABLE
            pair.circuit = CIRCUIT_OPEN
            pair.cooldown_until = moment + self.config.circuit_cooldown_ms / 1000.0
            logger.info("[GEMINI_COOLDOWN] provider=%s model=%s reason=circuit_open "
                        "failures=%d cooldown_ms=%d",
                        provider_id, model_id, pair.failures,
                        self.config.circuit_cooldown_ms)
        else:
            pair.state = DEGRADED
        self._write(pair_key, pair)
        # Provider rollup mirrors worst pair without killing healthy models.
        prov = self._read(_health_key(provider_id))
        if moment >= (prov.last_update or 0):
            prov.failures += 1
            prov.last_error = detail[:200]
            prov.last_error_category = category
            prov.last_update = moment
            if category == CAT_AUTH:
                prov.state = INVALID
                prov.cooldown_until = moment + self.config.auth_cooldown_ms / 1000.0
            elif prov.state not in (RATE_LIMITED, INVALID):
                prov.state = DEGRADED
            self._write(_health_key(provider_id), prov)
        with self._lock:
            outcomes = self._outcomes.setdefault(provider_id, deque(maxlen=SUCCESS_WINDOW))
            outcomes.append(0)

    def probe_eligible(self, provider_id: str, model_id: str) -> bool:
        """First attempt after cooldown expiry is a PROBE (§27)."""
        snap = self._read(_health_key(provider_id, model_id))
        return (snap.circuit == CIRCUIT_OPEN and snap.cooldown_until
                and snap.cooldown_until <= time.time())

    def reset(self, provider_id: str | None = None) -> None:
        """Reset Health UI action (§55): cooldowns, counters, circuits."""
        if self.client is None:
            return
        try:
            if provider_id is None:
                for key in list(self.client.hgetall(HEALTH_HASH) or {}):  # type: ignore[union-attr]
                    self.client.hdel(HEALTH_HASH, key)  # type: ignore[union-attr]
            else:
                for key in list(self.client.hgetall(HEALTH_HASH) or {}):  # type: ignore[union-attr]
                    name = key.decode() if isinstance(key, bytes) else str(key)
                    if name == provider_id or name.startswith(provider_id + ":"):
                        self.client.hdel(HEALTH_HASH, key)  # type: ignore[union-attr]
        except Exception:
            logger.warning("provider_health_reset_failed", exc_info=True)
        with self._lock:
            if provider_id is None:
                self._outcomes.clear()
            else:
                self._outcomes.pop(provider_id, None)
        logger.info("[GEMINI_PROVIDER_HEALTH] reset provider=%s", provider_id or "all")

    def success_rate(self, provider_id: str) -> float | None:
        with self._lock:
            outcomes = list(self._outcomes.get(provider_id, ()))
        if not outcomes:
            return None
        return sum(outcomes) / len(outcomes)

    # -- concurrency (§28) ---------------------------------------------
    def _semaphore(self, provider_id: str) -> threading.Semaphore:
        with self._lock:
            semaphore = self._semaphores.get(provider_id)
            if semaphore is None:
                semaphore = threading.Semaphore(max(1, self.config.max_concurrent_per_provider))
                self._semaphores[provider_id] = semaphore
            return semaphore

    def try_acquire(self, provider_id: str) -> bool:
        """Non-blocking take: local semaphore + coarse Redis counter."""
        if not self._semaphore(provider_id).acquire(blocking=False):
            return False
        if self.client is not None:
            field = INFLIGHT_PREFIX + provider_id
            try:
                current = int(self.client.incr(field))  # type: ignore[union-attr]
                try:
                    self.client.expire(field, 300)  # type: ignore[union-attr]
                except Exception:
                    pass
                if current > max(1, self.config.max_concurrent_per_provider):
                    try:
                        self.client.decr(field)  # type: ignore[union-attr]
                    except Exception:
                        pass
                    self._semaphore(provider_id).release()
                    return False
            except Exception:
                pass
        return True

    def release(self, provider_id: str) -> None:
        try:
            self._semaphore(provider_id).release()
        except ValueError:
            pass
        if self.client is not None:
            try:
                value = int(self.client.decr(INFLIGHT_PREFIX + provider_id))  # type: ignore[union-attr]
                if value < 0:
                    self.client.delete(INFLIGHT_PREFIX + provider_id)  # type: ignore[union-attr]
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Metrics (§69): in-memory counters (project has no metrics system).
# ---------------------------------------------------------------------------

class PoolMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._latency_ms_total = 0
        self._latency_ms_count = 0

    def incr(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def observe_latency(self, latency_ms: int) -> None:
        with self._lock:
            self._latency_ms_total += max(0, latency_ms)
            self._latency_ms_count += 1

    def snapshot(self) -> dict:
        with self._lock:
            data = dict(self._counters)
            total = self._latency_ms_total
            count = self._latency_ms_count
        data["latency_ms_avg"] = (total / count) if count else 0.0
        data["latency_ms_samples"] = count
        return data


METRICS = PoolMetrics()


# ---------------------------------------------------------------------------
# Request context (§11) + result / exhaustion (§49, §61)
# ---------------------------------------------------------------------------

@dataclass
class RequestContext:
    request_id: str
    task_type: str
    job_id: str | None = None


@dataclass
class AttemptRecord:
    provider: str
    model: str
    status: str | int | None
    category: str
    retry: int
    latency_ms: int
    timestamp: float


class PoolExhausted(Exception):
    """All providers/models failed (or budget spent). Callers fall back."""

    def __init__(self, failures: list[dict], last_error: BaseException | None = None,
                 report: str = ""):
        super().__init__("All Gemini providers failed")
        self.failures = failures
        self.last_error = last_error
        self.report = report


class _NextProvider(Exception):
    """Internal: abandon remaining models, continue with next provider."""


def failure_report(failures: list[dict]) -> str:
    lines = [f"Gemini failed after {len(failures)} attempts."]
    by_provider: dict[str, list[dict]] = {}
    for entry in failures:
        by_provider.setdefault(str(entry.get("provider", "?")), []).append(entry)
    for provider, entries in by_provider.items():
        lines.append(f"{provider}:")
        for entry in entries:
            lines.append(f"  {entry.get('model')} -> {entry.get('status') or entry.get('category')}"
                         + (" (skipped)" if entry.get("skipped") else ""))
    lines.append("Final fallback: local heuristic")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scheduler (§30-33)
# ---------------------------------------------------------------------------

def _provider_state_rank(state: str) -> int:
    return {HEALTHY: 0, DEGRADED: 1, UNKNOWN: 2, RATE_LIMITED: 3,
            UNAVAILABLE: 4, INVALID: 5, DISABLED: 6}.get(state, 2)


def order_providers(providers: list[GeminiProvider], health: HealthManager,
                    strategy: str, client: object | None) -> list[GeminiProvider]:
    eligible = [p for p in providers if p.enabled]
    if strategy == "round_robin" and eligible:
        offset = 0
        if client is not None:
            try:
                offset = int(client.incr(RR_COUNTER))  # type: ignore[union-attr]
            except Exception:
                with health._lock:
                    health._rr += 1
                    offset = health._rr
        else:
            with health._lock:
                health._rr += 1
                offset = health._rr
        ordered = sorted(eligible, key=lambda p: (p.priority, p.id))
        offset = offset % len(ordered)
        return ordered[offset:] + ordered[:offset]
    if strategy == "health_first":
        def key(provider: GeminiProvider) -> tuple:
            snap = health.snapshot(provider.id)
            rate = health.success_rate(provider.id)
            return (_provider_state_rank(snap.state),
                    -(rate if rate is not None else 0.5), provider.priority, provider.id)
        return sorted(eligible, key=key)
    return sorted(eligible, key=lambda p: (p.priority, p.id))


# ---------------------------------------------------------------------------
# Executor (§1, §6, §9-10, §32, §34-36, §48-50, §75-77)
# ---------------------------------------------------------------------------

def _new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def execute_pool(
    providers: list[GeminiProvider],
    *,
    task_type: str,
    request_fn,
    config: PoolConfig,
    health: HealthManager,
    job_id: str | None = None,
    sleep_fn=time.sleep,
    metrics: PoolMetrics | None = None,
) -> tuple[object, dict]:
    """Two-level fallback: models inside a provider, then next provider.

    ``request_fn(provider_id, model_id, attempt)`` performs ONE attempt and
    raises on failure (fresh SDK client per call by the caller). Returns
    (response, meta). Stops at first success (§76) or budget exhaustion.
    """
    if task_type not in TASK_TYPES:
        task_type = "curation"
    metrics = metrics or METRICS
    context = RequestContext(request_id=_new_request_id(), task_type=task_type, job_id=job_id)
    ordered = order_providers(providers, health, config.strategy, health.client)
    logger.info("[GEMINI_REQUEST] request_id=%s task=%s job=%s providers=%d strategy=%s",
                context.request_id, task_type, job_id,
                len(ordered), config.strategy)
    metrics.incr("gemini_requests_total")
    failures: list[dict] = []
    history: list[AttemptRecord] = []
    attempted_pairs: set[tuple[str, str]] = set()
    pair_retries: dict[tuple[str, str], int] = {}
    attempts_total = 0
    last_error: BaseException | None = None
    first_provider = True

    for provider in ordered:
        if attempts_total >= config.max_attempts:
            break
        if not provider.api_key.strip():
            failures.append({"provider": provider.id, "model": None,
                             "category": CAT_AUTH, "skipped": True,
                             "reason": "missing_api_key"})
            continue
        if not health.is_cooled_down(provider.id):
            logger.info("[GEMINI_PROVIDER_HEALTH] provider=%s state=cooldown skipped", provider.id)
            continue
        models = provider.ordered_models()
        if not models:
            continue
        if not health.try_acquire(provider.id):
            logger.info("[GEMINI_PROVIDER_HEALTH] provider=%s busy, trying next", provider.id)
            continue
        if not first_provider:
            logger.info("[GEMINI_PROVIDER_FALLBACK] to=%s request_id=%s",
                        provider.id, context.request_id)
            metrics.incr("gemini_provider_fallback_total")
        first_provider = False
        try:
            try:
                for model in models:
                    if attempts_total >= config.max_attempts:
                        break
                    pair = (provider.id, model.id)
                    if pair in attempted_pairs and pair_retries.get(pair, 0) >= config.retry_max_attempts:
                        continue
                    pair_snap = health.snapshot(provider.id, model.id)
                    now = time.time()
                    if pair_snap.cooldown_until > now:
                        continue
                    probing = health.probe_eligible(provider.id, model.id)
                    if probing:
                        logger.info("[GEMINI_PROVIDER_HEALTH] provider=%s model=%s probe", provider.id, model.id)
                    first_in_pair = pair not in attempted_pairs
                    if not first_in_pair:
                        logger.info("[GEMINI_MODEL_FALLBACK] provider=%s still=%s request_id=%s",
                                    provider.id, model.id, context.request_id)
                    elif attempted_pairs:
                        # New model after a previous pair failed.
                        logger.info("[GEMINI_MODEL_FALLBACK] provider=%s to=%s request_id=%s",
                                    provider.id, model.id, context.request_id)
                        metrics.incr("gemini_model_fallback_total")
                    attempted_pairs.add(pair)
                    retry_index = pair_retries.get(pair, 0)
                    while True:
                        if attempts_total >= config.max_attempts:
                            break
                        attempts_total += 1
                        attempt_no = attempts_total
                        logger.info("[GEMINI_ATTEMPT] request_id=%s provider=%s model=%s attempt=%d%s",
                                    context.request_id, provider.id, model.id, attempt_no,
                                    " probe" if probing else "")
                        started = time.monotonic()
                        try:
                            response = request_fn(provider.id, model.id, retry_index)
                            latency_ms = int((time.monotonic() - started) * 1000)
                            health.record_success(provider.id, model.id, latency_ms)
                            metrics.incr("gemini_success_total")
                            metrics.observe_latency(latency_ms)
                            history.append(AttemptRecord(provider.id, model.id, "ok",
                                                         "SUCCESS", retry_index, latency_ms, time.time()))
                            logger.info("[GEMINI_SUCCESS] request_id=%s provider=%s model=%s latency_ms=%d",
                                        context.request_id, provider.id, model.id, latency_ms)
                            return response, {
                                "provider": "gemini",
                                "provider_id": provider.id,
                                "model_used": model.id,
                                "selection_method": "pool",
                                "fallback_used": len(attempted_pairs) > 1 or retry_index > 0,
                                "fallback_reason": failures[-1].get("category") if failures else None,
                                "attempt": retry_index,
                                "attempts_total": attempts_total,
                                "latency_ms": latency_ms,
                                "request_id": context.request_id,
                                "task_type": task_type,
                                "failure_history": [asdict(h) for h in history if h.status != "ok"],
                            }
                        except Exception as exc:
                            latency_ms = int((time.monotonic() - started) * 1000)
                            last_error = exc
                            category, action, retry_after = classify_action(exc)
                            status = getattr(exc, "code", None)
                            try:
                                status = int(status) if status is not None else category
                            except (TypeError, ValueError):
                                status = category
                            history.append(AttemptRecord(provider.id, model.id, status,
                                                         category, retry_index, latency_ms, time.time()))
                            failures.append({"provider": provider.id, "model": model.id,
                                             "status": status, "category": category,
                                             "retry": retry_index})
                            metrics.incr("gemini_failures_total")
                            if category == CAT_RATE_LIMIT:
                                metrics.incr("gemini_429_total")
                            elif str(status) == "500":
                                metrics.incr("gemini_500_total")
                            elif str(status) == "503":
                                metrics.incr("gemini_503_total")
                            elif category == CAT_TIMEOUT:
                                metrics.incr("gemini_timeout_total")
                            health.record_failure(provider.id, model.id, category,
                                                  f"{category} attempt={retry_index}")
                            if category == CAT_MODEL_NOT_FOUND:
                                logger.info("[GEMINI_MODEL_FALLBACK] provider=%s from=%s "
                                            "reason=model_not_found", provider.id, model.id)
                                break
                            if action == PERMANENT:
                                if category == CAT_CANCELLED:
                                    logger.info("[GEMINI_FINAL_FAILURE] request_id=%s reason=cancelled",
                                                context.request_id)
                                    raise
                                if category == CAT_AUTH:
                                    logger.info("[GEMINI_PROVIDER_FALLBACK] from=%s reason=auth",
                                                provider.id)
                                    raise _NextProvider()
                                # Invalid configuration cannot succeed elsewhere
                                # with the same request: fail fast, keep budget.
                                raise PoolExhausted(failures, last_error,
                                                    failure_report(failures))
                            if action == NEXT_PROVIDER:
                                raise _NextProvider()
                            if action == NEXT_MODEL:
                                break
                            # RETRY_SAME (bounded)
                            if retry_index >= config.retry_max_attempts:
                                logger.info("[GEMINI_MODEL_FALLBACK] provider=%s from=%s reason=retries_spent",
                                            provider.id, model.id)
                                break
                            delay = compute_backoff(
                                retry_index, config.retry_base_delay_ms,
                                config.retry_max_delay_ms, config.retry_jitter_ratio,
                                retry_after)
                            logger.info("[GEMINI_RETRY] request_id=%s provider=%s model=%s "
                                        "reason=%s delay_ms=%d",
                                        context.request_id, provider.id, model.id,
                                        category, int(delay * 1000))
                            metrics.incr("gemini_retries_total")
                            sleep_fn(delay)
                            retry_index += 1
                            pair_retries[pair] = retry_index
                            probing = False
                            continue
            except _NextProvider:
                pass
        finally:
            health.release(provider.id)

    report = failure_report(failures)
    logger.info("[GEMINI_FINAL_FAILURE] request_id=%s attempts=%d\n%s",
                context.request_id, attempts_total, report)
    raise PoolExhausted(failures, last_error, report)


# ---------------------------------------------------------------------------
# Service glue (§45, §66, §67): legacy migration + request transport
# ---------------------------------------------------------------------------

@dataclass
class PoolContext:
    """Everything curate() needs for the pool path (built once per job)."""

    providers: list[GeminiProvider]
    config: PoolConfig
    health: HealthManager
    job_id: str | None = None


def resolve_automatic_models(client: object | None,
                             providers: list[GeminiProvider]) -> list[GeminiProvider]:
    """Fill automatic-mode providers from their own discovery registry.

    Uses the same policy ranking as global automatic mode. On discovery
    failure or empty chain the manual list stands (job never bricks).
    Persisted unavailable marks still apply via ordered_models().
    """
    from .ai_models import automatic_chain, get_registry

    for provider in providers:
        if provider.model_mode != "automatic" or not provider.api_key.strip():
            continue
        try:
            snap = get_registry(client, provider.api_key)
        except Exception:
            logger.warning("provider_auto_chain_discovery_failed provider=%s; keeping manual list",
                           provider.id)
            continue
        try:
            chain = automatic_chain(snap.models, provider.auto_free_tier_only,
                                    depth=max(1, provider.auto_depth))
        except Exception:
            logger.warning("provider_auto_chain_rank_failed provider=%s; keeping manual list",
                           provider.id, exc_info=True)
            continue
        if not chain:
            logger.warning("provider_auto_chain_empty provider=%s; keeping manual list",
                           provider.id)
            continue
        provider.models = [ProviderModel(id=mid, enabled=True, priority=i + 1)
                           for i, mid in enumerate(chain)]
        logger.info("provider_auto_chain_resolved provider=%s models=%d",
                    provider.id, len(chain))
    return providers


def build_pool_context(client: object | None,
                       ai_chain: list[str] | None = None,
                       job_id: str | None = None) -> PoolContext | None:
    """Assemble providers + tuned config + health from AppSettings.

    Request timeout: an explicitly customized GEMINI_REQUEST_TIMEOUT_MS
    wins; otherwise the legacy GEMINI_TIMEOUT_SECONDS is preserved so
    existing users see zero behavior change.
    """
    from .app_settings import AppSettings

    service = AppSettings(client)
    store = ProviderStore(client)
    providers = store.list()
    if not providers:
        try:
            key = str(service.get("GEMINI_API_KEY") or "").strip()
        except Exception:
            key = ""
        if not key:
            return None
        try:
            from .ai_models import parse_fallbacks

            primary = str(service.get("AI_PRIMARY_MODEL") or "").strip()
            fallbacks = parse_fallbacks(service.get("AI_FALLBACK_MODELS"))
            if not primary:
                primary = str(service.get("GEMINI_MODEL") or "gemini-3.8-flash").strip()
        except Exception:
            primary, fallbacks = "gemini-3.8-flash", []
        if ai_chain:
            primary, fallbacks = ai_chain[0], [m for m in ai_chain[1:] if m != ai_chain[0]]
        chain = [primary, *[m for m in fallbacks if m != primary]]
        providers = [GeminiProvider(
            id="gemini_api_1", name="Gemini API 1", enabled=True, priority=1,
            api_key=key,
            models=[ProviderModel(id=mid, enabled=True, priority=i + 1)
                    for i, mid in enumerate(chain)])]
    providers = resolve_automatic_models(client, providers)
    config = pool_config_from_service(service)
    try:
        customized = service.is_configured("GEMINI_REQUEST_TIMEOUT_MS")
    except Exception:
        customized = False
    if not customized:
        try:
            legacy_ms = int(service.get("GEMINI_TIMEOUT_SECONDS")) * 1000
        except Exception:
            legacy_ms = config.request_timeout_ms
        import dataclasses

        config = dataclasses.replace(config, request_timeout_ms=max(1000, legacy_ms))
    return PoolContext(providers=providers, config=config,
                       health=HealthManager(client, config), job_id=job_id)


def default_transport(api_key: str, model: str, contents: object,
                      timeout_ms: int, temperature: float = 0.2,
                      max_output_tokens: int | None = None):
    """One attempt with a strictly scoped SDK client (never shared/closed)."""
    from .gemini_service import generate_content

    return generate_content(
        api_key=api_key,
        model=model,
        contents=contents,
        timeout_seconds=max(1, int(timeout_ms / 1000)),
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )


def generate_via_pool(
    providers: list[GeminiProvider],
    *,
    task_type: str,
    contents: object,
    config: PoolConfig,
    health: HealthManager,
    job_id: str | None = None,
    temperature: float = 0.2,
    max_output_tokens: int | None = None,
    sleep_fn=time.sleep,
    transport=None,
    metrics: PoolMetrics | None = None,
) -> tuple[object, dict]:
    """GeminiService.generate core: pool execution with fresh clients."""
    keys = {p.id: p.api_key for p in providers}

    def request_fn(provider_id: str, model_id: str, attempt: int):
        if transport is not None:
            return transport(provider_id, model_id, attempt)
        return default_transport(
            keys.get(provider_id, ""), model_id, contents,
            config.request_timeout_ms, temperature, max_output_tokens)

    return execute_pool(
        providers, task_type=task_type, request_fn=request_fn,
        config=config, health=health, job_id=job_id,
        sleep_fn=sleep_fn, metrics=metrics)
