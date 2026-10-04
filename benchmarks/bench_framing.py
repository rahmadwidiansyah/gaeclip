#!/usr/bin/env python3
"""Framing benchmark harness (Phase 6).

Measures per-stage cost on synthetic clips: analysis (decode+VAD+detect
stubbed by static content) and static render throughput, with honest
per-case peak RSS (each case runs in a SUBPROCESS; the child reports its
own ru_maxrss, since in-process peaks are cumulative and misleading).

Nothing here asserts correctness (that's the test suite); this is a
tuning instrument. Requires ffmpeg/ffprobe on PATH.

Usage:
    .venv/bin/python benchmarks/bench_framing.py
    .venv/bin/python benchmarks/bench_framing.py --quick   # single small case
"""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CASES = [
    # (label, width, height, seconds)
    ("360p-6s", 640, 360, 6),
    ("720p-15s", 1280, 720, 15),
    ("1080p-30s", 1920, 1080, 30),
]


def sh(cmd: list[str], timeout: int = 600) -> None:
    subprocess.run(cmd, check=True, timeout=timeout,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_source(tmp: Path, width: int, height: int, seconds: int) -> Path:
    src = tmp / f"src_{width}x{height}_{seconds}s.mp4"
    if src.exists():
        return src
    sh(["ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(src)])
    return src


def child_run(case_label: str, src: str, seconds: int, dev: bool) -> dict:
    """Run one case in-process (called in the child); returns measurements."""
    import os

    if dev:
        os.environ["GAECLIP_RENDER_PROFILE"] = "dev"
    from gaeclip.autoframing import analyze_section
    from gaeclip.media import render_static_vertical

    t0 = time.monotonic()
    plan = analyze_section(src, float(seconds))
    t_analyze = time.monotonic() - t0
    out = Path(src).parent / f"out_{case_label}.mp4"
    t1 = time.monotonic()
    render_static_vertical(Path(src), out, 0, float(seconds), preset="veryfast")
    t_render = time.monotonic() - t1
    return {
        "case": case_label,
        "analysis_s": round(t_analyze, 2),
        "render_s": round(t_render, 2),
        "frames_decoded": plan.perf.get("frames_decoded"),
        "frames_analyzed": plan.perf.get("frames_analyzed"),
        "frames_skipped": plan.perf.get("frames_skipped"),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("ffmpeg/ffprobe required")
        return 2
    cases = [CASES[0]] if args.quick else CASES
    print(f"{'case':<10} {'analyze':>8} {'render':>7} {'dec':>5} {'anl':>5} {'skp':>5} {'peakRSS':>8}")
    with tempfile.TemporaryDirectory(prefix="gaeclip-bench-") as tmp:
        tmp_path = Path(tmp)
        for label, width, height, seconds in cases:
            src = make_source(tmp_path, width, height, seconds)
            code = (
                "import json,sys; sys.path.insert(0, %r);"
                "from benchmarks.bench_framing import child_run;"
                "print(json.dumps(child_run(%r, %r, %r, True)))"
                % (str(SRC), label, str(src), seconds)
            )
            proc = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True,
                timeout=900, cwd=str(REPO_ROOT),
            )
            if proc.returncode != 0:
                print(f"{label:<10} FAILED: {proc.stderr[-500:]}")
                continue
            row = json.loads(proc.stdout.strip().splitlines()[-1])
            print(f"{row['case']:<10} {row['analysis_s']:>7}s {row['render_s']:>6}s "
                  f"{row['frames_decoded']:>5} {row['frames_analyzed']:>5} "
                  f"{row['frames_skipped']:>5} {row['peak_rss_mb']:>7}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
