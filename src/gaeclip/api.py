from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .app_settings import AppSettings, apply_log_level
from .config import Settings
from .errors import ConflictError, ConfigurationError, ValidationError
from .jobs import JobRecord, JobRepository, TERMINAL_STATES
from .storage import new_job_id
from .url import extract_video_id, validate_youtube_url
from .videos import VideoStore

try:
    from fastapi import Response as FastAPIResponse
except ImportError:
    FastAPIResponse = Any


MAX_CLIPS_HARD_LIMIT = 10


class CreateJobRequest(BaseModel):
    source_url: str
    language: str = Field(default="id", min_length=2, max_length=16)
    max_clips: int | None = Field(default=None, ge=1, le=MAX_CLIPS_HARD_LIMIT)
    min_duration: float | None = Field(default=None, ge=5, le=600)
    max_duration: float | None = Field(default=None, ge=5, le=600)
    burn_subtitles: bool = True
    subtitle_style: Literal["bold_pop", "dynamic_highlight", "casual_center", "elegant_serif"] = "casual_center"
    subtitle_position: Literal["center", "bottom_center", "bottom"] = "bottom_center"


class RecreateJobRequest(BaseModel):
    force_retranscribe: bool = False


class SettingsUpdate(BaseModel):
    settings: dict[str, Any]


class SettingValue(BaseModel):
    value: Any


class GeminiTestRequest(BaseModel):
    api_key: str | None = None


class ProviderModelBody(BaseModel):
    id: str
    enabled: bool = True
    priority: int = 1


class ProviderUpsert(BaseModel):
    name: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    models: list[ProviderModelBody] | None = None
    model_mode: str | None = None
    auto_depth: int | None = None
    auto_free_tier_only: bool | None = None


class ProviderReorder(BaseModel):
    ids: list[str]


class ProviderTestModel(BaseModel):
    model: str


class YoutubeCookiesBody(BaseModel):
    cookies_txt: str


class FallbackChainTestBody(BaseModel):
    fail_provider_ids: list[str] = []
    max_providers: int | None = None


class HealthResetBody(BaseModel):
    provider_id: str | None = None


def create_app():
    try:
        from fastapi import FastAPI, Header, HTTPException
        from fastapi.responses import FileResponse
    except ImportError as exc:
        raise RuntimeError("Install gaeclip[api] to run the API") from exc
    try:
        from .runtime.factory import is_desktop, make_client, make_runner
        from .runtime.mode import RuntimeMode, current_runtime
    except ImportError as exc:
        raise RuntimeError("Install gaeclip[api] to run the API") from exc
    if not is_desktop():
        try:
            import redis  # noqa: F401  (server edition requires the driver)
        except ImportError as exc:
            raise RuntimeError("Install gaeclip[api] to run the API") from exc

    logger = logging.getLogger("gaeclip.api")
    runtime_mode = current_runtime()
    client = make_client()
    runner = make_runner()
    app_settings = AppSettings(client)
    app_settings.migrate_from_env()
    settings = Settings.resolve(client)
    settings.ensure_storage()
    apply_log_level(app_settings.get("LOG_LEVEL"))
    repository = JobRepository(client)
    app = FastAPI(title="Gaeclip API", version="1")
    from fastapi.middleware.cors import CORSMiddleware
    allowed_origins = [
        origin.strip()
        for origin in os.getenv(
            "FRONTEND_ORIGINS",
            "http://127.0.0.1:3000,http://localhost:3000",
        ).split(",")
        if origin.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key"],
    )

    def public_job(record: JobRecord) -> dict:
        payload = record.to_dict()
        public_clips = []
        for clip in record.clips or []:
            public_clip = {key: value for key, value in clip.items() if key != "path"}
            public_clip["download_url"] = (
                f"/api/v1/jobs/{record.job_id}/clips/{clip['clip_id']}"
            )
            public_clips.append(public_clip)
        payload["clips"] = public_clips
        return payload

    @app.get("/")
    def root():
        return {
            "service": "gaeclip",
            "message": "UI ada di http://127.0.0.1:3000, dokumentasi API di /docs",
            "docs": "/docs",
            "health": ["/health/live", "/health/ready", "/health/config"],
            "jobs": "/api/v1/jobs",
        }

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/api/v1/worker/status")
    def worker_status():
        """Best-effort worker visibility: what's running vs waiting.

        Never 500s: inspect failures degrade to worker_up=false with a
        note, so the jobs UI can explain a stuck 'queued' status instead
        of showing silence. Desktop reports the in-process LocalRunner.
        """
        result: dict = {"worker_up": False, "active_jobs": [],
                        "reserved_jobs": [], "note": "",
                        "runtime": runtime_mode.value,
                        "runner": runner.kind}
        if is_desktop():
            try:
                active = runner.active_jobs()
                queued = runner.queued_jobs()
                result.update(worker_up=True, active_jobs=active,
                              reserved_jobs=queued)
            except Exception as exc:
                result["note"] = f"local runner inspect gagal: {exc}"
            return result
        try:
            from celery import Celery

            app_celery = Celery("gaeclip", broker=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))
            insp = app_celery.control.inspect(timeout=2)

            def job_ids(entries) -> list[str]:
                out: list[str] = []
                for _worker, tasks in (entries or {}).items():
                    for task in tasks or []:
                        args = task.get("args") or []
                        if args and task.get("name") == "gaeclip.process_job":
                            out.append(str(args[0]))
                return out

            active_raw = insp.active()
            reserved_raw = insp.reserved()
            if active_raw is None and reserved_raw is None:
                result["note"] = "worker tidak merespons inspect"
                return result
            result.update(worker_up=True,
                          active_jobs=job_ids(active_raw),
                          reserved_jobs=job_ids(reserved_raw))
            return result
        except Exception as exc:
            result["note"] = f"inspect gagal: {exc}"
            return result

    @app.get("/health/ready")
    def ready():
        try:
            client.ping()
            shutil.which("ffmpeg") or (_ for _ in ()).throw(RuntimeError("ffmpeg not found"))
            return {"status": "ready"}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/health/config")
    def config():
        # Resolved fresh per request so UI always sees current values.
        live = Settings.resolve(client)
        try:
            client.ping()
            redis_ok = True
        except Exception:
            redis_ok = False
        try:
            disk_free = shutil.disk_usage(settings.storage_root).free
        except OSError:
            disk_free = None
        try:
            from .gemini_pool import ProviderStore

            pool_usable = ProviderStore(client).has_usable()
        except Exception:
            pool_usable = False
        return {
            "max_clips_default": live.max_clips,
            "max_clips_limit": MAX_CLIPS_HARD_LIMIT,
            "min_clip_duration": live.min_clip_duration_seconds,
            "max_clip_duration": live.max_clip_duration_seconds,
            "max_source_duration": live.max_source_duration_seconds,
            "whisper_model": live.whisper_model,
            "gemini_model": live.gemini_model,
            "llm_available": bool(live.gemini_api_key) or pool_usable,
            "runtime": runtime_mode.value,
            "runner": runner.kind,
            "system": {
                "redis_connected": redis_ok,
                "storage_root": str(settings.storage_root),
                "storage_free_bytes": disk_free,
                "frontend_origins": allowed_origins,
                "public_mode": settings.public_mode,
                "api_host": settings.api_host,
                "api_port": settings.api_port,
            },
        }

    # -- Application settings (persistent, DB-backed) ----------------------
    @app.get("/api/v1/settings")
    def list_settings():
        return {"settings": AppSettings(client).as_dict()}

    @app.patch("/api/v1/settings")
    def update_settings(payload: SettingsUpdate):
        keys = sorted((payload.settings or {}).keys())
        started = time.monotonic()
        logger.info("settings_update_started: keys=%s", keys)
        try:
            updated = AppSettings(client).set_many(payload.settings or {})
        except ValidationError as exc:
            logger.warning("settings_update_failed: keys=%s error=%s", keys, exc.code)
            raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
        except ConfigurationError as exc:
            logger.warning("settings_update_failed: keys=%s error=%s", keys, exc.code)
            raise HTTPException(status_code=500, detail={"code": exc.code, "message": str(exc)}) from exc
        logger.info(
            "settings_update_success: keys=%s duration_ms=%d",
            sorted(updated), int((time.monotonic() - started) * 1000),
        )
        return {"updated": sorted(updated), "settings": AppSettings(client).as_dict()}

    @app.patch("/api/v1/settings/{key}")
    def update_setting(key: str, payload: SettingValue):
        logger.info("settings_update_started: keys=%s", [key])
        try:
            AppSettings(client).set(key, payload.value)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
        except ConfigurationError as exc:
            raise HTTPException(status_code=500, detail={"code": exc.code, "message": str(exc)}) from exc
        logger.info("settings_update_success: keys=%s", [key])
        return {"updated": [key], "settings": AppSettings(client).as_dict()}

    @app.delete("/api/v1/settings/{key}")
    def reset_setting(key: str):
        logger.info("settings_update_started: keys=%s reset=True", [key])
        try:
            AppSettings(client).reset(key)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
        except ConfigurationError as exc:
            raise HTTPException(status_code=500, detail={"code": exc.code, "message": str(exc)}) from exc
        logger.info("settings_update_success: keys=%s reset=True", [key])
        return {"updated": [key], "settings": AppSettings(client).as_dict()}

    @app.post("/api/v1/settings/test/gemini")
    def test_gemini(payload: GeminiTestRequest):
        # NOTE: a provided key is used once for this test only;
        # it is never stored or logged here.
        from .ai_models import DiscoveryError, get_registry
        from .gemini_service import test_connection

        service = AppSettings(client)
        api_key = (payload.api_key or "").strip() or service.get("GEMINI_API_KEY")
        if not api_key:
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "API key belum dikonfigurasi"})
        try:
            timeout = int(service.get("GEMINI_TIMEOUT_SECONDS"))
        except Exception:
            timeout = 30
        try:
            result = test_connection(api_key, service.get("GEMINI_MODEL"), timeout)
        except ImportError as exc:
            raise HTTPException(status_code=503, detail={"code": "INVALID_CONFIGURATION", "message": "Library client Gemini belum terinstal"}) from exc
        if not result.ok:
            raise HTTPException(
                status_code=502,
                detail={"code": result.error_code or "EXTERNAL_COMMAND_FAILED", "message": result.message},
            )
        # Discovery report: reachable key -> which models does THIS key see?
        # Best effort only; a discovery hiccup must not fail a good connection.
        discovery: dict[str, Any] = {"status": "skipped"}
        try:
            snap = get_registry(client, api_key, timeout)
            counts = snap.counts()
            discovery = {
                "status": "ok" if snap.source in ("live", "cache", "cache-stale") else "error",
                "source": snap.source,
                "models": counts["discovered"],
                "compatible": counts["compatible"],
                "free_tier": counts["free_tier"],
            }
        except DiscoveryError as exc:
            discovery = {"status": "error", "kind": exc.kind}
        except Exception:
            logger.warning("gemini_test_discovery_failed", exc_info=True)
        return {
            "ok": True,
            "success": True,
            "model": result.model,
            "latency_ms": result.latency_ms,
            "message": result.message,
            "discovery": discovery,
        }

    # -- Gemini model discovery / registry (backend-driven, server-side) --
    def _ai_snapshot(force_refresh: bool) -> Any:
        from .ai_models import get_registry

        service = AppSettings(client)
        api_key = str(service.get("GEMINI_API_KEY") or "")
        if not api_key:
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "API key belum dikonfigurasi"})
        try:
            timeout = int(service.get("GEMINI_TIMEOUT_SECONDS"))
        except Exception:
            timeout = 30
        try:
            ttl = int(service.get("GEMINI_MODEL_DISCOVERY_TTL"))
        except Exception:
            ttl = None
        return get_registry(client, api_key, timeout, force_refresh=force_refresh, ttl=ttl)

    @app.get("/api/v1/settings/ai/models")
    def list_ai_models():
        """Backend-driven model list. Never includes the API key."""
        snap = _ai_snapshot(False)
        if snap.source == "error":
            raise HTTPException(status_code=502, detail={
                "code": (snap.error or {}).get("code", "MODEL_DISCOVERY_FAILED"),
                "message": (snap.error or {}).get("message", "Discovery model gagal"),
            })
        return {
            "provider": "gemini",
            "status": "ok" if snap.source in ("live", "cache") else "stale",
            "source": snap.source,
            "discovered_at": snap.discovered_at,
            "expires_at": snap.expires_at,
            "counts": snap.counts(),
            "models": [m.to_dict() for m in snap.models],
        }

    @app.post("/api/v1/settings/ai/models/refresh")
    def refresh_ai_models():
        """Force Google Models API discovery; revalidate user chain (AM)."""
        from .ai_models import describe_selection, get_registry, prefs_from_service

        try:
            snap = _ai_snapshot(True)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"code": "MODEL_DISCOVERY_FAILED", "message": str(exc)}) from exc
        if snap.source == "error":
            raise HTTPException(status_code=502, detail={
                "code": (snap.error or {}).get("code", "MODEL_DISCOVERY_FAILED"),
                "message": (snap.error or {}).get("message", "Discovery model gagal"),
            })
        service = AppSettings(client)
        selection = describe_selection(prefs_from_service(service), snap)
        body = snap.counts()
        body.update({
            "provider": "gemini",
            "source": snap.source,
            "discovered_at": snap.discovered_at,
            "expires_at": snap.expires_at,
            "selection": selection,
        })
        return body

    @app.get("/api/v1/settings/ai/selection")
    def get_ai_selection():
        """Effective chain (automatic policy or validated manual prefs)."""
        from .ai_models import cached_snapshot, describe_selection, prefs_from_service

        service = AppSettings(client)
        prefs = prefs_from_service(service)
        api_key = str(service.get("GEMINI_API_KEY") or "")
        snap = cached_snapshot(client, api_key) if api_key else None
        return {
            "provider": "gemini",
            "prefs": prefs.to_dict(),
            "selection": describe_selection(prefs, snap),
            "registry": {"source": snap.source if snap else "none",
                         "counts": snap.counts() if snap else {}},
        }

    # -- Gemini provider pool (multi key x multi model) --------------------
    @app.get("/api/v1/settings/ai/selection")
    def get_ai_selection():
        """Effective chain (automatic policy or validated manual prefs)."""
        from .ai_models import cached_snapshot, describe_selection, prefs_from_service

        service = AppSettings(client)
        prefs = prefs_from_service(service)
        api_key = str(service.get("GEMINI_API_KEY") or "")
        snap = cached_snapshot(client, api_key) if api_key else None
        return {
            "provider": "gemini",
            "prefs": prefs.to_dict(),
            "selection": describe_selection(prefs, snap),
            "registry": {"source": snap.source if snap else "none",
                         "counts": snap.counts() if snap else {}},
        }

    def _pool_store():
        from .gemini_pool import ProviderStore

        return ProviderStore(client)

    def _pool_health():
        from .gemini_pool import HealthManager, pool_config_from_service

        return HealthManager(client, pool_config_from_service(AppSettings(client)))

    def _provider_payload(provider, health=None):
        from .gemini_pool import UNKNOWN, HealthManager, pool_config_from_service

        health = health or HealthManager(client, pool_config_from_service(AppSettings(client)))
        data = provider.masked()
        data["health"] = asdict_health(health.snapshot(provider.id))
        data["success_rate"] = health.success_rate(provider.id)
        models_health = {}
        for model in provider.models:
            snap = health.snapshot(provider.id, model.id)
            if snap.state != UNKNOWN or snap.failures or snap.successes:
                models_health[model.id] = asdict_health(snap)
        data["models_health"] = models_health
        return data

    def asdict_health(snap):
        return {
            "state": snap.state,
            "failures": snap.failures,
            "successes": snap.successes,
            "cooldown_until": snap.cooldown_until,
            "circuit": snap.circuit,
            "last_success": snap.last_success,
            "last_error": snap.last_error,
            "last_error_category": snap.last_error_category,
        }

    def _validate_provider_body(body: ProviderUpsert, *, is_create: bool):
        import re

        if body.name is not None and not body.name.strip():
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "Nama wajib diisi"})
        if is_create and not (body.api_key or "").strip():
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "API Key wajib diisi"})
        if body.priority is not None and body.priority < 1:
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "Prioritas minimal 1"})
        if body.model_mode is not None and body.model_mode not in ("manual", "automatic"):
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "model_mode harus manual atau automatic"})
        if body.auto_depth is not None and not 1 <= body.auto_depth <= 10:
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "auto_depth harus 1..10"})
        if body.models is not None:
            if len(body.models) > 10:
                raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "Maksimal 10 model per provider"})
            seen: set[str] = set()
            for index, model in enumerate(body.models):
                mid = (model.id or "").strip()
                if not mid:
                    raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "ID model wajib diisi"})
                if mid in seen:
                    raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": f"Model duplikat: {mid}"})
                seen.add(mid)
                if model.priority < 1:
                    raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "Prioritas model minimal 1"})
                if not re.match(r"^[\w][\w.\-]*$", mid):
                    raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": f"ID model tidak valid: {mid}"})

    @app.get("/api/v1/settings/ai/providers")
    def list_providers():
        """Masked providers + runtime health. Never includes API keys."""
        from .gemini_pool import HealthManager, pool_config_from_service

        store = _pool_store()
        health = HealthManager(client, pool_config_from_service(AppSettings(client)))
        providers = store.list()
        if not providers:
            providers = store.migrate_legacy(AppSettings(client))
        return {"providers": [_provider_payload(p, health) for p in providers]}

    @app.post("/api/v1/settings/ai/providers", status_code=201)
    def add_provider(body: ProviderUpsert):
        from .gemini_pool import GeminiProvider, ProviderModel

        _validate_provider_body(body, is_create=True)
        store = _pool_store()
        existing = store.list()
        used = {p.id for p in existing}
        suffix = 1
        while f"gemini_api_{suffix}" in used:
            suffix += 1
        models = [ProviderModel(id=m.id.strip(), enabled=m.enabled, priority=m.priority if m.priority >= 1 else i + 1)
                  for i, m in enumerate(body.models or [])]
        provider = GeminiProvider(
            id=f"gemini_api_{suffix}",
            name=(body.name or "").strip() or f"Gemini API {suffix}",
            enabled=body.enabled if body.enabled is not None else True,
            priority=body.priority if body.priority and body.priority >= 1 else (max([p.priority for p in existing], default=0) + 1),
            api_key=(body.api_key or "").strip(),
            models=models,
            model_mode=body.model_mode or "manual",
            auto_depth=body.auto_depth or 4,
            auto_free_tier_only=True if body.auto_free_tier_only is None else body.auto_free_tier_only,
        )
        try:
            store.save(provider)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail={"code": "EXTERNAL_COMMAND_FAILED", "message": str(exc)}) from exc
        return _provider_payload(provider)

    @app.patch("/api/v1/settings/ai/providers/{provider_id}")
    def edit_provider(provider_id: str, body: ProviderUpsert):
        from .gemini_pool import ProviderModel

        _validate_provider_body(body, is_create=False)
        store = _pool_store()
        provider = store.get(provider_id)
        if provider is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Provider tidak ditemukan"})
        if body.name is not None:
            provider.name = body.name.strip() or provider.name
        if body.enabled is not None:
            provider.enabled = body.enabled
        if body.priority is not None and body.priority >= 1:
            provider.priority = body.priority
        if (body.api_key or "").strip():
            provider.api_key = body.api_key.strip()
            # New key: drop stale unavailability marks + discovery cache.
            provider.unavailable_models = []
            try:
                from .ai_models import invalidate_registry

                invalidate_registry(client, provider.api_key)
            except Exception:
                pass
        if body.models is not None:
            kept_unavailable = set(provider.unavailable_models)
            provider.models = [ProviderModel(id=m.id.strip(), enabled=m.enabled, priority=m.priority)
                               for m in body.models]
            provider.unavailable_models = [m for m in kept_unavailable
                                           if m in {m.id for m in provider.models}]
        if body.model_mode is not None:
            provider.model_mode = body.model_mode
        if body.auto_depth is not None:
            provider.auto_depth = body.auto_depth
        if body.auto_free_tier_only is not None:
            provider.auto_free_tier_only = body.auto_free_tier_only
        store.save(provider)
        return _provider_payload(provider)

    @app.delete("/api/v1/settings/ai/providers/{provider_id}")
    def delete_provider(provider_id: str):
        store = _pool_store()
        if store.get(provider_id) is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Provider tidak ditemukan"})
        if len(store.list()) <= 1:
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "Provider terakhir tidak bisa dihapus"})
        store.delete(provider_id)
        _pool_health().reset(provider_id)
        return {"deleted": provider_id}

    @app.post("/api/v1/settings/ai/providers/reorder")
    def reorder_providers(body: ProviderReorder):
        store = _pool_store()
        providers = {p.id: p for p in store.list()}
        if set(body.ids) != set(providers):
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "ids harus persis sama dengan provider yang ada"})
        for index, pid in enumerate(body.ids):
            providers[pid].priority = index + 1
            store.save(providers[pid])
        return {"order": body.ids}

    @app.post("/api/v1/settings/ai/providers/{provider_id}/test")
    def test_provider(provider_id: str):
        """Validate key + models.list discovery + one generateContent model."""
        from .ai_models import get_registry
        from .gemini_service import test_connection

        store = _pool_store()
        provider = store.get(provider_id)
        if provider is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Provider tidak ditemukan"})
        if not provider.api_key.strip():
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "Provider belum punya API key"})
        service = AppSettings(client)
        try:
            timeout = int(service.get("GEMINI_TIMEOUT_SECONDS"))
        except Exception:
            timeout = 30
        ordered = provider.ordered_models()
        model = ordered[0].id if ordered else ""
        result = test_connection(provider.api_key, model, timeout)
        if not result.ok:
            raise HTTPException(status_code=502, detail={"code": result.error_code or "EXTERNAL_COMMAND_FAILED", "message": result.message})
        discovery: dict[str, Any] = {"status": "skipped"}
        try:
            snap = get_registry(client, provider.api_key, timeout)
            counts = snap.counts()
            discovery = {"status": "ok" if snap.source in ("live", "cache", "cache-stale") else "error",
                         "source": snap.source, "models": counts["discovered"],
                         "compatible": counts["compatible"], "free_tier": counts["free_tier"]}
        except Exception:
            logger.warning("provider_test_discovery_failed", exc_info=True)
        return {"ok": True, "model": result.model, "latency_ms": result.latency_ms,
                "message": result.message, "discovery": discovery}

    @app.post("/api/v1/settings/ai/providers/{provider_id}/test-model")
    def test_provider_model(provider_id: str, body: ProviderTestModel):
        """Minimal safe generation ("Reply with OK") on the chosen model."""
        from .gemini_service import test_connection

        store = _pool_store()
        provider = store.get(provider_id)
        if provider is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Provider tidak ditemukan"})
        if not (body.model or "").strip():
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "Model wajib diisi"})
        service = AppSettings(client)
        try:
            timeout = int(service.get("GEMINI_TIMEOUT_SECONDS"))
        except Exception:
            timeout = 30
        result = test_connection(provider.api_key, body.model.strip(), timeout)
        if not result.ok:
            raise HTTPException(status_code=502, detail={"code": result.error_code or "EXTERNAL_COMMAND_FAILED", "message": result.message})
        return {"ok": True, "provider_id": provider_id, "model": result.model,
                "latency_ms": result.latency_ms}

    @app.post("/api/v1/settings/ai/providers/test-fallback")
    def test_fallback_chain(body: FallbackChainTestBody):
        """Live fallback-chain proof with minimal quota.

        Runs one tiny prompt ("Reply with exactly: OK") through the REAL
        dispatcher. Providers in `fail_provider_ids` fail synthetically
        (zero quota); every other provider receives a REAL request until
        the first success. Proves P2..P5 are actually reachable when P1
        fails. Never returns API keys.
        """
        import dataclasses

        from .gemini_pool import (
            HealthManager,
            PoolExhausted,
            default_transport,
            generate_via_pool,
            order_providers,
            pool_config_from_service,
        )

        store = _pool_store()
        service = AppSettings(client)
        enabled = [p for p in store.list() if p.enabled and p.api_key.strip()]
        if not enabled:
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "Tidak ada provider aktif"})
        cap = body.max_providers if body.max_providers and body.max_providers > 0 else len(enabled)
        ordered_all = sorted(enabled, key=lambda p: (p.priority, p.id))[:cap]
        fail_set = {pid for pid in (body.fail_provider_ids or []) if pid}
        try:
            timeout = int(service.get("GEMINI_TIMEOUT_SECONDS"))
        except Exception:
            timeout = 30
        config = pool_config_from_service(service)
        # Fail fast between pairs in diagnostics: prove the CHAIN, not retries.
        config = dataclasses.replace(config, retry_max_attempts=0)
        health = HealthManager(client, config)

        class _SimulatedFailure(Exception):
            def __init__(self, provider_id: str):
                super().__init__(f"simulated 503 for {provider_id}")
                self.code = 503

        def transport(provider_id: str, model_id: str, attempt: int):
            if provider_id in fail_set:
                raise _SimulatedFailure(provider_id)
            provider = next((p for p in ordered_all if p.id == provider_id), None)
            if provider is None:
                raise _SimulatedFailure(provider_id)
            return default_transport(
                provider.api_key, model_id, "Reply with exactly: OK",
                config.request_timeout_ms, temperature=0.0, max_output_tokens=16)

        ordered = order_providers(ordered_all, health, config.strategy, client)
        try:
            _response, meta = generate_via_pool(
                ordered_all, task_type="curation", contents="Reply with exactly: OK",
                config=config, health=health, job_id=None,
                temperature=0.0, max_output_tokens=16, transport=transport,
            )
        except PoolExhausted as exc:
            raise HTTPException(status_code=502, detail={
                "code": "FALLBACK_CHAIN_EXHAUSTED",
                "message": "Semua provider gagal",
                "ordered_providers": [p.id for p in ordered],
                "failure_history": exc.failures,
            }) from exc
        return {
            "ok": True,
            "success_provider": meta.get("provider_id"),
            "success_model": meta.get("model_used"),
            "attempts_total": meta.get("attempts_total"),
            "fallback_used": meta.get("fallback_used"),
            "ordered_providers": [p.id for p in ordered],
            "simulated_failures": sorted(fail_set),
            "failure_history": meta.get("failure_history") or [],
        }

    @app.get("/api/v1/settings/ai/providers/{provider_id}/models")
    def list_provider_models(provider_id: str):
        """Discovery for one provider key. Never includes the API key."""
        from .ai_models import get_registry

        store = _pool_store()
        provider = store.get(provider_id)
        if provider is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Provider tidak ditemukan"})
        service = AppSettings(client)
        try:
            timeout = int(service.get("GEMINI_TIMEOUT_SECONDS"))
        except Exception:
            timeout = 30
        try:
            ttl = int(service.get("GEMINI_MODEL_DISCOVERY_TTL"))
        except Exception:
            ttl = None
        snap = get_registry(client, provider.api_key, timeout, ttl=ttl)
        if snap.source == "error":
            raise HTTPException(status_code=502, detail={
                "code": (snap.error or {}).get("code", "MODEL_DISCOVERY_FAILED"),
                "message": (snap.error or {}).get("message", "Discovery model gagal")})
        configured = {m.id for m in provider.models}
        return {"provider_id": provider_id,
                "status": "ok" if snap.source in ("live", "cache") else "stale",
                "source": snap.source, "counts": snap.counts(),
                "models": [{**m.to_dict(), "configured": m.id in configured,
                            "marked_unavailable": m.id in set(provider.unavailable_models)}
                           for m in snap.models]}

    @app.post("/api/v1/settings/ai/providers/{provider_id}/models/refresh")
    def refresh_provider_models(provider_id: str):
        """Force discovery; preserve config, mark removed models unavailable."""
        from .ai_models import get_registry

        store = _pool_store()
        provider = store.get(provider_id)
        if provider is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Provider tidak ditemukan"})
        service = AppSettings(client)
        try:
            timeout = int(service.get("GEMINI_TIMEOUT_SECONDS"))
        except Exception:
            timeout = 30
        try:
            ttl = int(service.get("GEMINI_MODEL_DISCOVERY_TTL"))
        except Exception:
            ttl = None
        snap = get_registry(client, provider.api_key, timeout, force_refresh=True, ttl=ttl)
        if snap.source == "error":
            raise HTTPException(status_code=502, detail={
                "code": (snap.error or {}).get("code", "MODEL_DISCOVERY_FAILED"),
                "message": (snap.error or {}).get("message", "Discovery model gagal")})
        discovered = {m.id for m in snap.models if m.available}
        removed = [m.id for m in provider.models if m.id not in discovered]
        provider.unavailable_models = sorted(set(provider.unavailable_models) | set(removed))
        store.save(provider)
        body = snap.counts()
        body.update({"provider_id": provider_id, "source": snap.source,
                     "removed_marked_unavailable": removed})
        return body

    @app.get("/api/v1/settings/ai/providers/health")
    def list_providers_health():
        """Runtime health dashboard (no secrets)."""
        store = _pool_store()
        health = _pool_health()
        return {"providers": [_provider_payload(p, health) for p in store.list()],
                "metrics": METRICS_SNAPSHOT()}

    def METRICS_SNAPSHOT():
        from .gemini_pool import METRICS

        return METRICS.snapshot()

    @app.post("/api/v1/settings/ai/providers/health/reset")
    def reset_providers_health(body: HealthResetBody):
        _pool_health().reset(body.provider_id)
        return {"reset": body.provider_id or "all"}

    @app.post("/api/v1/jobs", status_code=202)
    def create_job(
        payload: CreateJobRequest,
        response: FastAPIResponse,
        idempotency_key: str | None = Header(default=None),
    ):
        try:
            source_url = validate_youtube_url(payload.source_url)
            server = Settings.resolve(client)
            max_clips = payload.max_clips if payload.max_clips is not None else server.max_clips
            min_duration = payload.min_duration if payload.min_duration is not None else server.min_clip_duration_seconds
            max_duration = payload.max_duration if payload.max_duration is not None else server.max_clip_duration_seconds
            if min_duration > max_duration:
                raise ValidationError("min_duration exceeds max_duration")
            fingerprint = repository.request_fingerprint(
                source_url, payload.language,
                max_clips, min_duration, max_duration,
                payload.burn_subtitles,
                payload.subtitle_style, payload.subtitle_position,
                getattr(server, "transcript_source", "youtube_first"),
            )
            video_id = extract_video_id(source_url)
            video_snapshot = None
            if video_id:
                try:
                    cached = VideoStore(client).get(video_id)
                    if cached:
                        video_snapshot = cached.snapshot()
                except Exception:
                    video_snapshot = None
            now = time.time()
            record = JobRecord(
                new_job_id(), source_url, payload.language, created_at=now, updated_at=now,
                max_clips=max_clips, min_duration=min_duration,
                max_duration=max_duration, total_clips=max_clips,
                burn_subtitles=payload.burn_subtitles,
                subtitle_style=payload.subtitle_style,
                subtitle_position=payload.subtitle_position,
                video_id=video_id, video=video_snapshot,
            )
            record, existing = repository.create(record, idempotency_key, fingerprint)
            if existing:
                response.status_code = 200
                return public_job(record)
            try:
                if is_desktop():
                    # Local runner: same submit contract (job_id -> task id),
                    # executed on a background thread in this process.
                    record.worker_task_id = runner.submit(record.job_id)
                else:
                    from .worker import celery_task
                    _, task = celery_task()
                    async_result = task.delay(record.job_id)
                    # Persist the REAL Celery task id: revoke-by-job-id is a
                    # no-op (task ids are random UUIDs), so cancel needs this.
                    record.worker_task_id = getattr(async_result, "id", None)
                repository.save(record)
                repository.transition(record.job_id, "queued", "enqueueing", 0)
            except ImportError:
                repository.transition(record.job_id, "failed", "enqueueing", 0)
                record = repository.get(record.job_id)
                if record:
                    record.error = {"code": "QUEUE_UNAVAILABLE", "message": "Celery is not installed", "retryable": False}
                    repository.save(record)
            return public_job(repository.get(record.job_id))
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc

    @app.get("/api/v1/jobs")
    def list_jobs(limit: int = 50):
        return {"jobs": [public_job(record) for record in repository.list(limit=max(1, min(limit, 100)))]}

    def _enqueue_refresh(job_id: str) -> bool:
        try:
            if is_desktop():
                runner.submit(job_id, task_name="refresh_metadata")
                return True
            from .worker import refresh_task
            _, task = refresh_task()
            task.delay(job_id)
            return True
        except Exception:
            return False

    @app.get("/api/v1/jobs/search")
    def search_jobs(
        q: str = "",
        status: str = "all",
        channel: str = "",
        date_from: float = 0,
        date_to: float = 0,
        sort: str = "newest",
        page: int = 1,
        per_page: int = 20,
    ):
        """Server-side search/filter/sort/pagination over stored job records."""
        per_page = max(1, min(per_page, 100))
        page = max(1, page)
        needle = q.strip().lower()
        wanted_channel = channel.strip().lower()
        records = repository.list(limit=10000)
        videos = VideoStore(client)

        def video_of(record: JobRecord) -> dict:
            if record.video:
                return record.video
            if record.video_id:
                cached = videos.get(record.video_id)
                if cached:
                    return cached.snapshot()
            return {}

        matched: list[tuple[JobRecord, dict]] = []
        for record in records:
            video = video_of(record)
            if status != "all":
                if status == "processing":
                    if record.status not in {"accepted", "queued", "running", "retrying"}:
                        continue
                elif record.status != status:
                    continue
            if wanted_channel and (video.get("channel_name") or "").lower() != wanted_channel:
                continue
            created = record.created_at or 0
            if date_from and created < date_from:
                continue
            if date_to and created > date_to:
                continue
            if needle:
                haystack = " ".join([
                    video.get("title") or "",
                    video.get("channel_name") or "",
                    record.source_url or "",
                    video.get("youtube_video_id") or "",
                    record.video_id or "",
                    record.job_id or "",
                ]).lower()
                if needle not in haystack:
                    continue
            matched.append((record, video))

        if sort in {"title_az", "title_za"}:
            matched.sort(
                key=lambda item: ((item[1].get("title") or item[0].job_id).lower()),
                reverse=(sort == "title_za"),
            )
        elif sort == "oldest":
            matched.sort(key=lambda item: (item[0].created_at or 0))
        elif sort == "updated":
            matched.sort(key=lambda item: (item[0].updated_at or item[0].created_at or 0), reverse=True)
        else:  # newest (default)
            matched.sort(key=lambda item: (item[0].created_at or 0), reverse=True)

        total = len(matched)
        start = (page - 1) * per_page
        return {
            "jobs": [public_job(record) for record, _ in matched[start:start + per_page]],
            "total": total,
            "page": page,
            "per_page": per_page,
        }

    @app.get("/api/v1/channels")
    def list_channels():
        """Distinct channel names with job counts (from stored metadata)."""
        videos = VideoStore(client)
        counts: dict[str, int] = {}
        for record in repository.list(limit=10000):
            name = (record.video or {}).get("channel_name")
            if not name and record.video_id:
                cached = videos.get(record.video_id)
                name = cached.channel_name if cached else None
            name = (name or "").strip()
            if not name:
                continue
            counts[name] = counts.get(name, 0) + 1
        return {
            "channels": [
                {"name": name, "jobs": count}
                for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
            ]
        }

    @app.get("/api/v1/videos")
    def list_videos():
        """Distinct videos with job counts. No YouTube refetch."""
        videos = VideoStore(client)
        by_video: dict[str, dict] = {}
        for record in repository.list(limit=10000):
            video_id = record.video_id
            if not video_id:
                continue
            entry = by_video.setdefault(video_id, {"video": None, "jobs": 0, "job_ids": []})
            entry["jobs"] += 1
            entry["job_ids"].append(record.job_id)
            if record.video and record.video.get("title") and not (entry["video"] or {}).get("title"):
                entry["video"] = record.video
        for video_id, entry in by_video.items():
            if not entry["video"]:
                cached = videos.get(video_id)
                entry["video"] = cached.snapshot() if cached else {"youtube_video_id": video_id}
        return {"videos": sorted(by_video.values(), key=lambda e: -e["jobs"])}

    @app.post("/api/v1/jobs/{job_id}/refresh-metadata", status_code=202)
    def refresh_metadata(job_id: str):
        record = repository.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if not _enqueue_refresh(job_id):
            raise HTTPException(status_code=503, detail="Queue unavailable")
        return {"job_id": job_id, "queued": True}

    @app.post("/api/v1/videos/backfill", status_code=202)
    def backfill_videos(limit: int = 20):
        """Enqueue metadata refresh for jobs missing titles. Never refetches filled ones."""
        videos = VideoStore(client)
        queued = 0
        skipped = 0
        for record in repository.list(limit=10000):
            if queued >= max(1, min(limit, 50)):
                break
            if record.video and record.video.get("title"):
                continue
            video_id = record.video_id
            if video_id:
                cached = videos.get(video_id)
                if cached and cached.title:
                    record.video_id = video_id
                    record.video = cached.snapshot()
                    repository.save(record)
                    skipped += 1
                    continue
            if _enqueue_refresh(record.job_id):
                queued += 1
        return {"queued": queued, "filled_from_cache": skipped}

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str):
        record = repository.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return public_job(record)

    @app.get("/api/v1/jobs/{job_id}/results")
    def results(job_id: str):
        record = repository.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if record.status not in {"completed", "partial"}:
            raise HTTPException(status_code=409, detail="Job is not complete")
        return {"job_id": job_id, "clips": public_job(record)["clips"]}

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        record = repository.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Job not found")
        # Stop the worker ASAP (Celery revoke on server, cancel event on
        # desktop); the pipeline also polls the cancelled flag between
        # clips, so both paths converge on the record status below.
        # NOTE: revoke needs the WORKER task UUID, not the job id
        # (task.delay generates random ids; revoking the job id is a no-op).
        try:
            if is_desktop():
                runner.revoke(record.worker_task_id or job_id)
            else:
                from .worker import celery_task
                app_celery, _ = celery_task()
                app_celery.control.revoke(record.worker_task_id or job_id, terminate=True)
        except Exception:
            pass
        repository.transition(job_id, "cancelled", "cancelled", record.progress)
        return public_job(repository.get(job_id))

    @app.get("/api/v1/jobs/{job_id}/recreate-preview")
    def recreate_preview(job_id: str):
        """Lightweight info for the Recreate dialog: is a transcript available?"""
        record = repository.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Job not found")
        has_artifact = False
        try:
            candidate = settings.storage_root / "jobs" / job_id / "source" / "transcript.json"
            has_artifact = candidate.is_file()
        except (OSError, ValueError):
            has_artifact = False
        cache_hit = False
        try:
            from .transcript_cache import TranscriptCache

            server = Settings.resolve(client)
            cache_hit = TranscriptCache(client, settings.storage_root).lookup(
                record.source_url,
                str(server.whisper_model),
                record.language,
                (record.video or {}).get("duration"),
            )[2] == "hit"
        except Exception:
            cache_hit = False
        return {
            "job_id": job_id,
            "source_url": record.source_url,
            "video": record.video,
            "status": record.status,
            "has_transcript_artifact": has_artifact,
            "cache_hit": cache_hit,
            "transcript_available": bool(has_artifact or cache_hit),
        }

    @app.post("/api/v1/jobs/{job_id}/recreate", status_code=202)
    def recreate(
        job_id: str,
        payload: RecreateJobRequest | None = None,
        idempotency_key: str | None = Header(default=None),
    ):
        """Create a NEW job from an existing one: reuse source + transcript,
        SKIP Whisper when a compatible transcript exists, RUN curation again.
        The parent job is never modified. A still-active recreate child of
        the same parent is returned instead of duplicating (double-click safe)."""
        from .jobs import TERMINAL_STATES as _TERMINAL

        parent = repository.get(job_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="Job not found")
        force = bool(payload.force_retranscribe) if payload else False
        for candidate in repository.list(limit=10000):
            if (
                candidate.recreated_from_job_id == job_id
                and candidate.status not in _TERMINAL
                and candidate.force_retranscribe == force
            ):
                logger.info(
                    "[JOB_RECREATE] duplicate blocked parent_job=%s existing_job=%s",
                    job_id, candidate.job_id,
                )
                return public_job(candidate)
        server = Settings.resolve(client)
        fingerprint = repository.request_fingerprint(
            parent.source_url, parent.language,
            parent.max_clips, parent.min_duration, parent.max_duration,
            parent.burn_subtitles,
            parent.subtitle_style, parent.subtitle_position,
            getattr(server, "transcript_source", "youtube_first"),
        )
        now = time.time()
        record = JobRecord(
            new_job_id(), parent.source_url, parent.language, created_at=now, updated_at=now,
            max_clips=parent.max_clips, min_duration=parent.min_duration,
            max_duration=parent.max_duration, total_clips=parent.max_clips,
            burn_subtitles=parent.burn_subtitles,
            subtitle_style=parent.subtitle_style,
            subtitle_position=parent.subtitle_position,
            video_id=parent.video_id, video=parent.video,
            recreated_from_job_id=job_id,
            force_retranscribe=force,
        )
        record, existing = repository.create(record, idempotency_key, fingerprint)
        if existing:
            return public_job(record)
        logger.info(
            "[JOB_RECREATE] requested parent_job=%s new_job=%s force_retranscribe=%s",
            job_id, record.job_id, force,
        )
        try:
            if is_desktop():
                record.worker_task_id = runner.submit(record.job_id)
            else:
                from .worker import celery_task
                _, task = celery_task()
                async_result = task.delay(record.job_id)
                record.worker_task_id = getattr(async_result, "id", None)
            repository.save(record)
            repository.transition(record.job_id, "queued", "enqueueing", 0)
            logger.info("[JOB_RECREATE] curation_started new_job=%s", record.job_id)
        except ImportError:
            repository.transition(record.job_id, "failed", "enqueueing", 0)
            record = repository.get(record.job_id)
            if record:
                record.error = {"code": "QUEUE_UNAVAILABLE", "message": "Celery is not installed", "retryable": False}
                repository.save(record)
        return public_job(repository.get(record.job_id))

    @app.get("/api/v1/transcripts/stats")
    def transcript_stats():
        """Shared Whisper transcript cache statistics (ops visibility)."""
        try:
            from .transcript_cache import TranscriptCache

            return TranscriptCache(client, settings.storage_root).stats()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/v1/vision/talknet/status")
    def talknet_status():
        """TalkNet escalation status: config, device, model, load state.

        Never loads the model (loading happens on first escalation only)
        and never returns keys or paths outside the storage volume.
        """
        from .talknet.config import TalkNetConfig
        from .talknet.device import resolve_device
        from .talknet import model as model_mod

        try:
            config = TalkNetConfig.resolve(client)
        except Exception:
            config = TalkNetConfig.from_env()
        if not config.enabled:
            return {"enabled": False, "status": "DISABLED",
                    "device_request": config.device,
                    "resource_mode": config.resource_mode}
        info = resolve_device(config.device)
        try:
            from .talknet.model import resolve_model_path

            path = resolve_model_path(config.model_path)
        except Exception:
            path = None
        loaded = bool(getattr(model_mod, "_REGISTRY", None) or {})
        if path is None:
            status = "MODEL_MISSING"
        elif loaded:
            status = "READY"
        else:
            status = "STANDBY"
        worker_report: dict | None = None
        try:
            raw = client.hgetall("gaeclip:talknet:worker")
            if raw:
                worker_report = {
                    (k.decode() if isinstance(k, bytes) else str(k)):
                    (v.decode() if isinstance(v, bytes) else str(v))
                    for k, v in raw.items()
                }
        except Exception:
            worker_report = None
        return {
            "enabled": True,
            # Local (API container) view: device/model presence here.
            "status": status,
            "device_request": config.device,
            "resource_mode": config.resource_mode,
            "device": info.device,
            "backend": info.backend,
            "device_name": info.name,
            "device_reason": info.reason,
            "memory_total": info.memory_total,
            "memory_free": info.memory_free,
            "model_found": path is not None,
            "model_loaded": loaded,
            # Worker view (where inference actually runs), reported by
            # the worker itself after TalkNet activity. Null until then.
            "worker": worker_report,
        }

    @app.get("/api/v1/settings/youtube-cookies")
    def get_youtube_cookies():
        """Whether a Settings-managed cookies.txt exists (both editions).

        Server operators using YTDLP_COOKIES_FILE keep working untouched;
        this only reports/adds the managed fallback file.
        """
        from .runtime.cookies import resolve_cookies_file

        resolved = resolve_cookies_file(settings.storage_root)
        return {"configured": resolved is not None}

    @app.post("/api/v1/settings/youtube-cookies")
    def save_youtube_cookies(body: YoutubeCookiesBody):
        from .runtime.cookies import save_managed_cookies

        try:
            save_managed_cookies(body.cookies_txt, settings.storage_root)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": str(exc)})
        return {"saved": True}

    @app.delete("/api/v1/jobs/{job_id}", status_code=204)
    def delete(job_id: str):
        record = repository.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if record.status not in TERMINAL_STATES:
            raise HTTPException(status_code=409, detail="Only terminal jobs can be deleted")
        try:
            shutil.rmtree(settings.storage_root / "jobs" / job_id, ignore_errors=False)
        except FileNotFoundError:
            # Orphan record: Redis punya job tapi folder storage sudah hilang
            # (manual rm / volume reset / STORAGE_ROOT pindah). Tetap hapus
            # record agar tidak stuck 500 + tanpa header CORS yang di browser
            # muncul sebagai opaque "Failed to fetch".
            logger.warning("delete_job_missing_dir: job_id=%s", job_id)
        repository.delete(job_id)

    @app.get("/api/v1/jobs/{job_id}/clips/{clip_id}")
    def download_clip(job_id: str, clip_id: str):
        record = repository.get(job_id)
        if record is None or record.status not in {"completed", "partial"}:
            raise HTTPException(status_code=404, detail="Clip not found")
        clip = next((item for item in record.clips or [] if item["clip_id"] == clip_id), None)
        if not clip:
            raise HTTPException(status_code=404, detail="Clip not found")
        path = Path(clip["path"]).resolve()
        root = (settings.storage_root / "jobs").resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(status_code=404, detail="Clip not found")
        return FileResponse(path, media_type="video/mp4", filename=f"{clip_id}.mp4")

    return app
