# Visual model setup

## Face detection

`gaeclip.vision_adapters` contains two real detector adapters:

- `MediaPipeFaceDetector`: lightweight CPU baseline.
- `YOLOFaceDetector`: Ultralytics inference with an explicitly supplied face
  checkpoint and `auto`, `cpu`, or `cuda:0` device selection.

Install them with `pip install -e ".[vision]"`. Do not use a generic COCO checkpoint with `YOLOFaceDetector`; it must contain a
face class. The local checkpoint is `models/yolo26n-face.pt`; verify its
SHA-256 against the value documented in the README.

## Active-speaker scoring

TalkNet-ASD and SyncNet are not maintained PyPI dependencies and their
checkpoints have independent terms. Their source trees are available under
`models/TalkNet-ASD` and `models/syncnet_python`. Keep checkpoints outside the
application package, export a TorchScript module with two inputs
(`visual_tensor`, `audio_tensor`), and use:

```python
from gaeclip.vision_adapters import TorchScriptActiveSpeakerScorer

scorer = TorchScriptActiveSpeakerScorer("models/talknet.pt", device="auto")
score = scorer.score(face_window, audio_window)
```

The export boundary is intentional: TalkNet and SyncNet have different legacy
preprocessing and output shapes. A model-specific preprocessing adapter must
produce the tensors expected by the selected checkpoint; the scorer will not
silently guess or substitute a heuristic score.

## Benchmark

```bash
python -m gaeclip benchmark-vision --detector mediapipe --device auto --frames 200
python -m gaeclip benchmark-vision --detector yolo \
  --weights models/yolo26n-face.pt --device auto --frames 200
```

(`--device` defaults to `auto` in `cli.py`; use `cpu` or `cuda:0` to force.
`auto` maps to `cuda:0` only when a CUDA/ROCm accelerator is usable,
otherwise `cpu`.)

Compare CPU and GPU runs on the same host, model, frame size, and frame count.
The benchmark uses synthetic frames to measure detector overhead, not detection
accuracy. Accuracy must be evaluated separately on representative clips.

### AMD/ROCm

This machine has an AMD Radeon iGPU and `/dev/kfd`, but the installed PyTorch
wheel is a CUDA build (`torch.version.hip is None`), so it cannot use Radeon.
`clinfo` exposes the GPU through Mesa Rusticl OpenCL; that is not a ROCm
PyTorch runtime. The current benchmark therefore has no valid AMD GPU result.

For a supported AMD ROCm host, install a ROCm PyTorch wheel before installing
the application vision extra:

```bash
python -m venv .venv-rocm
.venv-rocm/bin/pip install torch==2.9.1 torchvision \
  --index-url https://download.pytorch.org/whl/rocm6.3
.venv-rocm/bin/pip install -e ".[vision]"
.venv-rocm/bin/python -c \
  "import torch; print(torch.version.hip, torch.cuda.is_available())"
```

PyTorch intentionally keeps the ROCm API under `torch.cuda`; the adapter
detects ROCm through `torch.version.hip` and maps `--device auto` to that
backend. This Radeon iGPU may still be unsupported by the selected ROCm
release; verify the official ROCm GPU compatibility matrix before expecting
GPU inference.

The ROCm stack is now installed separately in `.venv-rocm` with
`torch==2.9.1+rocm6.3` and `torchvision==0.24.1+rocm6.3`; PyTorch reports HIP
6.3 and one AMD Radeon device. A YOLO inference attempt on this Renoir iGPU
caused a ROCm `GPU Hang`, so the GPU benchmark is intentionally not reported as
valid. This indicates a runtime/device compatibility problem, not a successful
performance result. Do not use `HSA_OVERRIDE_GFX_VERSION` as a production fix;
use a ROCm release and GPU target listed as supported by AMD, or keep the
pipeline on CPU.

## This environment

The optional CPU stack was installed into `.venv` with PyTorch 2.14.0+cu130,
Ultralytics 8.4.150, and MediaPipe 0.10.35. CUDA is not available on this
host (`torch.cuda.is_available() == false`), so a GPU result cannot be claimed.
Measured synthetic-frame CPU baselines at 1280x720:

| Detector | Frames | FPS |
| --- | ---: | ---: |
| MediaPipe BlazeFace | 100 | 240.53 |
| YOLO26n face | 100 | 9.23 |

TalkNet TalkSet checkpoint and SyncNet assets are also present locally:

- `models/TalkNet-ASD/pretrain_TalkSet.model`
- `models/syncnet_python/data/syncnet_v2.model`
- `models/syncnet_python/detectors/s3fd/weights/sfd_face.pth`

The upstream TalkNet/SyncNet scripts still own model-specific face tracking,
audio MFCC extraction, and 25-fps preprocessing. The application adapter keeps
that boundary explicit rather than silently feeding incompatible tensors to a
checkpoint.

## Implementation notes (2026-09-14)

- Active score: `(lip_score*0.55 + audio_score*0.45) * confidence`
  (`visual.py`); timeline params `min_switch_seconds=2.0`,
  `min_active_score=0.25`, `sample_step=0.1` — simplified hysteresis without
  separate enter/exit thresholds or smoothing.
- Verified present: `models/yolo26n-face.pt`; TalkNet/SyncNet trees under
  `models/` are source reference — confirm checkpoint files before claiming
  scorer readiness.
