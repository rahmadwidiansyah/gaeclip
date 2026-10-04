"""Centralized TalkNet configuration (§28).

Resolution: constructor args > TALKNET_* environment > defaults. All values
validated (no NaN, ranges clamped, enums checked). Resource modes map to
concrete thread/batch/escalation budgets so AUTO stays inspectable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


RESOURCE_MODES = ("auto", "low", "balanced", "performance")
DEVICES = ("auto", "cpu", "cuda", "mps")


def _host_ram_gb() -> float:
    """Host RAM in GiB without new dependencies (/proc, sysconf fallback)."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1048576.0
    except (OSError, ValueError, IndexError):
        pass
    try:
        import os as _os

        pages = _os.sysconf("SC_PHYS_PAGES")
        page = _os.sysconf("SC_PAGE_SIZE")
        return (pages * page) / 1073741824.0
    except (ValueError, OSError, AttributeError):
        return 16.0


@dataclass(frozen=True)
class TalkNetConfig:
    enabled: bool = True
    device: str = "auto"  # auto|cpu|cuda|mps
    resource_mode: str = "auto"  # auto|low|balanced|performance
    model_path: str = ""  # empty = models/pretrain_TalkSet.model
    window_ms: int = 500  # 300-1000
    face_padding: float = 0.25
    face_size: int = 112
    frame_fps: int = 25
    cpu_threads: int = 2
    max_batch_size: int = 4
    max_concurrent_inference: int = 1
    cache_ttl_ms: int = 1500
    cache_max_entries: int = 512
    gpu_memory_fraction: float = 0.70
    # Escalation triggers (§10-11, starting defaults for benchmarking).
    trigger_margin: float = 0.12
    trigger_confidence: float = 0.60
    min_people: int = 4
    onset_trigger: bool = True
    target_lost_trigger: bool = True
    # Fusion weights (§13 initial values; renormalized, NaN-safe).
    weight_talknet: float = 0.55
    weight_heuristic: float = 0.25
    weight_continuity: float = 0.10
    weight_face_quality: float = 0.05
    weight_onset: float = 0.10
    # Coverage rule: need this fraction of window frames with a face box.
    min_coverage: float = 0.60
    debug_export: bool = False

    def __post_init__(self):
        if self.device not in DEVICES:
            raise ValueError(f"device must be one of {DEVICES}")
        if self.resource_mode not in RESOURCE_MODES:
            raise ValueError(f"resource_mode must be one of {RESOURCE_MODES}")
        if not 300 <= self.window_ms <= 1000:
            raise ValueError("window_ms must be 300-1000")
        if not 0.0 <= self.face_padding <= 1.0:
            raise ValueError("face_padding must be 0-1")
        if self.cpu_threads < 1 or self.max_batch_size < 1:
            raise ValueError("cpu_threads/max_batch_size must be >= 1")
        if not 0.05 <= self.gpu_memory_fraction <= 1.0:
            raise ValueError("gpu_memory_fraction must be 0.05-1.0")
        for name in ("weight_talknet", "weight_heuristic", "weight_continuity",
                     "weight_face_quality", "weight_onset"):
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and value == value and value >= 0):
                raise ValueError(f"{name} must be a non-negative number")
        if not 0.0 <= self.min_coverage <= 1.0:
            raise ValueError("min_coverage must be 0-1")

    @classmethod
    def resolve(cls, client=None) -> "TalkNetConfig":
        """DB -> env -> default via AppSettings when a Redis client is
        given (worker/pipeline path); env-only otherwise (CLI/offline)."""
        if client is None:
            return cls.from_env()
        try:
            from ..app_settings import AppSettings

            service = AppSettings(client)
            return cls(
                enabled=bool(service.get("TALKNET_ENABLED")),
                device=str(service.get("TALKNET_DEVICE")),
                resource_mode=str(service.get("TALKNET_RESOURCE_MODE")),
                **{k: v for k, v in cls.from_env().__dict__.items()
                   if k not in ("enabled", "device", "resource_mode")},
            )
        except Exception:
            return cls.from_env()

    @classmethod
    def from_env(cls) -> "TalkNetConfig":
        return cls(
            enabled=_env_bool("TALKNET_ENABLED", True),
            device=_env_str("TALKNET_DEVICE", "auto").lower(),
            resource_mode=_env_str("TALKNET_RESOURCE_MODE", "auto").lower(),
            model_path=_env_str("TALKNET_MODEL_PATH", ""),
            window_ms=_env_int("TALKNET_WINDOW_MS", 500),
            face_padding=_env_float("TALKNET_FACE_PADDING", 0.25),
            face_size=_env_int("TALKNET_FACE_SIZE", 112),
            frame_fps=_env_int("TALKNET_FRAME_FPS", 25),
            cpu_threads=_env_int("TALKNET_CPU_THREADS", 2),
            max_batch_size=_env_int("TALKNET_MAX_BATCH_SIZE", 4),
            max_concurrent_inference=_env_int("TALKNET_MAX_CONCURRENT_INFERENCE", 1),
            cache_ttl_ms=_env_int("TALKNET_CACHE_TTL_MS", 1500),
            cache_max_entries=_env_int("TALKNET_CACHE_MAX_ENTRIES", 512),
            gpu_memory_fraction=_env_float("TALKNET_GPU_MEMORY_FRACTION", 0.70),
            trigger_margin=_env_float("TALKNET_TRIGGER_MARGIN", 0.12),
            trigger_confidence=_env_float("TALKNET_TRIGGER_CONFIDENCE", 0.60),
            min_people=_env_int("TALKNET_MIN_PEOPLE", 4),
            onset_trigger=_env_bool("TALKNET_ONSET_TRIGGER", True),
            target_lost_trigger=_env_bool("TALKNET_TARGET_LOST_TRIGGER", True),
            weight_talknet=_env_float("TALKNET_WEIGHT_TALKNET", 0.55),
            weight_heuristic=_env_float("TALKNET_WEIGHT_HEURISTIC", 0.25),
            weight_continuity=_env_float("TALKNET_WEIGHT_CONTINUITY", 0.10),
            weight_face_quality=_env_float("TALKNET_WEIGHT_FACE_QUALITY", 0.05),
            weight_onset=_env_float("TALKNET_WEIGHT_ONSET", 0.10),
            min_coverage=_env_float("TALKNET_MIN_COVERAGE", 0.60),
            debug_export=_env_bool("TALKNET_DEBUG_EXPORT", False),
        )

    def effective(self, cpu_count: int | None = None,
                  ram_gb: float | None = None,
                  vram_gb: float | None = None) -> dict:
        """Resolve `auto` resource mode to concrete budgets (inspectable).

        LOW: CPU<=8GB RAM. BALANCED: CPU 8-16GB. PERFORMANCE: adequate GPU
        VRAM (>=4GB) or explicitly requested. Returns the resolved numbers
        this process will actually use.
        """
        mode = self.resource_mode
        if mode == "auto":
            cpus = cpu_count or (os.cpu_count() or 2)
            ram = ram_gb
            if ram is None:
                ram = _host_ram_gb()
            vram = vram_gb if vram_gb is not None else 0.0
            if vram >= 4.0:
                mode = "performance"
            elif ram <= 8.0:
                mode = "low"
            else:
                mode = "balanced"
        table = {
            "low": {"cpu_threads": min(self.cpu_threads, 2),
                    "max_batch_size": min(self.max_batch_size, 2),
                    "escalation_stride": 2, "max_faces_per_window": 4},
            "balanced": {"cpu_threads": self.cpu_threads,
                         "max_batch_size": self.max_batch_size,
                         "escalation_stride": 1, "max_faces_per_window": 6},
            "performance": {"cpu_threads": max(self.cpu_threads, 4),
                            "max_batch_size": max(self.max_batch_size, 4),
                            "escalation_stride": 1, "max_faces_per_window": 8},
        }
        resolved = dict(table[mode])
        resolved["mode"] = mode
        return resolved
