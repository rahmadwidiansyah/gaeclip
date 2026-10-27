"""Persistent application settings backed by Redis (the app's database).

Two configuration kinds exist in Gaeclip:

- Deployment/infrastructure (.env): REDIS_URL, STORAGE_ROOT, API_HOST/PORT,
  frontend hosts, CORS origins, PUBLIC_MODE, SETTINGS_SECRET. Read at startup
  only, never editable from the UI.
- Application/runtime (this module): model choices, limits, timeouts,
  retention, log level. Stored in Redis, editable from Web UI, migrated once
  from the process environment (never overwriting user values).

Resolution order for every application setting::

    Redis override  ->  environment variable  ->  built-in default

Secrets are encrypted at rest with Fernet. The data key comes from the
``SETTINGS_SECRET`` deployment secret, a generated key file inside the
storage volume, or (last resort, warned) an ephemeral process key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from .errors import ConfigurationError, ValidationError

logger = logging.getLogger(__name__)

# Keys allowed to hold an explicit empty value (unset preference).
EMPTY_OK_KEYS = frozenset({"AI_PRIMARY_MODEL", "AI_FALLBACK_MODELS",
                           "SOCIAL_YOUTUBE_CLIENT_ID", "SOCIAL_YOUTUBE_CLIENT_SECRET",
                            "SOCIAL_TIKTOK_CLIENT_KEY", "SOCIAL_TIKTOK_CLIENT_SECRET",
                            "SOCIAL_META_APP_ID", "SOCIAL_META_APP_SECRET",
                            "SOCIAL_INSTAGRAM_APP_ID", "SOCIAL_INSTAGRAM_APP_SECRET",
                            "SOCIAL_PUBLIC_MEDIA_BASE_URL"})

SETTINGS_HASH = "gaeclip:settings"
SETTINGS_META_HASH = "gaeclip:settings:meta"

_SECRET_PREFIX = "enc:v1:"


# --------------------------------------------------------------------------
# Setting definitions
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SettingDef:
    key: str
    type: str  # string|integer|float|boolean|secret|duration|file_size|enum
    category: str
    label: str
    env_var: str
    description: str
    default: str  # canonical stored form (base unit for duration/file_size)
    unit: str | None = None  # base unit hint for the UI: seconds|hours|bytes
    options: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    needs_restart: bool = False
    apply_note: str = "Applied to new jobs."


SETTINGS: tuple[SettingDef, ...] = (
    # -- AI & models -------------------------------------------------------
    SettingDef("GEMINI_API_KEY", "secret", "AI & Models", "Gemini API Key",
               "GEMINI_API_KEY", "API key untuk kurasi Gemini. Tidak pernah ditampilkan kembali.",
               "", None, None, None, None, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_MODEL", "string", "AI & Models", "Model Gemini (legacy)",
               "GEMINI_MODEL", "Model tunggal lama. Dipakai sebagai primary manual saat Model Utama AI kosong.",
               "gemini-3.8-flash", None, None, None, None, False, "Berlaku untuk job baru."),
    SettingDef("AI_MODEL_MODE", "enum", "AI & Models", "Mode Pemilihan Model",
               "AI_MODEL_MODE", "Otomatis mengurutkan model hasil discovery berdasarkan policy; manual memakai rantai pilihanmu.",
               "automatic", None, ("automatic", "manual"),
               None, None, False, "Berlaku untuk job baru."),
    SettingDef("AI_PRIMARY_MODEL", "string", "AI & Models", "Model Utama AI",
               "AI_PRIMARY_MODEL", "ID model kurasi utama. Kosong berarti memakai Model Gemini.",
               "", None, None, None, None, False, "Berlaku untuk job baru."),
    SettingDef("AI_FALLBACK_MODELS", "string", "AI & Models", "Model Fallback AI",
               "AI_FALLBACK_MODELS", "Daftar JSON ID model fallback sesuai urutan.",
               "[]", None, None, None, None, False, "Berlaku untuk job baru."),
    SettingDef("AI_FREE_TIER_ONLY", "boolean", "AI & Models", "Hanya Free Tier",
               "AI_FREE_TIER_ONLY", "Hanya model free-tier yang bisa dipilih/dijalankan.",
               "True", None, None, None, None, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_TIMEOUT_SECONDS", "duration", "AI & Models", "Batas Waktu Gemini",
               "GEMINI_TIMEOUT_SECONDS", "Waktu tunggu maksimum satu panggilan kurasi Gemini.",
               "300", "seconds", None, 1, 3600, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_MAX_ATTEMPTS", "integer", "AI & Models", "Upaya Maksimum Gemini",
               "GEMINI_MAX_ATTEMPTS", "Batas keras satu permintaan di semua provider dan model.",
               "25", None, None, 1, 50, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_RETRY_MAX_ATTEMPTS", "integer", "AI & Models", "Percobaan Ulang Gemini Per Endpoint",
               "GEMINI_RETRY_MAX_ATTEMPTS", "Percobaan ulang pada provider+model yang sama sebelum pindah.",
               "2", None, None, 0, 5, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_RETRY_BASE_DELAY_MS", "integer", "AI & Models", "Jeda Dasar Percobaan Ulang Gemini",
               "GEMINI_RETRY_BASE_DELAY_MS", "Dasar backoff eksponensial antar percobaan ulang.",
               "500", None, None, 50, 10000, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_RETRY_MAX_DELAY_MS", "integer", "AI & Models", "Jeda Maksimum Percobaan Ulang Gemini",
               "GEMINI_RETRY_MAX_DELAY_MS", "Backoff dan Retry-After dibatasi sampai nilai ini.",
               "5000", None, None, 500, 60000, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_RETRY_JITTER_RATIO", "float", "AI & Models", "Jitter Percobaan Ulang Gemini",
               "GEMINI_RETRY_JITTER_RATIO", "Keacakan terbatas pada jeda backoff.",
               "0.25", None, None, 0, 1, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_REQUEST_TIMEOUT_MS", "integer", "AI & Models", "Batas Waktu Permintaan Gemini",
               "GEMINI_REQUEST_TIMEOUT_MS", "Batas waktu per percobaan dalam milidetik.",
               "30000", None, None, 1000, 300000, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_CIRCUIT_FAILURE_THRESHOLD", "integer", "AI & Models", "Ambang Circuit Gemini",
               "GEMINI_CIRCUIT_FAILURE_THRESHOLD", "Kegagalan beruntun sebelum circuit provider+model terbuka.",
               "3", None, None, 1, 10, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_CIRCUIT_COOLDOWN_MS", "integer", "AI & Models", "Masa Pendinginan Circuit Gemini",
               "GEMINI_CIRCUIT_COOLDOWN_MS", "Masa pendinginan setelah batas permintaan atau circuit terbuka.",
               "30000", None, None, 1000, 600000, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_AUTH_COOLDOWN_MS", "integer", "AI & Models", "Masa Pendinginan Auth Gemini",
               "GEMINI_AUTH_COOLDOWN_MS", "Pendinginan lama untuk key tidak valid; tanpa perulangan percobaan pada 401/403.",
               "300000", None, None, 10000, 3600000, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_MAX_CONCURRENT_PER_PROVIDER", "integer", "AI & Models", "Maksimum Konkuren Per Provider Gemini",
               "GEMINI_MAX_CONCURRENT_PER_PROVIDER", "Batas permintaan berjalan per provider; kelebihan dialihkan ke tempat lain.",
               "2", None, None, 1, 8, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_ROUTING_STRATEGY", "enum", "AI & Models", "Strategi Routing Gemini",
               "GEMINI_ROUTING_STRATEGY", "Urutan provider: priority, round_robin, atau health_first.",
               "priority", None, ("priority", "round_robin", "health_first"),
               None, None, False, "Berlaku untuk job baru."),
    SettingDef("GEMINI_MODEL_DISCOVERY_TTL", "duration", "AI & Models", "TTL Cache Discovery Gemini",
               "GEMINI_MODEL_DISCOVERY_TTL", "Berapa lama hasil models.list dipercaya.",
               "21600", "seconds", None, 60, 86400, False, "Berlaku untuk job baru."),
    SettingDef("WHISPER_MODEL", "enum", "Transcript & LLM", "Model Whisper",
               "WHISPER_MODEL", "Model faster-whisper untuk transkripsi video.",
               "small", None, ("tiny", "base", "small", "medium", "large", "large-v3", "turbo"),
               None, None, False, "Berlaku untuk job baru."),
    SettingDef("TALKNET_ENABLED", "boolean", "AI & Models", "Deteksi Speaker Lanjutan (TalkNet)",
               "TALKNET_ENABLED", "Lapisan eskalasi AI untuk kasus ambigu (4+ wajah, margin tipis). Heuristik tetap jalan dulu; nonaktif = heuristik saja.",
               "True", None, None, None, None, False, "Berlaku untuk job baru."),
    SettingDef("TALKNET_DEVICE", "enum", "AI & Models", "Perangkat Inferensi TalkNet",
               "TALKNET_DEVICE", "Auto = CUDA > MPS > CPU. Paksa CPU untuk hemat daya; cuda/mps yang tak tersedia otomatis fallback ke CPU.",
               "auto", None, ("auto", "cpu", "cuda", "mps"),
               None, None, False, "Berlaku untuk job baru."),
    SettingDef("TALKNET_RESOURCE_MODE", "enum", "AI & Models", "Mode Resource TalkNet",
               "TALKNET_RESOURCE_MODE", "Low = hemat (thread/batch/eskalasi dibatasi); auto menyesuaikan RAM/VRAM mesin.",
               "auto", None, ("auto", "low", "balanced", "performance"),
               None, None, False, "Berlaku untuk job baru."),
    # -- Processing --------------------------------------------------------
    SettingDef("RENDER_PROFILE", "enum", "Processing", "Profil Kualitas Render",
               "GAECLIP_RENDER_PROFILE", "Tingkat encode akhir: fast (veryfast/22), balanced (medium/20), quality (medium/18).",
               "quality", None, ("fast", "balanced", "quality"),
               None, None, False, "Berlaku untuk job baru."),
    SettingDef("WORKER_CONCURRENCY", "integer", "Processing", "Konkurensi Worker",
               "WORKER_CONCURRENCY", "Slot worker Celery paralel. Perlu restart worker.",
               "1", None, None, 1, 8, True, "Berlaku setelah worker di-restart."),
    SettingDef("MAX_CONCURRENT_JOBS", "integer", "Processing", "Maksimum Job Konkuren",
               "MAX_CONCURRENT_JOBS", "Penjaga antrean untuk job yang berjalan bersamaan.",
               "1", None, None, 1, 16, False, "Berlaku untuk job baru."),
    SettingDef("MAX_SOURCE_DURATION_SECONDS", "duration", "Processing", "Durasi Maksimum Sumber",
               "MAX_SOURCE_DURATION_SECONDS", "Video sumber terpanjang yang diterima.",
               "7200", "seconds", None, 60, 86400, False, "Berlaku untuk job baru."),
    SettingDef("MAX_CLIPS", "integer", "Processing", "Maksimum Klip",
               "MAX_CLIPS", "Jumlah bawaan klip yang dibuat per job.",
               "3", None, None, 1, 10, False, "Berlaku untuk job baru."),
    SettingDef("MIN_CLIP_DURATION_SECONDS", "duration", "Processing", "Durasi Minimum Klip",
               "MIN_CLIP_DURATION_SECONDS", "Klip terpendek yang diizinkan. Tidak boleh melebihi maksimum.",
               "30", "seconds", None, 5, 600, False, "Berlaku untuk job baru."),
    SettingDef("MAX_CLIP_DURATION_SECONDS", "duration", "Processing", "Durasi Maksimum Klip",
               "MAX_CLIP_DURATION_SECONDS", "Klip terpanjang yang diizinkan.",
               "60", "seconds", None, 5, 600, False, "Berlaku untuk job baru."),
    SettingDef("CLIP_BUFFER_SECONDS", "duration", "Processing", "Buffer Klip",
               "CLIP_BUFFER_SECONDS", "Padding yang ditambahkan di sekitar tiap batas klip.",
               "1.5", "seconds", None, 0, 5, False, "Berlaku untuk job baru."),
    SettingDef("MAX_JOB_SECONDS", "duration", "Processing", "Durasi Maksimum Job",
               "MAX_JOB_SECONDS", "Batas watchdog untuk satu job.",
               "3600", "seconds", None, 60, 86400, False, "Berlaku untuk job baru."),
    # -- Transcript & LLM --------------------------------------------------
    SettingDef("TRANSCRIPT_SOURCE", "enum", "Transcript & LLM", "Sumber Transkrip",
               "TRANSCRIPT_SOURCE", "youtube_first: subtitle YT dulu, gagal baru Whisper (hemat CPU). whisper_only: selalu Whisper, YT dilewati (paling akurat). youtube_only: hanya YT, gagal cepat bila tidak ada subtitle.",
               "youtube_first", None, ("youtube_first", "whisper_only", "youtube_only"),
               None, None, False, "Berlaku untuk job baru."),
    SettingDef("MAX_TRANSCRIPT_CHARS", "integer", "Transcript & LLM", "Maksimum Karakter Transkrip",
               "MAX_TRANSCRIPT_CHARS", "Batas ukuran transkrip sebelum kurasi.",
               "1000000", None, None, minimum=1000, maximum=10000000, needs_restart=False, apply_note="Berlaku untuk job baru."),
    SettingDef("MAX_LLM_INPUT_CHARS", "integer", "Transcript & LLM", "Maksimum Karakter Input LLM",
               "MAX_LLM_INPUT_CHARS", "Di atas nilai ini, kurasi beralih ke mode deterministik.",
               "120000", None, None, minimum=1000, maximum=1000000, needs_restart=False, apply_note="Berlaku untuk job baru."),
    SettingDef("MAX_LLM_OUTPUT_TOKENS", "integer", "Transcript & LLM", "Maksimum Token Output LLM",
               "MAX_LLM_OUTPUT_TOKENS", "Anggaran token untuk satu respons kurasi.",
               "4096", None, None, minimum=256, maximum=32768, needs_restart=False, apply_note="Berlaku untuk job baru."),
    # -- Storage & retention -----------------------------------------------
    SettingDef("MAX_ARTIFACT_BYTES", "file_size", "Storage & Retention", "Ukuran Maksimum Artefak",
               "MAX_ARTIFACT_BYTES", "Satu artefak tersimpan terbesar.",
               "524288000", "bytes", None, minimum=1048576, maximum=10737418240, needs_restart=False, apply_note="Berlaku untuk job baru."),
    SettingDef("ARTIFACT_RETENTION_HOURS", "duration", "Storage & Retention", "Masa Simpan Artefak Selesai",
               "ARTIFACT_RETENTION_HOURS", "Menentukan berapa lama artefak yang telah selesai disimpan. Isi 0 untuk menyimpan tanpa batas waktu.",
               "24", "hours", None, 0, 8760, False, "Diterapkan saat proses pembersihan berjalan."),
    SettingDef("INTERMEDIATE_RETENTION_HOURS", "duration", "Storage & Retention", "Masa Simpan File Sementara",
               "INTERMEDIATE_RETENTION_HOURS", "Menentukan berapa lama file sementara disimpan. Isi 0 untuk menyimpan tanpa batas waktu.",
               "6", "hours", None, 0, 8760, False, "Diterapkan saat proses pembersihan berjalan."),
    # -- Logging -------------------------------------------------------------
    SettingDef("LOG_LEVEL", "enum", "Logging", "Level Log",
               "LOG_LEVEL", "Tingkat detail log aplikasi.",
               "INFO", None, ("DEBUG", "INFO", "WARNING", "ERROR"),
               None, None, False, "Berlaku untuk job baru; log API mengikuti setelah restart."),
    # -- Social Publishing (OAuth client registrations; secrets encrypted) --
    SettingDef("SOCIAL_YOUTUBE_CLIENT_ID", "string", "Social Publishing", "YouTube OAuth Client ID",
               "SOCIAL_YOUTUBE_CLIENT_ID", "Client ID aplikasi Desktop dari Google Cloud Console. Kosong = YouTube connect nonaktif.",
               "", None, None, None, None, False, "Berlaku untuk koneksi baru."),
    SettingDef("SOCIAL_YOUTUBE_CLIENT_SECRET", "secret", "Social Publishing", "YouTube OAuth Client Secret",
               "SOCIAL_YOUTUBE_CLIENT_SECRET", "Opsional untuk tipe Desktop app. Tidak pernah ditampilkan kembali.",
               "", None, None, None, None, False, "Berlaku untuk koneksi baru."),
    SettingDef("SOCIAL_TIKTOK_CLIENT_KEY", "string", "Social Publishing", "TikTok Client Key",
               "SOCIAL_TIKTOK_CLIENT_KEY", "Client Key dari TikTok developer portal. Kosong = TikTok connect nonaktif.",
               "", None, None, None, None, False, "Berlaku untuk koneksi baru."),
    SettingDef("SOCIAL_TIKTOK_CLIENT_SECRET", "secret", "Social Publishing", "TikTok Client Secret",
               "SOCIAL_TIKTOK_CLIENT_SECRET", "Client Secret TikTok. Tidak pernah ditampilkan kembali.",
               "", None, None, None, None, False, "Berlaku untuk koneksi baru."),
    SettingDef("SOCIAL_META_APP_ID", "string", "Social Publishing", "Meta App ID",
               "SOCIAL_META_APP_ID", "App ID Meta untuk Facebook Login (Facebook publishing). Kosong = Facebook connect nonaktif.",
               "", None, None, None, None, False, "Berlaku untuk koneksi baru."),
    SettingDef("SOCIAL_META_APP_SECRET", "secret", "Social Publishing", "Meta App Secret",
               "SOCIAL_META_APP_SECRET", "App Secret Meta (Facebook). Tidak pernah ditampilkan kembali.",
               "", None, None, None, None, False, "Berlaku untuk koneksi baru."),
    SettingDef("SOCIAL_INSTAGRAM_APP_ID", "string", "Social Publishing", "Instagram App ID",
               "SOCIAL_INSTAGRAM_APP_ID", "Instagram App ID (App Dashboard > Instagram > API setup with Instagram login). Kosong = Instagram connect nonaktif.",
               "", None, None, None, None, False, "Berlaku untuk koneksi baru."),
    SettingDef("SOCIAL_INSTAGRAM_APP_SECRET", "secret", "Social Publishing", "Instagram App Secret",
               "SOCIAL_INSTAGRAM_APP_SECRET", "Instagram App Secret (Business Login). Tidak pernah ditampilkan kembali.",
               "", None, None, None, None, False, "Berlaku untuk koneksi baru."),
    SettingDef("SOCIAL_PUBLIC_MEDIA_BASE_URL", "string", "Social Publishing", "Public Media Base URL",
               "SOCIAL_PUBLIC_MEDIA_BASE_URL", "Basis URL https publik untuk file clip (mis. domain Cloudflare Tunnel). Dipakai sebagai video_url Instagram. Kosong = nonaktif.",
               "", None, None, None, None, False, "Berlaku untuk publish berikutnya."),
    SettingDef("SOCIAL_OAUTH_HOST", "string", "Social Publishing", "OAuth Callback Host",
               "SOCIAL_OAUTH_HOST", "Interface listener callback OAuth. 127.0.0.1 untuk desktop. "
               "Docker satu mesin dengan browser: 0.0.0.0 + publish 127.0.0.1:PORT:PORT (host tetap loopback).",
               "127.0.0.1", None, None, None, None, False, "Berlaku untuk koneksi baru."),
    SettingDef("SOCIAL_OAUTH_PORT", "integer", "Social Publishing", "OAuth Callback Port",
               "SOCIAL_OAUTH_PORT", "0 = port acak tiap sesi (disarankan). Isi port tetap bila perlu publish port Docker.",
               "0", None, None, 0, 65535, False, "Berlaku untuk koneksi baru."),
)

DEFS: dict[str, SettingDef] = {item.key: item for item in SETTINGS}

INFRASTRUCTURE_KEYS = frozenset({
    "REDIS_URL", "STORAGE_ROOT", "API_HOST", "API_PORT",
    "NEXT_PUBLIC_API_BASE_URL", "FRONTEND_ORIGINS", "FRONTEND_ORIGIN",
    "FRONTEND_HOST", "FRONTEND_PORT", "PUBLIC_MODE", "SETTINGS_SECRET",
    "API_AUTH_TOKEN",
})


# --------------------------------------------------------------------------
# Parsing / validation
# --------------------------------------------------------------------------

def _parse(key: str, raw: str) -> Any:
    item = DEFS[key]
    text = raw.strip() if isinstance(raw, str) else raw
    try:
        if item.type == "boolean":
            lowered = str(text).strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
            raise ValueError("harus true/false")
        if item.type == "integer":
            value: Any = int(str(text).strip())
        elif item.type in {"float", "duration", "file_size"}:
            value = float(str(text).strip())
            if item.type == "file_size":
                value = int(value)
        elif item.type == "enum":
            value = str(text).strip()
            options = [opt.upper() if key == "LOG_LEVEL" else opt for opt in (item.options or ())]
            needle = value.upper() if key == "LOG_LEVEL" else value
            if needle not in options:
                raise ValueError(f"harus salah satu dari: {', '.join(item.options or [])}")
            value = needle if key == "LOG_LEVEL" else value
        else:  # string, secret
            value = str(text)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"{key}: nilai tidak valid ({exc})") from exc
    if item.minimum is not None and isinstance(value, (int, float)) and value < item.minimum:
        raise ValidationError(f"{key}: minimal {item.minimum:g}")
    if item.maximum is not None and isinstance(value, (int, float)) and value > item.maximum:
        raise ValidationError(f"{key}: maksimal {item.maximum:g}")
    if item.type == "string" and not value and key not in EMPTY_OK_KEYS:
        raise ValidationError(f"{key}: nilai wajib diisi")
    return value


def _env_raw(item: SettingDef) -> str | None:
    raw = os.getenv(item.env_var)
    if raw is None or (isinstance(raw, str) and raw.strip() == "" and item.key != "GEMINI_API_KEY"):
        return None
    if isinstance(raw, str) and raw.strip() == "":
        return "" if item.key == "GEMINI_API_KEY" else None
    return raw


# --------------------------------------------------------------------------
# Secret encryption (Fernet; key from deployment secret, key file, or ephemeral)
# --------------------------------------------------------------------------

_fernet_cache: dict[str, Any] = {}


def _data_key() -> bytes:
    """32-byte data key. Never logged, never returned to clients."""
    direct = os.getenv("SETTINGS_SECRET", "").strip()
    if direct:
        return hashlib.sha256(direct.encode()).digest()
    storage_root = os.getenv("STORAGE_ROOT", "./storage")
    key_file = os.path.join(os.path.expanduser(storage_root), ".settings_key")
    try:
        if os.path.exists(key_file):
            with open(key_file, "rb") as handle:
                raw = handle.read().strip()
            if len(raw) == 32:
                return raw
        os.makedirs(os.path.dirname(os.path.abspath(key_file)), exist_ok=True)
        raw = os.urandom(32)
        with open(key_file, "wb") as handle:
            handle.write(raw)
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
        logger.warning("SETTINGS_SECRET not set; generated a persistent key file for secret storage.")
        return raw
    except OSError as exc:
        logger.warning("Cannot persist settings key (%s); secrets will not survive restart.", exc)
        return hashlib.sha256(f"ephemeral-{os.getpid()}-{time.time()}".encode()).digest()


def _fernet() -> Any:
    if "fernet" not in _fernet_cache:
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise ConfigurationError("Install cryptography to store secrets") from exc
        digest = hashlib.sha256(_data_key()).digest()
        _fernet_cache["fernet"] = Fernet(base64.urlsafe_b64encode(digest))
    return _fernet_cache["fernet"]


def encrypt_secret(plaintext: str) -> str:
    return _SECRET_PREFIX + _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(stored: str) -> str:
    if not stored.startswith(_SECRET_PREFIX):
        # Legacy/plain entries: accept read-only so migration never bricks boot.
        return stored
    return _fernet().decrypt(stored[len(_SECRET_PREFIX):].encode()).decode()


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------

class AppSettings:
    """Central configuration service. Single source of truth for app settings."""

    def __init__(self, client: Any | None):
        self.client = client

    # -- low level ---------------------------------------------------------
    def stored_raw(self, key: str) -> str | None:
        if self.client is None:
            return None
        raw = self.client.hget(SETTINGS_HASH, key)
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    # -- reads ---------------------------------------------------------------
    def get(self, key: str) -> Any:
        item = DEFS.get(key)
        if item is None:
            raise ValidationError(f"Pengaturan tidak dikenal: {key}")
        stored = self.stored_raw(key)
        if stored is not None:
            if item.type == "secret":
                try:
                    value = decrypt_secret(stored)
                except Exception:
                    # Wrong/rotated data key: never brick boot or settings;
                    # fall through to env/default like a missing value.
                    logger.warning(
                        "Stored secret for %s cannot be decrypted "
                        "(data key changed?); falling back.", key)
                    value = None
            else:
                value = stored
            if value is not None:
                try:
                    return _parse(key, value)
                except ValidationError:
                    logger.warning("Stored value for %s is invalid; falling back.", key)
        env = _env_raw(item)
        if env is not None:
            try:
                return _parse(key, env)
            except ValidationError:
                logger.warning("Environment value for %s is invalid; using default.", item.env_var)
        return _parse(key, item.default)

    def is_configured(self, key: str) -> bool:
        item = DEFS.get(key)
        if item is None:
            raise ValidationError(f"Pengaturan tidak dikenal: {key}")
        if self.stored_raw(key):
            return True
        env = _env_raw(item)
        return env is not None and str(env).strip() != ""

    def as_dict(self, *, mask_secrets: bool = True) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in SETTINGS:
            stored = self.stored_raw(item.key)
            if item.type == "secret":
                out.append({
                    "key": item.key, "type": item.type, "category": item.category,
                    "label": item.label, "env_var": item.env_var,
                    "description": item.description, "unit": item.unit,
                    "options": list(item.options or []),
                    "minimum": item.minimum, "maximum": item.maximum,
                    "needs_restart": item.needs_restart, "apply_note": item.apply_note,
                    "default": None, "value": None,
                    "configured": self.is_configured(item.key),
                    "customized": stored is not None,
                })
                continue
            out.append({
                "key": item.key, "type": item.type, "category": item.category,
                "label": item.label, "env_var": item.env_var,
                "description": item.description, "unit": item.unit,
                "options": list(item.options or []),
                "minimum": item.minimum, "maximum": item.maximum,
                "needs_restart": item.needs_restart, "apply_note": item.apply_note,
                "default": _parse(item.key, item.default),
                "value": self.get(item.key),
                "configured": True,
                "customized": stored is not None,
            })
        return out

    # -- writes --------------------------------------------------------------
    def _check_unknown(self, keys: list[str]) -> None:
        unknown = [key for key in keys if key not in DEFS]
        if unknown:
            raise ValidationError(f"Pengaturan tidak dikenal: {', '.join(unknown)}")
        infra = [key for key in keys if key in INFRASTRUCTURE_KEYS]
        if infra:
            raise ValidationError(
                f"Pengaturan infrastruktur tidak bisa diubah di sini: {', '.join(infra)}"
            )

    def set(self, key: str, value: Any) -> Any:
        self._check_unknown([key])
        parsed = _parse(key, "" if value is None else str(value))
        self._check_cross_field(key, parsed)
        parsed = self._check_ai_prefs({key: parsed})[key]
        stored = encrypt_secret(parsed) if DEFS[key].type == "secret" else str(parsed)
        if self.client is None:
            raise ConfigurationError("Settings storage is not available")
        self.client.hset(SETTINGS_HASH, key, stored)
        self._touch_meta(key)
        self._after_write([key])
        return parsed

    def set_many(self, values: dict[str, Any]) -> dict[str, Any]:
        keys = list(values.keys())
        self._check_unknown(keys)
        parsed = {key: _parse(key, "" if value is None else str(value)) for key, value in values.items()}
        self._check_cross_field_bulk(parsed)
        parsed = self._check_ai_prefs(parsed)
        if self.client is None:
            raise ConfigurationError("Settings storage is not available")
        pipe = self.client.pipeline()
        for key, value in parsed.items():
            stored = encrypt_secret(value) if DEFS[key].type == "secret" else str(value)
            pipe.hset(SETTINGS_HASH, key, stored)
        pipe.execute()
        for key in parsed:
            self._touch_meta(key)
        self._after_write(list(parsed))
        return parsed

    def reset(self, key: str) -> None:
        self._check_unknown([key])
        if self.client is None:
            raise ConfigurationError("Settings storage is not available")
        self.client.hdel(SETTINGS_HASH, key)
        self._touch_meta(key)
        self._after_write([key])

    # -- AI model preferences: atomic validation + key-change invalidation --
    def _check_ai_prefs(self, parsed: dict[str, Any]) -> dict[str, Any]:
        """Normalize + validate AI chain prefs before commit (AL).

        - AI_FALLBACK_MODELS must be a JSON list of model ids; duplicates
          (incl. primary) are normalized out, order preserved.
        - When the write touches the chain and a discovery cache exists, the
          chain is validated against it (eligibility + Free Tier Only).
        Never touches the network here: cache-only, so saves stay atomic
        even when Google is unreachable.
        """
        from .ai_models import AI_PREF_KEYS, parse_fallbacks

        if not (set(parsed) & (set(AI_PREF_KEYS) | {"GEMINI_MODEL"})):
            return parsed
        out = dict(parsed)
        if "AI_FALLBACK_MODELS" in out or "AI_PRIMARY_MODEL" in out:
            # Backend normalization (T): dedupe preserving order, primary wins.
            eff_primary = str(out.get("AI_PRIMARY_MODEL", "") or self._safe_get("AI_PRIMARY_MODEL", ""))
            if "AI_FALLBACK_MODELS" in out:
                seen: set[str] = set()
                deduped: list[str] = []
                for model in parse_fallbacks(out["AI_FALLBACK_MODELS"]):
                    if not model or model == eff_primary or model in seen:
                        continue
                    seen.add(model)
                    deduped.append(model)
                out["AI_FALLBACK_MODELS"] = json.dumps(deduped)
        if "AI_PRIMARY_MODEL" in out or "AI_FALLBACK_MODELS" in out or "AI_FREE_TIER_ONLY" in out:
            self._validate_ai_chain(out)
        return out

    def _safe_get(self, key: str, default: Any) -> Any:
        try:
            return self.get(key)
        except Exception:
            return default

    def _validate_ai_chain(self, overrides: dict[str, Any]) -> None:
        from .ai_models import AIPrefs, cached_snapshot, parse_fallbacks, validate_prefs

        def current(key: str, default: Any) -> Any:
            if key in overrides:
                return overrides[key]
            try:
                return self.get(key)
            except Exception:
                return default

        mode = str(current("AI_MODEL_MODE", "automatic"))
        primary = str(current("AI_PRIMARY_MODEL", "") or "")
        if not primary:
            try:
                primary = str(self.get("GEMINI_MODEL") or "")
            except Exception:
                primary = ""
        prefs = AIPrefs(
            mode=mode,
            primary_model=primary,
            fallback_models=parse_fallbacks(current("AI_FALLBACK_MODELS", "[]")),
            free_tier_only=bool(current("AI_FREE_TIER_ONLY", True)),
        )
        snapshot = None
        try:
            api_key = str(self.get("GEMINI_API_KEY") or "")
        except Exception:
            api_key = ""
        if api_key and self.client is not None:
            try:
                snapshot = cached_snapshot(self.client, api_key)
            except Exception:
                snapshot = None
        validate_prefs(prefs, snapshot)

    def _after_write(self, keys: list[str]) -> None:
        """API key change invalidates the model registry (AJ)."""
        if "GEMINI_API_KEY" not in keys:
            return
        try:
            from .ai_models import invalidate_registry

            invalidate_registry(self.client)
        except Exception:
            logger.warning("model_registry_invalidation_failed", exc_info=True)
    # -- cross-field validation ----------------------------------------------
    def _effective_pair(self, overrides: dict[str, Any]) -> tuple[float, float]:
        lo = overrides.get("MIN_CLIP_DURATION_SECONDS", self.get("MIN_CLIP_DURATION_SECONDS"))
        hi = overrides.get("MAX_CLIP_DURATION_SECONDS", self.get("MAX_CLIP_DURATION_SECONDS"))
        return float(lo), float(hi)

    def _check_cross_field(self, key: str, parsed: Any) -> None:
        if key in {"MIN_CLIP_DURATION_SECONDS", "MAX_CLIP_DURATION_SECONDS"}:
            lo, hi = self._effective_pair({key: parsed})
            if lo > hi:
                raise ValidationError("MIN_CLIP_DURATION_SECONDS tidak boleh melebihi MAX_CLIP_DURATION_SECONDS")

    def _check_cross_field_bulk(self, parsed: dict[str, Any]) -> None:
        if {"MIN_CLIP_DURATION_SECONDS", "MAX_CLIP_DURATION_SECONDS"} & set(parsed):
            lo, hi = self._effective_pair(parsed)
            if lo > hi:
                raise ValidationError("MIN_CLIP_DURATION_SECONDS tidak boleh melebihi MAX_CLIP_DURATION_SECONDS")

    # -- migration -------------------------------------------------------------
    def migrate_from_env(self) -> list[str]:
        """Seed DB from environment once. Never overwrites user values."""
        if self.client is None:
            return []
        seeded: list[str] = []
        for item in SETTINGS:
            if self.stored_raw(item.key) is not None:
                continue
            env = _env_raw(item)
            if env is None:
                continue
            try:
                parsed = _parse(item.key, env)
            except ValidationError:
                logger.warning("Skipping invalid env value for %s during migration.", item.env_var)
                continue
            stored = encrypt_secret(parsed) if item.type == "secret" else str(parsed)
            if item.type == "secret" and not parsed:
                continue
            self.client.hset(SETTINGS_HASH, item.key, stored)
            seeded.append(item.key)
        if seeded:
            logger.info("Migrated %d settings from environment.", len(seeded))
        return seeded

    def _touch_meta(self, key: str) -> None:
        if self.client is None:
            return
        self.client.hset(SETTINGS_META_HASH, key, json.dumps({"updated_at": time.time()}))


def apply_log_level(level: str) -> None:
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    # Uvicorn configures only its own loggers; without a root handler our
    # INFO records would vanish (last-resort handler keeps WARNING+ only).
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(numeric)
    logging.getLogger("gaeclip").setLevel(numeric)
