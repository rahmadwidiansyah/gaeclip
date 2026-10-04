from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .curation import deterministic_candidates
from .pipeline import generate_from_url
from .errors import GaeclipError
from .subtitles import parse_srt
from .url import validate_youtube_url
from .visual import FaceObservation, build_speaker_timeline, make_crop_instructions
from .vision_adapters import MediaPipeFaceDetector, YOLOFaceDetector, benchmark_detector, torch_accelerator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gaeclip local Release A tools")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-url")
    validate.add_argument("url")
    parse = sub.add_parser("parse-srt")
    parse.add_argument("path", type=Path)
    parse.add_argument("--language", default="id")
    generate = sub.add_parser("generate", help="Download a YouTube video and render static vertical clips")
    generate.add_argument("url")
    generate.add_argument("--language", default="id")
    generate.add_argument("--yt-dlp", default="yt-dlp")
    generate.add_argument("--no-subtitles", action="store_true", help="Render clips without burned-in subtitles")
    generate.add_argument(
        "--subtitle-style",
        choices=["bold_pop", "dynamic_highlight", "casual_center", "elegant_serif"],
        default="casual_center",
    )
    generate.add_argument(
        "--subtitle-position", choices=["center", "bottom_center", "bottom"], default="bottom_center",
    )
    generate.add_argument("--observations", type=Path, help="Visual observations JSON for dynamic crop")
    generate.add_argument(
        "--transcript-source",
        choices=["youtube_first", "whisper_only", "youtube_only"],
        default=None,
        help="Sumber transkrip (default ikut Settings TRANSCRIPT_SOURCE)",
    )
    visual = sub.add_parser("analyze-visual", help="Build speaker timeline from visual/audio observations")
    visual.add_argument("path", type=Path, help="JSON array of FaceObservation objects")
    visual.add_argument("--duration", type=float, required=True)
    visual.add_argument("--switch-seconds", type=float, default=2)
    benchmark = sub.add_parser("benchmark-vision", help="Benchmark a real face detector on CPU or GPU")
    benchmark.add_argument("--detector", choices=("mediapipe", "yolo"), default="mediapipe")
    benchmark.add_argument("--weights", default="models/yolo26n-face.pt")
    benchmark.add_argument("--mediapipe-model", default="models/blaze_face_short_range.tflite")
    benchmark.add_argument("--device", default="auto")
    benchmark.add_argument("--frames", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
        if args.command == "validate-url":
            print(validate_youtube_url(args.url))
        elif args.command == "parse-srt":
            lines = parse_srt(args.path.read_text(encoding="utf-8"), language=args.language)
            candidates = deterministic_candidates(
                lines,
                min_duration=settings.min_clip_duration_seconds,
                max_duration=settings.max_clip_duration_seconds,
                max_clips=settings.max_clips,
            )
            print(json.dumps({
                "schema_version": 1,
                "lines": [line.model_dump(mode="json") for line in lines],
                "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
            }, ensure_ascii=False, indent=2))
        elif args.command == "generate":
            observations = None
            if args.observations:
                observations = [
                    FaceObservation.model_validate(item)
                    for item in json.loads(args.observations.read_text(encoding="utf-8"))
                ]
            if args.transcript_source:
                import dataclasses

                settings = dataclasses.replace(settings, transcript_source=args.transcript_source)
            result = generate_from_url(
                args.url, settings=settings, language=args.language, yt_dlp=args.yt_dlp,
                observations=observations, burn_subtitles=not args.no_subtitles,
                subtitle_style=args.subtitle_style, subtitle_position=args.subtitle_position,
            )
            print(json.dumps({
                "job_id": result.job_id,
                "source": str(result.source),
                "curation_source": result.curation.source,
                "candidates": [
                    {"candidate_id": clip.candidate_id, "path": str(clip.path)}
                    for clip in result.clips
                ],
            }, ensure_ascii=False, indent=2))
        elif args.command == "analyze-visual":
            raw_observations = json.loads(args.path.read_text(encoding="utf-8"))
            observations = [FaceObservation.model_validate(item) for item in raw_observations]
            timeline = build_speaker_timeline(
                observations, duration=args.duration, min_switch_seconds=args.switch_seconds,
            )
            crops = make_crop_instructions(timeline, observations)
            print(json.dumps({
                "schema_version": 1,
                "timeline": [item.model_dump(mode="json") for item in timeline],
                "crops": [item.model_dump(mode="json") for item in crops],
            }, ensure_ascii=False, indent=2))
        else:
            try:
                import numpy as np
            except ImportError as exc:
                raise GaeclipError("Install gaeclip[vision] before benchmarking") from exc
            if args.frames <= 0:
                raise ValueError("--frames must be positive")
            detector = (
                MediaPipeFaceDetector(model_path=args.mediapipe_model)
                if args.detector == "mediapipe"
                else YOLOFaceDetector(args.weights, device=args.device)
            )
            frames = [np.zeros((720, 1280, 3), dtype=np.uint8) for _ in range(args.frames)]
            print(json.dumps(benchmark_detector(detector, frames), indent=2))
        return 0
    except (GaeclipError, OSError, ValueError) as exc:
        print(f"{getattr(exc, 'code', 'ERROR')}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
