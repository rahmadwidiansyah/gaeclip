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
    generate.add_argument(
        "--subtitle-max-words", type=int, default=None,
        help="Maks kata per baris subtitle 1-3 (default 3; di atas 3 dinormalisasi)",
    )
    generate.add_argument("--brand", action="store_true", help="Aktifkan clip branding")
    generate.add_argument(
        "--brand-template",
        choices=["glass_end", "minimal_corner", "creator_tag", "creator_outro"],
        default="glass_end",
    )
    generate.add_argument("--brand-channel", default=None, help="Nama channel untuk branding")
    generate.add_argument("--brand-avatar", default=None, help="Asset id avatar (lihat /api/v1/brand-assets)")
    generate.add_argument(
        "--brand-position",
        choices=["top_left", "top_right", "bottom_left", "bottom_right", "top_center", "center"],
        default="top_left",
    )
    generate.add_argument("--brand-duration", type=float, default=2.0)
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
    retention = sub.add_parser("retention", help="Run storage retention scan (artifacts + intermediate)")
    retention.add_argument("--dry-run", action="store_true", help="Show what would be removed without deleting")
    backup = sub.add_parser("backup", help="Create a compressed tarball of STORAGE_ROOT")
    backup.add_argument("output", type=Path, help="Path to output .tar.gz file")
    backup.add_argument("--exclude-audit", action="store_true", help="Exclude audit.jsonl from backup")
    restore = sub.add_parser("restore", help="Restore STORAGE_ROOT from a backup tarball")
    restore.add_argument("input", type=Path, help="Path to backup .tar.gz file")
    restore.add_argument("--target", type=Path, default=None, help="Target directory (default: current STORAGE_ROOT)")
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
        elif args.command == "retention":
            from .app_settings import AppSettings
            from .retention import run as retention_run
            service = AppSettings(None)
            artifact_hours = float(service.get("ARTIFACT_RETENTION_HOURS") or 0)
            intermediate_hours = float(service.get("INTERMEDIATE_RETENTION_HOURS") or 0)
            result = retention_run(
                storage_root=settings.storage_root,
                artifact_retention_hours=artifact_hours,
                intermediate_retention_hours=intermediate_hours,
                dry_run=bool(args.dry_run),
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == "backup":
            import tarfile
            storage = settings.storage_root
            if not storage.exists():
                raise GaeclipError(f"STORAGE_ROOT does not exist: {storage}")
            output = args.output
            if output.suffix != ".gz" or output.suffixes != [".tar", ".gz"]:
                output = output.parent / (output.name + ".tar.gz")
            exclude = ["audit.jsonl"] if args.exclude_audit else []
            with tarfile.open(output, "w:gz") as tar:
                for item in storage.iterdir():
                    arcname = item.name
                    if arcname in exclude:
                        continue
                    tar.add(item, arcname=arcname)
            print(f"Backup created: {output} ({output.stat().st_size} bytes)")
        elif args.command == "restore":
            import tarfile
            input_path = args.input
            if not input_path.exists():
                raise GaeclipError(f"Backup file not found: {input_path}")
            target = args.target or settings.storage_root
            target.mkdir(parents=True, exist_ok=True)
            with tarfile.open(input_path, "r:gz") as tar:
                # Security: reject absolute paths and traversal.
                for member in tar.getmembers():
                    if member.name.startswith("/") or ".." in member.name:
                        raise GaeclipError(f"Unsafe path in archive: {member.name}")
                tar.extractall(path=target)
            print(f"Restored to {target}")
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
                subtitle_max_words_per_line=args.subtitle_max_words,
                branding={
                    "enabled": bool(args.brand),
                    "template": args.brand_template,
                    "channel_name": args.brand_channel,
                    "avatar_asset": args.brand_avatar,
                    "position": args.brand_position,
                    "duration": args.brand_duration,
                },
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
