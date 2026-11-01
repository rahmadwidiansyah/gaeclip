"""Storage retention enforcement.

Deletes finished-job artifacts and intermediate temp files when they have
exceeded their configured age. The scanner is safe for concurrent use: it
only removes files that still exist at the moment of inspection and skips
anything that looks in-use (mtime very recent).

This module is intentionally low-level. Scheduling is left to the CLI
(`gaeclip retention`) or a periodic Celery task; the API does not expose
a public run endpoint so operators control when scans happen.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("gaeclip.retention")


def _age_hours(path: Path) -> float:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0.0
    return (time.time() - mtime) / 3600.0


def _scan_tree(root: Path, pattern: str, max_age_hours: float) -> list[Path]:
    if max_age_hours <= 0 or not root.is_dir():
        return []
    return [p for p in root.rglob(pattern) if _age_hours(p) > max_age_hours]


def run(
    *,
    storage_root: Path | str,
    artifact_retention_hours: float,
    intermediate_retention_hours: float,
    dry_run: bool = False,
) -> dict[str, Any]:
    root = Path(storage_root).expanduser().resolve()
    jobs_dir = root / "jobs"
    tmp_dir = root / "tmp"

    removed: dict[str, int] = {
        "artifacts": 0,
        "intermediate": 0,
        "jobs_hollowed": 0,
    }
    errors: list[str] = []

    def _safe_remove(path: Path) -> None:
        try:
            if dry_run:
                logger.info("DRY-RUN would remove %s", path)
            else:
                path.unlink()
                removed["artifacts"] += 1
        except OSError as exc:
            errors.append(f"artifact remove failed: {path}: {exc}")

    # Finished-job clips (mp4) older than artifact retention.
    if artifact_retention_hours > 0 and jobs_dir.is_dir():
        for job_dir in jobs_dir.iterdir():
            if not job_dir.is_dir():
                continue
            clips = _scan_tree(job_dir, "*.mp4", artifact_retention_hours)
            for clip in clips:
                _safe_remove(clip)
            # If all clips gone and no other artifacts, hollow the job dir.
            remaining = [p for p in job_dir.iterdir() if p.is_file()]
            if not remaining:
                try:
                    job_dir.rmdir()
                    removed["jobs_hollowed"] += 1
                except OSError:
                    pass

    # Intermediate/temp artifacts: transcript.json, source videos, srt, etc.
    if intermediate_retention_hours > 0:
        intermediate_patterns = [
            "*.json",
            "*.srt",
            "*.vtt",
            "*.txt",
            "*.wav",
            "*.opus",
            "*.webm",
            "source/*",
        ]
        for pat in intermediate_patterns:
            for path in _scan_tree(tmp_dir, pat, intermediate_retention_hours):
                try:
                    if dry_run:
                        logger.info("DRY-RUN would remove %s", path)
                    else:
                        path.unlink()
                        removed["intermediate"] += 1
                except OSError as exc:
                    errors.append(f"intermediate remove failed: {path}: {exc}")
            # Also scan inside job/source dirs for downloaded video chunks.
            for src_dir in (jobs_dir.rglob("source")):
                if not src_dir.is_dir():
                    continue
                for path in _scan_tree(src_dir, pat, intermediate_retention_hours):
                    try:
                        if dry_run:
                            logger.info("DRY-RUN would remove %s", path)
                        else:
                            removed["intermediate"] += 1
                            path.unlink()
                    except OSError as exc:
                        errors.append(f"intermediate remove failed: {path}: {exc}")

    if errors:
        logger.warning("retention finished with %d error(s)", len(errors))
    else:
        logger.info("retention: %s", removed)
    return {**removed, "errors": errors, "dry_run": dry_run}
