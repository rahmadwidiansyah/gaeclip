"""Post-render QC for dynamic framing (Phase 4). Pure checks + ffprobe.

Never raises on check failure: every check returns a ``QCCheck`` verdict
and ``qc_clip`` wraps even probing itself, so QC output can never fail a
job — the pipeline hook is strictly log-only. Strict where it matters
(geometry exactness), tolerant where the pipeline is inherently fuzzy
(duration within max(0.5s, 5%)).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DURATION_TOL_ABS = 0.5
DURATION_TOL_REL = 0.05
AV_SYNC_TOL = 0.5


@dataclass
class QCCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class QCReport:
    clip_id: str
    checks: list[QCCheck] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def summary(self) -> str:
        lines = ["QC SUMMARY", f"clip: {self.clip_id}",
                 f"result: {'PASS' if self.passed else 'FAIL'}"]
        for check in self.checks:
            flag = "ok" if check.passed else "FAIL"
            lines.append(f"  [{flag}] {check.name}"
                         + (f": {check.detail}" if check.detail else ""))
        for key, value in self.metrics.items():
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)


def probe_json(path: Path) -> dict:
    """Full ffprobe JSON (streams + format). Raises on probe failure."""
    from .media import run_command

    result = run_command([
        "ffprobe", "-hide_banner", "-v", "error",
        "-print_format", "json", "-show_streams", "-show_format",
        str(path),
    ])
    raw = result.stdout
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    return json.loads(text)


def _video_audio_streams(info: dict) -> tuple[dict | None, dict | None]:
    video = audio = None
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video" and video is None:
            video = stream
        elif stream.get("codec_type") == "audio" and audio is None:
            audio = stream
    return video, audio


def check_output_geometry(info: dict, exp_w: int, exp_h: int) -> list[QCCheck]:
    """Strict geometry: exact resolution, SAR 1:1, yuv420p, h264, audio."""
    video, audio = _video_audio_streams(info)
    checks: list[QCCheck] = []
    if video is None:
        return [QCCheck("video_stream", False, "no video stream")]
    width, height = int(video.get("width", 0)), int(video.get("height", 0))
    checks.append(QCCheck(
        "resolution", (width, height) == (exp_w, exp_h),
        f"{width}x{height} expected {exp_w}x{exp_h}"))
    sar = video.get("sample_aspect_ratio", "1:1")
    checks.append(QCCheck("sar", sar in ("1:1", "0:1", ""),
                          f"SAR={sar}"))
    pix_fmt = video.get("pix_fmt", "")
    checks.append(QCCheck("pix_fmt", pix_fmt == "yuv420p", f"{pix_fmt}"))
    codec = video.get("codec_name", "")
    checks.append(QCCheck("vcodec", codec == "h264", f"{codec}"))
    dar = video.get("display_aspect_ratio", "")
    checks.append(QCCheck("dar_9_16", dar in ("9:16", ""),
                          f"DAR={dar or 'unknown'}"))
    if audio is None:
        checks.append(QCCheck("audio_stream", False, "no audio stream"))
    else:
        checks.append(QCCheck("audio_stream", True,
                              f"{audio.get('codec_name', '?')} "
                              f"{audio.get('sample_rate', '?')}Hz"))
    return checks


def check_duration(actual: float, expected: float,
                   tol_abs: float = DURATION_TOL_ABS,
                   tol_rel: float = DURATION_TOL_REL) -> QCCheck:
    """Tight duration check: |actual-expected| <= max(tol_abs, tol_rel*expected)."""
    tolerance = max(tol_abs, expected * tol_rel)
    delta = abs(actual - expected)
    return QCCheck("duration", delta <= tolerance,
                   f"{actual:.2f}s vs {expected:.2f}s (tol {tolerance:.2f}s)")


def check_output_fps(info: dict, expected: float | None) -> QCCheck | None:
    """Output fps matches the source-native policy (None = skip)."""
    if expected is None:
        return None
    video, _ = _video_audio_streams(info)
    if video is None:
        return QCCheck("fps", False, "no video stream")

    def _rate(value: object) -> float | None:
        try:
            text = str(value)
            if "/" in text:
                num, den = text.split("/", 1)
                rate = float(num) / float(den)
            else:
                rate = float(text)
        except (ValueError, ZeroDivisionError, TypeError):
            return None
        return rate if rate > 0 else None

    actual = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    if actual is None:
        return QCCheck("fps", False, "fps unknown")
    ok = abs(actual - expected) <= max(0.5, expected * 0.02)
    return QCCheck("fps", ok, f"{actual:.2f}fps expected {expected:.2f}fps")


def check_av_sync(video_dur: float | None, audio_dur: float | None,
                  tol: float = AV_SYNC_TOL) -> QCCheck:
    if video_dur is None or audio_dur is None:
        return QCCheck("av_sync", True, "stream duration unavailable")
    delta = abs(video_dur - audio_dur)
    return QCCheck("av_sync", delta <= tol, f"Δ={delta:.2f}s")


def instruction_metrics(instructions: list,
                        expected_duration: float) -> dict:
    """Trajectory-level metrics from the rendered instruction list."""
    metrics: dict = {"instructions": len(instructions)}
    if not instructions:
        metrics.update(switches=0, static_segments=0, eased_waypoints=0,
                       coverage=0.0, max_gap_s=0.0, max_overlap_s=0.0)
        return metrics
    ordered = sorted(instructions, key=lambda item: item.start)
    switches = sum(
        1 for prev, cur in zip(ordered, ordered[1:])
        if prev.speaker_id != cur.speaker_id
        and "static" not in (prev.speaker_id, cur.speaker_id))
    metrics["switches"] = switches
    metrics["static_segments"] = sum(
        1 for item in ordered if item.speaker_id == "static")
    coverage = sum(max(0.0, min(item.end, expected_duration) - item.start)
                   for item in ordered)
    metrics["coverage"] = round(coverage, 3)
    max_gap = max_overlap = 0.0
    for prev, cur in zip(ordered, ordered[1:]):
        gap = cur.start - prev.end
        if gap > 0:
            max_gap = max(max_gap, gap)
        else:
            max_overlap = max(max_overlap, -gap)
    metrics["max_gap_s"] = round(max_gap, 3)
    metrics["max_overlap_s"] = round(max_overlap, 3)
    return metrics


def trajectory_metrics(points: list) -> dict:
    """Velocity/acceleration/jerk stats over raw trajectory points."""
    metrics: dict = {"trajectory_points": len(points)}
    if not points:
        return metrics
    metrics["eased_points"] = sum(1 for p in points if p.eased)
    velocities = [abs(p.vx) + abs(p.vy) for p in points]
    metrics["mean_crop_velocity"] = round(
        sum(velocities) / len(velocities), 4)
    metrics["max_crop_velocity"] = round(max(velocities), 4)
    accels: list[float] = []
    jerks: list[float] = []
    prev: tuple | None = None  # (t, vx, vy)
    prev_accel: tuple | None = None  # (t, ax, ay)
    for point in points:
        timestamp = getattr(point, "timestamp", None)
        if timestamp is None:
            continue
        if prev is not None and timestamp > prev[0]:
            dt = timestamp - prev[0]
            ax = (point.vx - prev[1]) / dt
            ay = (point.vy - prev[2]) / dt
            accels.append(abs(ax) + abs(ay))
            if prev_accel is not None and timestamp > prev_accel[0]:
                adt = timestamp - prev_accel[0]
                jerks.append((abs(ax - prev_accel[1]) + abs(ay - prev_accel[2])) / adt)
            prev_accel = (timestamp, ax, ay)
        prev = (timestamp, point.vx, point.vy)
    if accels:
        metrics["mean_crop_acceleration"] = round(sum(accels) / len(accels), 4)
        metrics["max_crop_acceleration"] = round(max(accels), 4)
    if jerks:
        metrics["max_crop_jerk"] = round(max(jerks), 4)
    return metrics


def max_upscale_of(instructions: list, src_w: int, src_h: int,
                   out_w: int = 1080) -> float | None:
    """Largest implied uniform upscale across instructions (None if empty)."""
    from .framing_geometry import upscale_factor

    if not instructions or src_w <= 0:
        return None
    return max(upscale_factor(item.width * src_w, out_w) for item in instructions)


def qc_clip(output: Path, clip_id: str, expected_duration: float,
            exp_w: int = 1080, exp_h: int = 1920,
            exp_fps: float | None = None,
            instructions: list | None = None,
            trajectory: list | None = None,
            src_w: int | None = None, src_h: int | None = None
            ) -> QCReport:
    """Full post-render QC. Never raises: probe failures become checks."""
    report = QCReport(clip_id=clip_id)
    try:
        info = probe_json(output)
    except Exception as exc:
        report.checks.append(QCCheck("probe", False, str(exc)[:160]))
        return report
    report.checks.append(QCCheck("probe", True, ""))
    report.checks.extend(check_output_geometry(info, exp_w, exp_h))
    fps_check = check_output_fps(info, exp_fps)
    if fps_check is not None:
        report.checks.append(fps_check)
    fmt_duration = info.get("format", {}).get("duration")
    try:
        actual = float(fmt_duration) if fmt_duration is not None else -1.0
    except (TypeError, ValueError):
        actual = -1.0
    if actual < 0:
        report.checks.append(QCCheck("duration", False, "duration unknown"))
    else:
        report.checks.append(check_duration(actual, expected_duration))

    def _stream_dur(stream: dict | None) -> float | None:
        if not stream:
            return None
        for key in ("duration",):
            try:
                return float(stream[key])
            except (KeyError, TypeError, ValueError):
                continue
        return None

    video, audio = _video_audio_streams(info)
    report.checks.append(check_av_sync(_stream_dur(video), _stream_dur(audio)))
    if video is not None:
        report.metrics["final_media"] = {
            "width": video.get("width"), "height": video.get("height"),
            "fps": video.get("avg_frame_rate") or video.get("r_frame_rate"),
            "sar": video.get("sample_aspect_ratio"),
            "dar": video.get("display_aspect_ratio"),
            "codec": video.get("codec_name"),
            "pixel_format": video.get("pix_fmt"),
            "audio_codec": (audio or {}).get("codec_name"),
            "duration": info.get("format", {}).get("duration"),
        }
    report.metrics.update(instruction_metrics(instructions or [], expected_duration))
    if trajectory is not None:
        report.metrics.update(trajectory_metrics(trajectory))
    if instructions and src_w and src_h:
        factor = max_upscale_of(instructions, src_w, src_h, out_w=exp_w)
        if factor is not None:
            report.metrics["max_upscale"] = round(factor, 2)
            report.checks.append(QCCheck(
                "upscale_cap", factor <= 2.0, f"{factor:.2f}x"))
    return report


def dump_debug_artifacts(output: Path, plan) -> list[str]:
    """Write trajectory JSON beside the clip (debug mode only). Returns paths."""
    from .smoothing import trajectory_to_json

    written: list[str] = []
    trajectory = getattr(plan, "trajectory", []) or []
    payload = {
        "scenes": list(getattr(plan, "scenes", []) or []),
        "mode": getattr(plan, "mode", ""),
        "reason": getattr(plan, "reason", ""),
        "trajectory": trajectory_to_json(trajectory),
        "evidence": list(getattr(plan, "evidence_log", []) or []),
        "metrics": dict(getattr(plan, "metrics", {}) or {}),
        "keyframe_stats": dict(getattr(plan, "keyframe_stats", {}) or {}),
        "instructions": [
            dict(start=item.start, end=item.end, speaker_id=item.speaker_id,
                 center_x=item.center_x, center_y=item.center_y,
                 width=item.width, height=item.height, layout=item.layout)
            for item in (getattr(plan, "instructions", []) or [])],
    }
    target = output.with_name(f"{output.stem}_trajectory.json")
    target.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    written.append(str(target))
    return written
