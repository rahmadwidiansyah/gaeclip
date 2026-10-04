"""TalkNet inference model: lazy singleton, safe device, no gradients.

Loads the vendored TalkNet-ASD architecture (see NOTICE) with the
TalkSet checkpoint (``model.*`` prefix stripped) plus the ``lossAV.FC``
speaking head. Exactly one reusable instance per (model_path, device);
inference runs under ``torch.inference_mode()`` with ``model.eval()``.
CUDA init/infer failure degrades to CPU (job never fails over GPU).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL_RELATIVE = "models/pretrain_TalkSet.model"

_REGISTRY: dict[tuple[str, str], "TalkNetInferenceModel"] = {}
_REGISTRY_LOCK = threading.Lock()
_THREADS_CONFIGURED = {"done": False, "previous": None}


def _require_torch():
    try:
        import torch
    except ImportError as exc:
        from ..errors import ExternalCommandError

        raise ExternalCommandError(
            "torch is not installed; TalkNet needs the vision/torch "
            "environment (heuristic fallback remains available)"
        ) from exc
    return torch


def resolve_model_path(configured: str = "") -> Path | None:
    """Configured path, else the bundled TalkSet checkpoint. None if missing
    (caller reports MODEL MISSING; nothing is downloaded per job)."""
    if configured.strip():
        candidate = Path(configured.strip()).expanduser()
        return candidate if candidate.is_file() else None
    bases = [Path.cwd(), Path(__file__).resolve().parents[3]]
    try:
        from ..storage import app_base_dir

        bundled_base = app_base_dir()
        if bundled_base not in bases:
            bases.insert(0, bundled_base)
    except Exception:
        pass
    for base in bases:
        candidate = base / DEFAULT_MODEL_RELATIVE
        if candidate.is_file():
            return candidate
    return None


def estimate_model_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 250 * 1048576


class TalkNetInferenceModel:
    """Reusable inference-only TalkNet (visual + audio + cross-attention)."""

    def __init__(self, model_path: Path, device: str,
                 cpu_threads: int = 2):
        torch = _require_torch()
        self.torch = torch
        self.model_path = model_path
        self.device = torch.device(device)
        self.device_name = device
        self._configure_threads(cpu_threads)
        started = time.perf_counter()
        try:
            self.model = self._build(torch, model_path, self.device)
        except Exception as exc:
            raise RuntimeError(f"talknet_init_failed on {device}: {exc}") from exc
        self.load_ms = (time.perf_counter() - started) * 1000.0
        logger.info("[TALKNET_MODEL] status=ready device=%s load_ms=%.0f",
                    device, self.load_ms)

    @staticmethod
    def _configure_threads(cpu_threads: int) -> None:
        """Scoped note: torch thread knobs are PROCESS-global. We set them
        once (first TalkNet use), log the previous value, and never touch
        them again — no per-window/per-face fiddling."""
        if _THREADS_CONFIGURED["done"]:
            return
        try:
            import torch

            previous = torch.get_num_threads()
            wanted = max(1, int(cpu_threads))
            if previous != wanted:
                torch.set_num_threads(wanted)
            _THREADS_CONFIGURED.update(done=True, previous=previous)
            logger.info("[TALKNET_PERF] cpu_threads=%d (was %s)",
                        wanted, previous)
        except Exception as exc:
            logger.warning("[TALKNET_PERF] thread_config_skipped: %s", exc)

    @staticmethod
    def _build(torch, model_path: Path, device) -> Any:
        from ._upstream.talkNetModel import talkNetModel

        net = talkNetModel()
        raw = torch.load(str(model_path), map_location="cpu")
        state = net.state_dict()
        loaded, skipped = 0, 0
        for name, param in raw.items():
            short = name[6:] if name.startswith("model.") else name
            if short in state and state[short].shape == param.shape:
                state[short].copy_(param)
                loaded += 1
            else:
                skipped += 1
        if loaded == 0:
            raise RuntimeError("checkpoint incompatible: no matching tensors")
        net.load_state_dict(state, strict=False)
        # Speaking head (upstream lossAV.FC 256->2).
        head_w = raw.get("lossAV.FC.weight")
        head_b = raw.get("lossAV.FC.bias")
        if head_w is None or head_b is None:
            raise RuntimeError("checkpoint missing lossAV.FC speaking head")
        head = torch.nn.Linear(256, 2)
        head.weight.data.copy_(head_w)
        head.bias.data.copy_(head_b)
        net.speaking_head = head
        net = net.to(device).eval()
        for param in net.parameters():
            param.requires_grad_(False)
        logger.info("[TALKNET_MODEL] status=loaded tensors=%d skipped=%d",
                    loaded, skipped)
        return net

    def infer(self, visual: Any, audio: Any) -> Any:
        """(B,T,112,112) faces + (B,T*4,13) MFCC -> (B,T) speaking probs."""
        torch = self.torch
        visual = visual.to(self.device, dtype=torch.float32)
        audio = audio.to(self.device, dtype=torch.float32)
        model = self.model
        with torch.inference_mode():
            audio_embed = model.forward_audio_frontend(audio)
            visual_embed = model.forward_visual_frontend(visual)
            audio_embed, visual_embed = model.forward_cross_attention(
                audio_embed, visual_embed)
            outs = model.forward_audio_visual_backend(audio_embed, visual_embed)
            logits = model.speaking_head(outs)
            probs = torch.softmax(logits.float(), dim=-1)[..., 1]
        batch = visual.shape[0]
        frames = visual.shape[1]
        return probs.reshape(batch, frames).detach().cpu()

    def warmup(self) -> float:
        """One dummy forward so first real window pays no init cost."""
        torch = self.torch
        started = time.perf_counter()
        visual = torch.zeros((1, 8, 112, 112))
        audio = torch.zeros((1, 32, 13))
        try:
            self.infer(visual, audio)
        except Exception as exc:
            raise RuntimeError(f"talknet_warmup_failed: {exc}") from exc
        return (time.perf_counter() - started) * 1000.0

    def release(self) -> None:
        """Drop tensors; the registry entry stays (model reused)."""
        try:
            import torch

            if str(self.device).startswith("cuda"):
                torch.cuda.empty_cache()
        except Exception:
            pass


def get_model(model_path: Path, device: str,
              cpu_threads: int = 2) -> TalkNetInferenceModel:
    """Process-wide reusable instance (exactly one per path+device)."""
    key = (str(model_path), device)
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(key)
        if existing is not None:
            return existing
        logger.info("[TALKNET_MODEL] status=loading device=%s", device)
        instance = TalkNetInferenceModel(model_path, device, cpu_threads)
        _REGISTRY[key] = instance
        return instance


def release_all() -> None:
    with _REGISTRY_LOCK:
        for instance in _REGISTRY.values():
            try:
                instance.release()
            except Exception:
                pass
        _REGISTRY.clear()
