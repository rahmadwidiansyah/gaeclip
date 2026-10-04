from __future__ import annotations

import math
import subprocess
from pathlib import Path

from .errors import ExternalCommandError
from .visual import CropInstruction


def subtitle_font_size(width: int = 1080, height: int = 1920) -> int:
    """Responsive subtitle size from the smaller frame dimension.

    Same relative size in portrait, landscape, and square; hard-capped so
    text can never fill the screen, floored so it stays readable.
    """
    return min(44, max(28, round(min(width, height) / 26)))


def subtitle_chars_per_line(width: int = 1080, height: int = 1920) -> int:
    """Characters that fit on one subtitle line at the responsive size."""
    font_size = subtitle_font_size(width, height)
    return max(24, round(width * 0.88 / (font_size * 0.55)))


def subtitle_margins(width: int = 1080, height: int = 1920) -> tuple[int, int]:
    """(margin_lr, margin_v) in pixels; single source for ASS + filter styles.

    Bottom margin is 12% of the frame height: clear of the bottom edge,
    progress bars, and player UI, while staying in the lower safe area.
    """
    return (round(width * 0.06), round(height * 0.12))


def resolve_output_geometry(
    source: Path, max_w: int = 1080, max_h: int = 1920
) -> tuple[int, int]:
    """Output (w, h) for a source file via the vertical ladder."""
    probed = probe_dimensions(source)
    if probed is None:
        return max_w, max_h
    _, _, _, _, out_w, out_h = compute_vertical_ladder(probed[0], probed[1], max_w, max_h)
    return out_w, out_h


def _subtitle_filter(subtitles: Path) -> str:
    """ffmpeg subtitles filter: the ASS file carries PlayRes == frame size
    plus the full style row, so no force_style/original_size overrides.
    (Verified: force_style breaks libass middle alignment and scaling;
    file-only rendering is exact for every style/position/aspect.)"""
    subtitle_path = str(subtitles.resolve()).replace("\\", "/").replace(":", r"\:")
    return f"subtitles=filename='{subtitle_path}'"


VIDEO_CRF = 20
VIDEO_PRESET = "medium"
AUDIO_BITRATE = "128k"


def compute_vertical_ladder(src_w: int, src_h: int,
                            max_w: int = 1080, max_h: int = 1920,
                            ) -> tuple[int, int, int, int, int, int]:
    """Crop-first portrait ladder: (crop_x, crop_y, crop_w, crop_h, out_w, out_h).

    The 9:16 window is cut from SOURCE resolution first, then scaled once to
    the output size. Output is always exactly 9:16, selected from the standard
    portrait ladder below to avoid non-9:16 DAR from even-rounding:
      1920x1080 -> crop 608x1080  -> 1080x1920   (≥540 crop: full HD portrait)
      1280x720  -> crop 405x720   -> 810x1440     (≥405 crop: 810p portrait)
      640x360   -> crop 202x360   -> 405x720      (small: 405p portrait)
      480x270   -> crop 152x270   -> 270x480      (tiny: 270p portrait)

    Output never exceeds max_w/max_h and never upscales the crop more than 2×.
    """
    # Standard 9:16 output sizes (width, height) from largest to smallest.
    # All entries must be even (libx264 yuv420p requirement) and exactly 9:16.
    _PORTRAIT_LADDER = [
        (1080, 1920),
        (810, 1440),
        (720, 1280),
        (540, 960),
        (360, 640),
        (270, 480),
        (180, 320),
    ]

    src_w, src_h = max(2, int(src_w)), max(2, int(src_h))
    crop_w = min(src_w, src_h * 9 / 16)
    crop_h = crop_w * 16 / 9
    if crop_h > src_h:
        crop_h = float(src_h)
        crop_w = crop_h * 9 / 16
    # Round to nearest even.  Use ceiling so e.g. 405 -> 406 (not 404) so the
    # crop stays as close to the ideal 9:16 size as possible without exceeding
    # source bounds.  Down-round only when the up-round would exceed the source.
    crop_w_ceil = math.ceil(crop_w / 2) * 2
    crop_w_floor = int(crop_w) // 2 * 2
    crop_w = crop_w_ceil if crop_w_ceil <= src_w else crop_w_floor
    crop_h_ceil = math.ceil(crop_h / 2) * 2
    crop_h_floor = int(crop_h) // 2 * 2
    crop_h = crop_h_ceil if crop_h_ceil <= src_h else crop_h_floor
    crop_w = max(2, min(crop_w, src_w // 2 * 2))
    crop_h = max(2, min(crop_h, src_h // 2 * 2))
    crop_x = (src_w - crop_w) // 2
    crop_y = (src_h - crop_h) // 2

    # Select the largest standard output that:
    #   (a) does not exceed max_w / max_h
    #   (b) does not upscale the crop more than 2×
    out_w, out_h = 2, 4  # fallback
    for pw, ph in _PORTRAIT_LADDER:
        if pw <= max_w and ph <= max_h and pw <= crop_w * 2:
            out_w, out_h = pw, ph
            break

    out_w, out_h = max(2, out_w), max(2, out_h)
    return crop_x, crop_y, crop_w, crop_h, out_w, out_h


def probe_dimensions(path: Path) -> tuple[int, int] | None:
    """(width, height) of the first video stream, or None when unknown."""
    try:
        info = probe(path)
        width = int(float(info.get("width", 0) or 0))
        height = int(float(info.get("height", 0) or 0))
        if width > 0 and height > 0:
            return width, height
    except Exception:
        pass
    return None


def probe_fps(path: Path) -> tuple[float | None, float | None, bool]:
    """(r_frame_rate, avg_frame_rate, is_vfr) for the first video stream.

    Separate from probe() so its flat-dict contract stays untouched.
    VFR is declared when the two rates disagree by more than 1%.
    """
    try:
        result = run_command([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate,avg_frame_rate",
            "-of", "default=noprint_wrappers=1", str(path),
        ])
    except Exception:
        return None, None, False

    def _rate(value: str | None) -> float | None:
        if not value or value in ("0/0",):
            return None
        try:
            if "/" in value:
                num, den = value.split("/", 1)
                rate = float(num) / float(den)
            else:
                rate = float(value)
        except (ValueError, ZeroDivisionError):
            return None
        return rate if rate > 0 and rate < 1000 else None

    info = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    r_frame = _rate(info.get("r_frame_rate"))
    avg_frame = _rate(info.get("avg_frame_rate"))
    vfr = (
        r_frame is not None and avg_frame is not None
        and abs(r_frame - avg_frame) / max(avg_frame, 1e-9) > 0.01
    )
    return r_frame, avg_frame, vfr


def resolve_output_fps(path: Path, mode: str = "source",
                       fallback: float = 30.0,
                       fps_min: float = 24.0,
                       fps_max: float = 60.0) -> tuple[float, str]:
    """Output fps policy: follow the probed section fps, clamped to
    [fps_min, fps_max]; VFR or probe failure falls back (default 30).
    Never the analysis rate. Returns (fps, reason)."""
    if mode != "source":
        try:
            return max(fps_min, min(fps_max, float(mode))), "forced"
        except (TypeError, ValueError):
            pass
    r_frame, avg_frame, vfr = probe_fps(path)
    rate = avg_frame or r_frame
    if vfr or rate is None:
        return fallback, "vfr_or_unknown_source"
    return max(fps_min, min(fps_max, rate)), "source_native"


def run_command(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise ExternalCommandError(f"External command failed: {detail[-1000:]}") from exc


def render_static_vertical(
    source: Path,
    output: Path,
    start: float,
    duration: float,
    width: int = 1080,
    height: int = 1920,
    subtitles: Path | None = None,
    preset: str | None = None,
    crf: int | None = None,
    fps: float | None = None,
) -> Path:
    """Static center-crop render. ``preset=None`` keeps the production
    default; ``GAECLIP_RENDER_PROFILE=dev`` selects the fast dev preset."""
    from .framing_config import resolve_encode_preset

    preset = resolve_encode_preset(preset)
    if crf is None:
        crf = VIDEO_CRF
    if fps is None:
        fps = 30.0
    output.parent.mkdir(parents=True, exist_ok=True)
    probed = probe_dimensions(source)
    if probed is not None:
        crop_x, crop_y, crop_w, crop_h, out_w, out_h = compute_vertical_ladder(
            probed[0], probed[1], width, height,
        )
    else:
        crop_x = crop_y = 0
        crop_w, crop_h, out_w, out_h = width, height, width, height
    vf = (
        f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
        f"scale={out_w}:{out_h}:flags=lanczos,setsar=1:1,fps={fps}"
    )
    if subtitles is not None:
        vf += f",{_subtitle_filter(subtitles)}"
    run_command([
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(source),
        "-t", f"{duration:.3f}", "-vf", vf, "-c:v", "libx264",
        "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        "-fflags", "+genpts", "-avoid_negative_ts", "make_zero",
        str(output),
    ])
    verify_clip(output, expected_duration=duration)
    return output


def _portrait_crop_filter(
    item: "CropInstruction",
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    pane_h: int | None = None,
) -> str:
    """Build an ffmpeg crop+scale filter string that produces a correct 9:16
    portrait crop from a source of arbitrary aspect ratio.

    ``CropInstruction`` stores fractions of the source frame (center_x,
    center_y in [0,1] relative to source dimensions; width/height are the
    *intended* portrait crop fractions but may be wrong when the source is
    landscape because analyze_section computes them as min(1, 9/16) = 0.5625
    regardless of source shape).

    Instead of blindly multiplying ``iw * width_fraction`` (which yields a
    square on a 1920×1080 source), this function re-derives the crop window
    in *pixels* by fixing the 9:16 aspect ratio constraint and using the
    face center (center_x / center_y) only for positioning.

    When ``pane_h`` is set (split-screen), we use a wide crop scaled to
    ``out_w × pane_h``, centering on the speaker's X position while spanning
    the full source height (each speaker gets one horizontal band).

    Steps for single-speaker portrait mode:
    1. Desired output aspect is out_w × target_h (portrait).
    2. Compute the largest out_w:target_h window that fits inside src_w × src_h.
    3. Center that window on the speaker (center_x, center_y are in [0,1]
       relative to source dimensions).
    4. Clamp so the window stays inside the source frame.
    5. Produce crop=<w>:<h>:<x>:<y>,scale=<out_w>:<target_h>:flags=lanczos,setsar=1:1.
    """
    target_h = pane_h if pane_h is not None else out_h

    if pane_h is not None:
        # Split-screen pane: aspect-correct source window (out_w:pane_h),
        # centered on the speaker horizontally. Crop-first + uniform scale
        # later, so panes never stretch (Phase 1 fix: was a full-frame
        # squash into the pane, i.e. non-uniform scale = distortion).
        from .framing_geometry import portrait_window_size

        crop_w, crop_h = portrait_window_size(src_w, src_h, out_w, pane_h)
        half_w = (crop_w / src_w) / 2
        half_w = min(half_w, 0.5)
        cx = min(1.0 - half_w, max(half_w, item.center_x))
        half_h = (crop_h / src_h) / 2
        half_h = min(half_h, 0.5)
        cy = min(1.0 - half_h, max(half_h, 0.5))
        crop_x = max(0, min(int(round(cx * src_w - crop_w / 2)), src_w - crop_w))
        crop_y = max(0, min(int(round(cy * src_h - crop_h / 2)), src_h - crop_h))
        return (
            f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
            f"scale={out_w}:{target_h}:flags=lanczos,setsar=1:1"
        )

    # --- 1. Compute pixel crop size that exactly matches out_w:target_h ---
    # We want: crop_w / crop_h == out_w / target_h
    # Largest such window that fits inside src_w × src_h:
    crop_w_from_h = src_h * out_w / target_h   # if we use full source height
    crop_h_from_w = src_w * target_h / out_w   # if we use full source width

    if crop_w_from_h <= src_w:
        crop_w = int(round(crop_w_from_h))
        crop_h = src_h
    else:
        crop_w = src_w
        crop_h = int(round(crop_h_from_w))

    # Force even dimensions (required by libx264 yuv420p).
    crop_w = max(2, crop_w // 2 * 2)
    crop_h = max(2, crop_h // 2 * 2)

    # --- 2. Position: use center_x/center_y (0-1 relative to source) ---
    # Convert to pixel coordinates of the center point.
    cx_px = item.center_x * src_w
    cy_px = item.center_y * src_h

    # Top-left corner of the crop window.
    crop_x = int(round(cx_px - crop_w / 2))
    crop_y = int(round(cy_px - crop_h / 2))

    # --- 3. Clamp to stay inside source frame ---
    crop_x = max(0, min(crop_x, src_w - crop_w))
    crop_y = max(0, min(crop_y, src_h - crop_h))

    return (
        f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
        f"scale={out_w}:{target_h}:flags=lanczos,setsar=1:1"
    )


def render_dynamic_vertical(
    source: Path,
    output: Path,
    instructions: list[CropInstruction],
    width: int = 1080,
    height: int = 1920,
    subtitles: Path | None = None,
    preset: str | None = None,
    crf: int | None = None,
    fps: float | None = None,
) -> Path:
    """Dynamic trim/concat render. ``preset=None`` keeps the production
    default; ``GAECLIP_RENDER_PROFILE=dev`` selects the fast dev preset."""
    from .framing_config import resolve_encode_preset

    preset = resolve_encode_preset(preset)
    if crf is None:
        crf = VIDEO_CRF
    if fps is None:
        fps = 30.0
    if not instructions:
        raise ValueError("at least one crop instruction is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    probed = probe_dimensions(source)
    src_w: int
    src_h: int
    if probed is not None:
        src_w, src_h = probed
        _, _, _, _, width, height = compute_vertical_ladder(src_w, src_h, width, height)
    else:
        src_w, src_h = width, height
    filters = []
    outputs = []
    index = 0
    while index < len(instructions):
        item = instructions[index]
        pair = (
            index + 1 < len(instructions)
            and item.layout == "split"
            and instructions[index + 1].layout == "split"
            and abs(item.start - instructions[index + 1].start) < 0.001
            and abs(item.end - instructions[index + 1].end) < 0.001
        )
        if pair:
            panes = []
            for pane_index, pane in enumerate(instructions[index:index + 2]):
                crop = _portrait_crop_filter(pane, src_w, src_h, width, height, pane_h=height // 2)
                label = f"v{index}_{pane_index}"
                filters.append(
                    f"[0:v]trim=start={pane.start:.3f}:end={pane.end:.3f},"
                    f"setpts=PTS-STARTPTS,{crop}[{label}]"
                )
                panes.append(f"[{label}]")
            label = f"v{index}"
            filters.append(f"{''.join(panes)}vstack=inputs=2[{label}]")
            outputs.append(f"[{label}]")
            index += 2
            continue
        crop = _portrait_crop_filter(item, src_w, src_h, width, height)
        label = f"v{index}"
        filters.append(
            f"[0:v]trim=start={item.start:.3f}:end={item.end:.3f},"
            f"setpts=PTS-STARTPTS,{crop}[{label}]"
        )
        outputs.append(f"[{label}]")
        index += 1
    final_input = "".join(outputs)
    filters.append(f"{final_input}concat=n={len(outputs)}:v=1:a=0[vconcatraw]")
    filters.append(f"[vconcatraw]fps={fps}[vconcat]")
    if subtitles is not None:
        filters.append(
            f"[vconcat]{_subtitle_filter(subtitles)}[vout]"
        )
    else:
        filters.append("[vconcat]null[vout]")
    audio_start = min(item.start for item in instructions)
    audio_end = max(item.end for item in instructions)
    audio_span = max(0.1, audio_end - audio_start)
    filters.append(
        f"[0:a]atrim=start={audio_start:.3f}:end={audio_end:.3f},"
        f"asetpts=PTS-STARTPTS[aout]"
    )
    run_command([
        "ffmpeg", "-y", "-i", str(source), "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart", "-shortest",
        str(output),
    ])
    verify_clip(output, expected_duration=audio_span)
    return output


def sample_keyframes(keyframes: list[tuple[float, float, float, float, float, int]],
                     fps: float, duration: float,
                     src_w: int, src_h: int) -> list[tuple[float, int, int, int, int]]:
    """Expand sparse normalized keyframes to per-frame source-pixel ints.

    Linear interpolation within the same scene; zero-dt duplicates (scene
    hard cuts) take the right-hand value. Every output clamped in-frame
    and even-rounded. Returns [(t, x, y, w, h)] sorted by t.
    """
    def _even(value: float) -> int:
        return max(2, int(round(value)) // 2 * 2)

    if not keyframes:
        raise ValueError("at least one keyframe is required")
    ordered = sorted(keyframes, key=lambda kf: kf[0])
    n_frames = max(1, int(round(duration * fps)))
    out: list[tuple[float, int, int, int, int]] = []
    seg = 0
    for index in range(n_frames):
        timestamp = index / fps
        while (seg + 1 < len(ordered)
               and ordered[seg + 1][0] <= timestamp + 1e-9):
            seg += 1
        if seg + 1 < len(ordered):
            nxt = ordered[seg + 1]
            cur = ordered[seg]
            dt = nxt[0] - cur[0]
            if dt > 1e-9 and nxt[5] == cur[5]:
                blend = min(1.0, max(0.0, (timestamp - cur[0]) / dt))
                cx = cur[1] + (nxt[1] - cur[1]) * blend
                cy = cur[2] + (nxt[2] - cur[2]) * blend
                w = cur[3] + (nxt[3] - cur[3]) * blend
                h = cur[4] + (nxt[4] - cur[4]) * blend
            else:
                # Past the end, before the start, or across a hard cut:
                # hold (duplicates already encode the cut side).
                cx, cy, w, h = cur[1], cur[2], cur[3], cur[4]
        else:
            cur = ordered[seg]
            cx, cy, w, h = cur[1], cur[2], cur[3], cur[4]
        px_w = _even(w * src_w)
        px_h = _even(h * src_h)
        if px_w > src_w or px_h > src_h:
            # Defensive proportional shrink (keyframes are pre-clamped
            # upstream; aspect must never break here).
            scale = min(src_w / max(px_w, 1), src_h / max(px_h, 1))
            px_w, px_h = _even(px_w * scale), _even(px_h * scale)
        px_x = _even(min(max(cx * src_w - px_w / 2, 0), max(0, src_w - px_w)))
        px_y = _even(min(max(cy * src_h - px_h / 2, 0), max(0, src_h - px_h)))
        out.append((round(timestamp, 3), px_x, px_y, px_w, px_h))
    return out


def write_sendcmd_file(keyframes: list[tuple[float, int, int, int, int]],
                       path: Path) -> Path:
    """Write per-frame crop commands: (t, x, y, w, h), pixel ints.

    Nested-if expressions were rejected empirically (filter reinit failure
    past ~80 depth); a sendcmd file has no nesting and no cmdline limits.
    All four crop axes verified to apply per-frame via file input.
    """
    lines = []
    for timestamp, x, y, w, h in keyframes:
        stamp = f"{timestamp:.3f}"
        lines.append(f"{stamp} crop x {int(x)};")
        lines.append(f"{stamp} crop y {int(y)};")
        lines.append(f"{stamp} crop w {int(w)};")
        lines.append(f"{stamp} crop h {int(h)};")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def render_trajectory_vertical(
    source: Path,
    output: Path,
    keyframes: list[tuple[float, float, float, float, float, int]],
    width: int = 1080,
    height: int = 1920,
    fps: float = 30.0,
    duration: float | None = None,
    subtitles: Path | None = None,
    preset: str | None = None,
    crf: int | None = None,
    cmdfile: Path | None = None,
) -> Path:
    """Single-pass smooth crop render. ``keyframes`` are normalized
    (t, cx, cy, w, h, scene); they are sampled at every OUTPUT frame
    (timestamp-based, so 30/60fps sources keep native cadence), clamped,
    even-rounded, and driven per-frame via a sendcmd file. Order
    preserved: crop -> scale -> setsar -> fps -> subtitle LAST. Audio
    mapping identical to the legacy dynamic path.
    """
    from .framing_config import resolve_encode_preset

    if not keyframes:
        raise ValueError("at least one crop keyframe is required")
    if preset is None:
        preset = resolve_encode_preset(None)
    if crf is None:
        crf = VIDEO_CRF
    output.parent.mkdir(parents=True, exist_ok=True)
    probed = probe_dimensions(source)
    src_w: int
    src_h: int
    if probed is not None:
        src_w, src_h = probed
        _, _, _, _, width, height = compute_vertical_ladder(src_w, src_h, width, height)
    else:
        src_w, src_h = width, height
    span_end = duration if duration is not None else keyframes[-1][0]
    span_start = keyframes[0][0]
    span = max(0.1, span_end - span_start)
    sampled = sample_keyframes(keyframes, fps, span, src_w, src_h)
    # Rebase command timestamps to the section (keyframes are section-relative).
    t0, x0, y0, w0, h0 = sampled[0]
    cmd_path = cmdfile or output.with_name(f"{output.stem}_crop.txt")
    write_sendcmd_file(sampled, cmd_path)
    vf = (
        f"sendcmd=f='{str(cmd_path)}',"
        f"crop={w0}:{h0}:{x0}:{y0},"
        f"scale={width}:{height}:flags=lanczos,setsar=1,fps={fps}"
    )
    if subtitles is not None:
        vf += f",{_subtitle_filter(subtitles)}"
    audio_span = max(0.1, span)
    run_command([
        "ffmpeg", "-y", "-i", str(source), "-vf", vf,
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        "-fflags", "+genpts", "-avoid_negative_ts", "make_zero",
        "-shortest",
        str(output),
    ])
    verify_clip(output, expected_duration=audio_span)
    return output


def render_debug_overlay(
    source: Path,
    output: Path,
    faces: list[tuple[float, float, float, float, bool]],
    crops: list[tuple[float, float, float, float]],
    width: int = 1080,
    height: int = 1920,
) -> Path:
    """Render a debug copy with face boxes (green=selected, red=other) and
    the crop window (yellow). Coordinates are relative 0-1. Text labels use
    DejaVu when present, otherwise boxes only."""
    import os

    output.parent.mkdir(parents=True, exist_ok=True)
    filters = [f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1:1"]
    for x1, y1, x2, y2, selected in faces:
        color = "lime" if selected else "red"
        filters.append(
            f"drawbox=x='iw*{x1:.4f}':y='ih*{y1:.4f}':w='iw*{max(0.01, x2 - x1):.4f}':"
            f"h='ih*{max(0.01, y2 - y1):.4f}':color={color}:t=4"
        )
    for x, y, w, h in crops:
        filters.append(
            f"drawbox=x='iw*{x:.4f}':y='ih*{y:.4f}':w='iw*{w:.4f}':h='ih*{h:.4f}':"
            "color=yellow:t=3"
        )
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if faces and os.path.exists(font):
        x1, y1, _, _ = faces[0]
        filters.append(
            f"drawtext=fontfile={font}:text='ACTIVE SPEAKER DEBUG':"
            f"x='iw*{x1:.4f}':y='ih*{max(0, y1 - 0.05):.4f}':fontsize=28:fontcolor=white:"
            "box=1:boxcolor=black@0.6"
        )
    run_command([
        "ffmpeg", "-y", "-i", str(source), "-vf", ",".join(filters),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        str(output),
    ])
    return output


def probe(path: Path) -> dict[str, str]:
    result = run_command([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height",
        "-of", "default=noprint_wrappers=1", str(path),
    ])
    return dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )


def verify_clip(
    path: Path,
    expected_duration: float | None = None,
    tolerance: float = 3.0,
    min_bytes: int = 100_000,
) -> dict[str, str]:
    """Ensure a rendered clip is playable: exists, has video+audio, sane duration."""
    if not path.is_file():
        raise ExternalCommandError(f"Rendered clip is missing: {path}")
    size = path.stat().st_size
    if size < min_bytes:
        raise ExternalCommandError(
            f"Rendered clip is too small ({size} bytes): {path}"
        )
    result = run_command([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name",
        "-of", "default=noprint_wrappers=1", str(path),
    ])
    streams = [
        line.split("=", 1)[1].strip()
        for line in result.stdout.splitlines()
        if line.startswith("codec_type=")
    ]
    if "video" not in streams:
        raise ExternalCommandError(f"Rendered clip has no video stream: {path}")
    if "audio" not in streams:
        raise ExternalCommandError(f"Rendered clip has no audio stream: {path}")
    info = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    if expected_duration is not None:
        try:
            actual = float(info.get("duration", "nan"))
        except ValueError:
            actual = float("nan")
        if actual != actual:  # NaN check
            raise ExternalCommandError(f"Rendered clip has unknown duration: {path}")
        if abs(actual - expected_duration) > tolerance:
            raise ExternalCommandError(
                f"Rendered clip duration {actual:.1f}s differs from "
                f"expected {expected_duration:.1f}s: {path}"
            )
    return info
