from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigurationError


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


@dataclass(frozen=True)
class Settings:
    storage_root: Path
    api_host: str
    api_port: int
    max_source_duration_seconds: int
    max_clips: int
    min_clip_duration_seconds: int
    max_clip_duration_seconds: int
    clip_buffer_seconds: float
    max_transcript_chars: int
    max_artifact_bytes: int
    max_job_seconds: int
    max_llm_output_tokens: int
    max_llm_input_chars: int
    max_concurrent_jobs: int
    gemini_api_key: str
    gemini_model: str
    gemini_timeout_seconds: int
    ai_model_mode: str
    ai_primary_model: str
    ai_fallback_models: list
    ai_free_tier_only: bool
    whisper_model: str
    render_profile: str
    public_mode: bool
    transcript_source: str = "youtube_first"
    api_auth_token: str = ""
    job_rate_limit_per_minute: int = 60
    audit_max_bytes: int = 50 * 1024 * 1024
    signed_url_secret: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        """Environment-only resolution (CLI, tests, offline use)."""
        return cls.resolve(None)

    @classmethod
    def resolve(cls, client: Any | None) -> "Settings":
        """Resolve through the central settings service.

        With a Redis client: persistent DB value -> environment -> default.
        Without one (client=None): environment -> default. Infrastructure
        fields (storage, host, public mode) always come from the environment.
        """
        from .ai_models import parse_fallbacks
        from .app_settings import AppSettings

        service = AppSettings(client)
        values = cls(
            storage_root=Path(os.getenv("STORAGE_ROOT", "./storage")).expanduser(),
            api_host=os.getenv("API_HOST", "127.0.0.1"),
            api_port=_int("API_PORT", 8000),
            max_source_duration_seconds=int(service.get("MAX_SOURCE_DURATION_SECONDS")),
            max_clips=int(service.get("MAX_CLIPS")),
            min_clip_duration_seconds=int(service.get("MIN_CLIP_DURATION_SECONDS")),
            max_clip_duration_seconds=int(service.get("MAX_CLIP_DURATION_SECONDS")),
            clip_buffer_seconds=float(service.get("CLIP_BUFFER_SECONDS")),
            max_transcript_chars=int(service.get("MAX_TRANSCRIPT_CHARS")),
            max_artifact_bytes=int(service.get("MAX_ARTIFACT_BYTES")),
            max_job_seconds=int(service.get("MAX_JOB_SECONDS")),
            max_llm_output_tokens=int(service.get("MAX_LLM_OUTPUT_TOKENS")),
            max_llm_input_chars=int(service.get("MAX_LLM_INPUT_CHARS")),
            max_concurrent_jobs=int(service.get("MAX_CONCURRENT_JOBS")),
            gemini_api_key=str(service.get("GEMINI_API_KEY")),
            gemini_model=str(service.get("GEMINI_MODEL")),
            gemini_timeout_seconds=int(service.get("GEMINI_TIMEOUT_SECONDS")),
            ai_model_mode=str(service.get("AI_MODEL_MODE")),
            ai_primary_model=str(service.get("AI_PRIMARY_MODEL")),
            ai_fallback_models=parse_fallbacks(service.get("AI_FALLBACK_MODELS")),
            ai_free_tier_only=bool(service.get("AI_FREE_TIER_ONLY")),
            whisper_model=str(service.get("WHISPER_MODEL")),
            transcript_source=str(service.get("TRANSCRIPT_SOURCE")),
            render_profile=str(service.get("RENDER_PROFILE")),
            public_mode=os.getenv("PUBLIC_MODE", "false").lower() == "true",
            # Env-only: a bearer token must never live in the shared
            # settings DB / UI, where it could be read back.
            api_auth_token=os.getenv("API_AUTH_TOKEN", "").strip(),
            job_rate_limit_per_minute=_int("JOB_RATE_LIMIT_PER_MINUTE", 60),
            audit_max_bytes=_int("AUDIT_MAX_BYTES", 50 * 1024 * 1024),
            signed_url_secret=os.getenv("SIGNED_URL_SECRET", "").strip(),
        )
        values.validate()
        return values

    @classmethod
    def resolve_best_effort(cls) -> "Settings":
        """Try Redis-backed resolution with a short timeout, else env-only."""
        try:
            import redis

            client = redis.Redis.from_url(
                os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                decode_responses=False,
                socket_timeout=2,
            )
            client.ping()
            return cls.resolve(client)
        except Exception:
            return cls.resolve(None)

    def validate(self) -> None:
        positive = {
            "max_source_duration_seconds": self.max_source_duration_seconds,
            "max_clips": self.max_clips,
            "min_clip_duration_seconds": self.min_clip_duration_seconds,
            "max_clip_duration_seconds": self.max_clip_duration_seconds,
            "max_transcript_chars": self.max_transcript_chars,
            "max_artifact_bytes": self.max_artifact_bytes,
            "max_job_seconds": self.max_job_seconds,
            "max_llm_output_tokens": self.max_llm_output_tokens,
            "max_llm_input_chars": self.max_llm_input_chars,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "gemini_timeout_seconds": self.gemini_timeout_seconds,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise ConfigurationError(f"Values must be positive: {', '.join(invalid)}")
        if self.min_clip_duration_seconds > self.max_clip_duration_seconds:
            raise ConfigurationError("Minimum clip duration exceeds maximum")
        if not 0 <= self.clip_buffer_seconds <= 5:
            raise ConfigurationError("CLIP_BUFFER_SECONDS must be between 0 and 5")
        if self.public_mode and self.api_host in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigurationError("PUBLIC_MODE requires a non-loopback API host")
        if self.public_mode and not self.api_auth_token:
            raise ConfigurationError(
                "PUBLIC_MODE requires API_AUTH_TOKEN; refusing to expose an "
                "unauthenticated API"
            )
        if self.transcript_source not in {"youtube_first", "whisper_only", "youtube_only"}:
            raise ConfigurationError("TRANSCRIPT_SOURCE must be youtube_first, whisper_only, or youtube_only")

    def ensure_storage(self) -> Path:
        self.storage_root.mkdir(parents=True, exist_ok=True)
        return self.storage_root
