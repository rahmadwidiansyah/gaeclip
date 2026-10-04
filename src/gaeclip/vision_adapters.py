from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .errors import ExternalCommandError
from .visual import BoundingBox, FaceObservation


def torch_accelerator() -> str:
    """Return the usable PyTorch accelerator, including AMD ROCm builds."""
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "rocm" if getattr(torch.version, "hip", None) else "cuda"
    return "cpu"


def _require(module: str, extra: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise ExternalCommandError(
            f"{module} is not installed; install gaeclip[{extra}]"
        ) from exc


class YOLOFaceDetector:
    """Ultralytics YOLO face detector adapter.

    The supplied weights must be a face-detection checkpoint, not a generic
    COCO detector. Each result is converted to normalized face observations.
    """

    def __init__(self, weights: str = "yolov8n-face.pt", device: str = "auto"):
        ultralytics = _require("ultralytics", "vision")
        self.device = self._device(device)
        self.model = ultralytics.YOLO(weights)

    @staticmethod
    def _device(device: str) -> str:
        if device != "auto":
            return device
        return "cuda:0" if torch_accelerator() in {"cuda", "rocm"} else "cpu"

    def detect(self, frame: Any, timestamp: float) -> list[FaceObservation]:
        height, width = frame.shape[:2]
        result = self.model.predict(frame, device=self.device, verbose=False)[0]
        observations = []
        for index, box in enumerate(result.boxes):
            x1, y1, x2, y2 = (float(value) for value in box.xyxy[0].tolist())
            confidence = float(box.conf[0])
            observations.append(FaceObservation(
                timestamp=timestamp,
                speaker_id=f"face-{index:03d}",
                face=BoundingBox(
                    x=max(0, x1) / width, y=max(0, y1) / height,
                    width=min(width, x2) / width - max(0, x1) / width,
                    height=min(height, y2) / height - max(0, y1) / height,
                ),
                lip_score=0,
                audio_score=0,
                confidence=confidence,
            ))
        return observations


class MediaPipeFaceDetector:
    """MediaPipe Face Detection adapter for a lightweight CPU baseline."""

    def __init__(
        self,
        model_path: str = "models/blaze_face_short_range.tflite",
        min_detection_confidence: float = 0.4,
    ):
        mp = _require("mediapipe", "vision")
        path = Path(model_path)
        if not path.is_file():
            raise ExternalCommandError(
                f"MediaPipe model not found at {path}; download the face detector .tflite asset"
            )
        options = mp.tasks.vision.FaceDetectorOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(path)),
            min_detection_confidence=min_detection_confidence,
        )
        self._mp = mp
        self._detector = mp.tasks.vision.FaceDetector.create_from_options(options)

    def detect(self, frame: Any, timestamp: float) -> list[FaceObservation]:
        height, width = frame.shape[:2]
        image = self._mp.Image(
            image_format=self._mp.ImageFormat.SRGB,
            data=frame,
        )
        result = self._detector.detect(image)
        observations = []
        for index, detection in enumerate(result.detections or []):
            box = detection.bounding_box
            x, y = max(0, box.origin_x / width), max(0, box.origin_y / height)
            w, h = min(1 - x, box.width / width), min(1 - y, box.height / height)
            observations.append(FaceObservation(
                timestamp=timestamp,
                speaker_id=f"face-{index:03d}",
                face=BoundingBox(x=x, y=y, width=w, height=h),
                lip_score=0, audio_score=0,
                confidence=float(detection.categories[0].score),
            ))
        return observations


class TorchScriptActiveSpeakerScorer:
    """Adapter for exported TalkNet/SyncNet TorchScript checkpoints.

    The checkpoint must accept the preprocessed visual/audio tensors and return
    one scalar score per window. This keeps legacy TalkNet/SyncNet repositories
    isolated while making their real checkpoints usable by the pipeline.
    """

    def __init__(self, checkpoint: str | Path, device: str = "auto"):
        torch = _require("torch", "vision")
        self.torch = torch
        if device == "auto":
            device = "cuda" if torch_accelerator() in {"cuda", "rocm"} else "cpu"
        self.device = torch.device(device)
        self.model = torch.jit.load(str(checkpoint), map_location=self.device).eval()

    def score(self, visual_tensor: Any, audio_tensor: Any) -> float:
        with self.torch.inference_mode():
            visual = visual_tensor.to(self.device)
            audio = audio_tensor.to(self.device)
            result = self.model(visual, audio)
            if isinstance(result, (tuple, list)):
                result = result[0]
            return float(result.detach().reshape(-1).mean().item())


def benchmark_detector(detector: Any, frames: list[Any], fps: float = 25) -> dict[str, float | str]:
    start = time.perf_counter()
    detections = sum(len(detector.detect(frame, index / fps)) for index, frame in enumerate(frames))
    elapsed = time.perf_counter() - start
    return {
        "device": getattr(detector, "device", "cpu"),
        "frames": float(len(frames)),
        "detections": float(detections),
        "elapsed_seconds": elapsed,
        "fps": len(frames) / elapsed if elapsed else 0,
    }
