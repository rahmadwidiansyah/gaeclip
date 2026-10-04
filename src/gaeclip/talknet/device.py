"""Hardware-aware inference device detection (§2).

Priority: CUDA (incl. ROCm, which exposes the cuda API) > Apple MPS > CPU.
Detection is safe: torch import failure, missing backends, and broken CUDA
installs all degrade to CPU with a logged reason. A CUDA device that fails
at model-init time falls back to CPU as well (handled in model.py) — the
job MUST NOT fail only because GPU init failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InferenceDevice:
    device: str  # cuda | mps | cpu (torch device string, cuda may carry index)
    backend: str  # cuda | mps | cpu
    available: bool
    name: str = ""
    memory_total: int | None = None  # bytes, when queryable
    memory_free: int | None = None  # bytes, when queryable
    reason: str = ""


def _cuda_info(torch) -> InferenceDevice | None:
    try:
        if not torch.cuda.is_available():
            return None
    except Exception as exc:
        logger.warning("[TALKNET_DEVICE] device=cpu reason=cuda_probe_failed: %s", exc)
        return None
    try:
        index = torch.cuda.current_device()
        name = torch.cuda.get_device_name(index)
    except Exception as exc:
        logger.warning("[TALKNET_DEVICE] device=cpu reason=cuda_name_failed: %s", exc)
        return None
    total, free = None, None
    try:
        free, total = torch.cuda.mem_get_info(index)
    except Exception:
        pass
    import torch as _t

    backend = "cuda"
    if getattr(_t.version, "hip", None):
        backend = "cuda"  # ROCm exposes the cuda API; keep torch string "cuda"
        name = f"{name} (ROCm)"
    return InferenceDevice(device=f"cuda:{index}", backend=backend,
                           available=True, name=name,
                           memory_total=total, memory_free=free,
                           reason="cuda_available")


def _mps_info(torch) -> InferenceDevice | None:
    try:
        mps = torch.backends.mps
    except Exception:
        return None
    try:
        if not (mps.is_available() and mps.is_built()):
            return None
    except Exception as exc:
        logger.warning("[TALKNET_DEVICE] device=cpu reason=mps_probe_failed: %s", exc)
        return None
    return InferenceDevice(device="mps", backend="mps", available=True,
                           name="Apple MPS", reason="mps_available")


def detect_inference_device() -> InferenceDevice:
    """Auto-detect the best usable inference device (never raises)."""
    try:
        import torch
    except ImportError:
        logger.info("[TALKNET_DEVICE] device=cpu reason=torch_not_installed")
        return InferenceDevice(device="cpu", backend="cpu", available=True,
                               name="CPU", reason="torch_not_installed")
    cuda = _cuda_info(torch)
    if cuda is not None:
        logger.info("[TALKNET_DEVICE] device=%s backend=%s name=%s",
                    cuda.device, cuda.backend, cuda.name)
        return cuda
    mps = _mps_info(torch)
    if mps is not None:
        logger.info("[TALKNET_DEVICE] device=mps name=Apple MPS")
        return mps
    logger.info("[TALKNET_DEVICE] device=cpu reason=no_accelerator")
    return InferenceDevice(device="cpu", backend="cpu", available=True,
                           name="CPU", reason="no_accelerator")


def resolve_device(requested: str = "auto") -> InferenceDevice:
    """Honor TALKNET_DEVICE override; unknown/forced-unavailable values fall
    back safely (cuda/mps required-but-missing -> CPU + log, never crash)."""
    want = (requested or "auto").strip().lower()
    if want == "cpu":
        return InferenceDevice(device="cpu", backend="cpu", available=True,
                               name="CPU", reason="forced_cpu")
    auto = detect_inference_device()
    if want == "auto":
        return auto
    if want == "cuda":
        if auto.backend == "cuda":
            return auto
        logger.warning("[TALKNET_DEVICE] device=cpu reason=cuda_requested_unavailable")
        return InferenceDevice(device="cpu", backend="cpu", available=True,
                               name="CPU", reason="cuda_requested_unavailable")
    if want == "mps":
        if auto.backend == "mps":
            return auto
        logger.warning("[TALKNET_DEVICE] device=cpu reason=mps_requested_unavailable")
        return InferenceDevice(device="cpu", backend="cpu", available=True,
                               name="CPU", reason="mps_requested_unavailable")
    logger.warning("[TALKNET_DEVICE] device=cpu reason=unknown_device_%s", want)
    return InferenceDevice(device="cpu", backend="cpu", available=True,
                           name="CPU", reason=f"unknown_device_{want}")


def vram_allows_cuda(info: InferenceDevice, model_bytes: int,
                     fraction: float = 0.70) -> tuple[bool, str]:
    """GPU memory safety gate (§3): refuse CUDA when the model would exceed
    `fraction` of free VRAM. Unknown memory figures -> allow (CUDA OOM still
    falls back to CPU at init/infer time)."""
    if info.backend != "cuda" or not info.memory_free:
        return True, "vram_unknown_or_cpu"
    budget = info.memory_free * max(0.05, min(1.0, fraction))
    if model_bytes <= budget:
        return True, "vram_ok"
    return False, (
        f"model ~{model_bytes // 1048576}MB exceeds "
        f"{fraction:.0%} of free VRAM ({info.memory_free // 1048576}MB)")
