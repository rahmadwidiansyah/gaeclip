"""TalkNet adaptive active-speaker escalation (Phase 4.2+).

Q1 (who speaks) escalation only: heuristic first, TalkNet for hard cases,
then the EXISTING Target Manager / geometry / renderer decide everything
else. All TalkNet paths degrade to heuristic-only (never fail the job).

Submodules: types (provider abstraction + SyncNet future hook), device
(CUDA>MPS>CPU + VRAM gate), config (central TALKNET_* config), audio
(numpy MFCC), preprocessing (track face crops), model (lazy singleton
inference), detector (cache + batching + metrics), fusion (escalation +
score blend). Upstream architecture lives in _upstream (see NOTICE).
"""

from .config import TalkNetConfig
from .detector import TalkNetDetector
from .device import InferenceDevice, detect_inference_device, resolve_device
from .fusion import decide_escalation, fuse_scores, overlapping_speech
from .types import (ActiveSpeakerProvider, HeuristicSpeakerProvider,
                    SpeakerScore, TalkNetResult)

__all__ = [
    "ActiveSpeakerProvider",
    "HeuristicSpeakerProvider",
    "InferenceDevice",
    "SpeakerScore",
    "TalkNetConfig",
    "TalkNetDetector",
    "TalkNetResult",
    "decide_escalation",
    "detect_inference_device",
    "fuse_scores",
    "overlapping_speech",
    "resolve_device",
]
