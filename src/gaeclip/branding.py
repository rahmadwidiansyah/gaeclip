"""Clip branding: channel identity overlays rendered into the final clip.

Four templates (all rendered with FFmpeg in the SAME encode pass as the
framing/subtitle render — never a second post-process pass):

- ``glass_end``      — blurred-bg end card, last N seconds (default).
- ``minimal_corner`` — small channel tag in a corner, full/first/last window.
- ``creator_tag``    — glass pill (avatar + name) near the top.
- ``creator_outro``  — big blurred-bg outro, last N seconds, optional text.

Compositing order (per spec §5, honoring the pinned pipeline invariant
``crop -> scale -> setsar -> fps -> subtitles LAST``):

1. base video chain (crop/framing/scale/fps, built by ``media.py``),
2. end-card blurred background (trim of the SAME segment — never black),
3. avatar overlay (pre-circled PNG via ``movie=`` source),
4. channel-name / additional text (``drawtext``),
5. subtitles filter (unchanged, still last — sync untouched),
6. audio is never touched (no concat/cut), so A/V sync is preserved.

All filter strings are built as list-args (no ``shell=True`` anywhere);
user text is escaped for the ``drawtext``/``overlay`` grammar.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TEMPLATES = ("glass_end", "minimal_corner", "creator_tag", "creator_outro")
DISPLAY_MODES = ("avatar_name", "avatar_only", "name_only")
POSITIONS = ("top_left", "top_right", "bottom_left", "bottom_right",
             "top_center", "center")
TIMINGS = ("full", "first", "last")
ANIMATIONS = ("none", "fade", "fade_scale")

ENDCARD_TEMPLATES = ("glass_end", "creator_outro")

MAX_CHANNEL_NAME = 60
MAX_ADDITIONAL_TEXT = 40

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "template": "glass_end",
    "channel_name": None,
    "avatar_asset": None,
    "display_mode": "avatar_name",
    "position": "top_left",
    "timing": "last",
    "duration": 2.0,
    "blur": 20.0,
    "dim": 0.35,
    "animation": "fade",
    "additional_text": None,
}


def _escape_drawtext(text: str) -> str:
    """Escape user text for the drawtext filter grammar."""
    out = str(text or "")
    out = out.replace("\\", r"\\")
    out = out.replace(":", r"\:")
    out = out.replace(",", r"\,")
    out = out.replace("'", r"\'")
    out = out.replace("[", r"\[").replace("]", r"\]")
    return out


def _escape_path(path: str) -> str:
    """Escape a file path for the movie= filter grammar."""
    return str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _clean_text(text: Any, limit: int) -> str | None:
    if text is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(text)).strip()
    if not cleaned:
        return None
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip()
    return cleaned or None


def validate_brand_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate + normalize a branding payload. Raises ValueError.

    Unknown/absent keys fall back to DEFAULTS (old job payloads without any
    branding keys therefore stay valid with branding disabled).
    """
    data = dict(raw or {})
    cfg: dict[str, Any] = dict(DEFAULTS)
    if "enabled" in data:
        cfg["enabled"] = bool(data["enabled"])
    if data.get("template"):
        template = str(data["template"] or "")
        if template not in TEMPLATES:
            raise ValueError(f"unknown brand template: {template!r}")
        cfg["template"] = template
    if data.get("channel_name") is not None:
        cfg["channel_name"] = _clean_text(data["channel_name"], MAX_CHANNEL_NAME)
    if data.get("avatar_asset") is not None:
        asset = data["avatar_asset"]
        cfg["avatar_asset"] = str(asset).strip() if asset else None
    if data.get("display_mode"):
        mode = str(data["display_mode"] or "")
        if mode not in DISPLAY_MODES:
            raise ValueError(f"unknown brand display_mode: {mode!r}")
        cfg["display_mode"] = mode
    if data.get("position"):
        position = str(data["position"] or "")
        if position not in POSITIONS:
            raise ValueError(f"unknown brand position: {position!r}")
        cfg["position"] = position
    if data.get("timing"):
        timing = str(data["timing"] or "")
        if timing not in TIMINGS:
            raise ValueError(f"unknown brand timing: {timing!r}")
        cfg["timing"] = timing
    if data.get("duration") is not None:
        try:
            duration = float(data["duration"])
        except (TypeError, ValueError):
            raise ValueError(f"invalid brand duration: {data['duration']!r}")
        if not math.isfinite(duration):
            raise ValueError(f"invalid brand duration: {data['duration']!r}")
        cfg["duration"] = min(10.0, max(0.5, duration))
    if data.get("blur") is not None:
        try:
            blur = float(data["blur"])
        except (TypeError, ValueError):
            raise ValueError(f"invalid brand blur: {data['blur']!r}")
        if not math.isfinite(blur):
            raise ValueError(f"invalid brand blur: {data['blur']!r}")
        cfg["blur"] = min(50.0, max(0.0, blur))
    if data.get("dim") is not None:
        try:
            dim = float(data["dim"])
        except (TypeError, ValueError):
            raise ValueError(f"invalid brand dim: {data['dim']!r}")
        if not math.isfinite(dim):
            raise ValueError(f"invalid brand dim: {data['dim']!r}")
        cfg["dim"] = min(0.8, max(0.0, dim))
    if data.get("animation"):
        animation = str(data["animation"] or "")
        if animation not in ANIMATIONS:
            raise ValueError(f"unknown brand animation: {animation!r}")
        cfg["animation"] = animation
    if data.get("additional_text") is not None:
        cfg["additional_text"] = _clean_text(data["additional_text"], MAX_ADDITIONAL_TEXT)
    return cfg


def _fontfile_option(path: str) -> str:
    """Build an ffmpeg drawtext ``fontfile`` option for a real path.

    Windows paths need two layers of escaping inside a filter graph:
    backslashes become forward slashes, and the drive colon is escaped so
    ffmpeg does not read it as an option separator. Single quotes protect
    the value from the filtergraph parser.
    """
    normalized = path.replace("\\", "/").replace(":", r"\:")
    return f"fontfile='{normalized}'"


def resolve_brand_font() -> str:
    """drawtext font selector: ``fontfile=PATH`` when a local file exists,
    else a fontconfig ``font='DejaVu Sans'`` name.

    Windows installs have no DejaVu Sans, and the ffmpeg build shipped to
    users may not have fontconfig, so an explicit Segoe UI/Arial fontfile
    is required for branding to render on desktop.
    """
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    candidates = (
        os.path.join(windir, "Fonts", "segoeui.ttf"),
        os.path.join(windir, "Fonts", "arial.ttf"),
        # macOS (dev machines only; the desktop app targets Windows).
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for path in candidates:
        if os.path.exists(path):
            return _fontfile_option(path)
    return "font='DejaVu Sans'"


@dataclass(frozen=True)
class BrandPlan:
    """Fully resolved, render-ready branding for one clip."""

    template: str
    window: tuple[float, float]  # (start, end) seconds in clip timeline
    bg_blur: bool
    blur_sigma: int
    dim: float
    avatar_path: Path | None  # prepared circle PNG (None = name-only/fallback)
    show_avatar: bool
    show_name: bool
    channel_name: str
    avatar_size: int
    avatar_xy: tuple[int, int] | None
    font_size: int
    text_x: str  # int or drawtext expression (center/right alignment)
    text_y: int
    box: tuple[int, int, int, int] | None  # tag pill (x, y, w, h)
    additional_text: str | None
    additional_fs: int
    additional_xy: tuple[str, int] | None
    animation: str
    width: int
    height: int
    fps: float


def _even(value: float) -> int:
    return max(2, int(round(value)) // 2 * 2)


def resolve_brand_plan(
    cfg: dict[str, Any],
    clip_duration: float,
    width: int,
    height: int,
    avatar_path: Path | None,
    burn_subtitles: bool = True,
    fps: float = 30.0,
) -> BrandPlan | None:
    """Resolve a validated config into absolute geometry. None = render plain.

    End-card windows never extend the video: ``D = min(duration, T)`` so
    short clips still show the card (whole clip when shorter than D).
    """
    if not cfg.get("enabled"):
        return None
    try:
        duration_total = max(0.1, float(clip_duration))
    except (TypeError, ValueError):
        return None
    template = str(cfg.get("template") or DEFAULTS["template"])
    display = str(cfg.get("display_mode") or DEFAULTS["display_mode"])
    position = str(cfg.get("position") or DEFAULTS["position"])
    timing = str(cfg.get("timing") or DEFAULTS["timing"])
    window_s = float(cfg.get("duration", DEFAULTS["duration"]) or 2.0)
    animation = str(cfg.get("animation") or DEFAULTS["animation"])
    blur = int(round(float(cfg.get("blur", DEFAULTS["blur"]) or 0.0)))
    dim = float(cfg.get("dim", DEFAULTS["dim"]) or 0.0)

    channel_name = _clean_text(cfg.get("channel_name"), MAX_CHANNEL_NAME) or ""
    avatar_ok = avatar_path is not None and Path(avatar_path).is_file()
    show_avatar = display in ("avatar_name", "avatar_only") and avatar_ok
    show_name = display in ("avatar_name", "name_only") and bool(channel_name)
    if display == "avatar_only" and not avatar_ok:
        logger.warning("brand avatar_only requested but no avatar available; skipping branding")
        return None
    if not show_avatar and not show_name:
        return None

    if template in ENDCARD_TEMPLATES:
        length = min(window_s, duration_total)
        start, end = duration_total - length, duration_total
        bg_blur = True
    else:
        if timing == "full":
            start, end = 0.0, duration_total
        elif timing == "first":
            start, end = 0.0, min(window_s, duration_total)
        else:
            start, end = max(0.0, duration_total - window_s), duration_total
        bg_blur = False
    if end - start < 0.2:
        return None

    width = max(2, int(width))
    height = max(2, int(height))
    margin = _even(width * 0.06)
    pad = _even(width * 0.02)

    if template == "creator_outro":
        avatar_size, font_size = _even(height * 0.24), _even(height * 0.055)
        add_fs = _even(height * 0.032)
    elif template == "glass_end":
        avatar_size, font_size = _even(height * 0.17), _even(height * 0.048)
        add_fs = _even(height * 0.030)
    elif template == "creator_tag":
        avatar_size, font_size = _even(height * 0.065), _even(height * 0.030)
        add_fs = 0
    else:  # minimal_corner
        avatar_size, font_size = _even(height * 0.075), _even(height * 0.030)
        add_fs = 0
    avatar_size = max(16, avatar_size)
    font_size = max(12, font_size)

    avatar_xy: tuple[int, int] | None = None
    text_x: str = "0"
    text_y = 0
    box: tuple[int, int, int, int] | None = None
    additional = _clean_text(cfg.get("additional_text"), MAX_ADDITIONAL_TEXT)
    additional_xy: tuple[str, int] | None = None
    use_additional = bool(additional) and template == "creator_outro" and show_name

    if template in ENDCARD_TEMPLATES:
        center_y = int(height * (0.36 if template == "glass_end" else 0.34))
        if show_avatar:
            avatar_xy = ((width - avatar_size) // 2, center_y - avatar_size // 2)
            text_y = avatar_xy[1] + avatar_size + pad
        else:
            text_y = center_y
        text_x = "(w-text_w)/2"
        if use_additional and add_fs:
            additional_xy = ("(w-text_w)/2", text_y + font_size + pad)
    elif template == "creator_tag":
        est_w = (avatar_size if show_avatar else 0) + pad * 3 + int(font_size * 0.62 * max(1, len(channel_name)))
        pill_w = min(width - 2 * margin, max(avatar_size + pad * 2, est_w))
        pill_h = avatar_size + pad * 2
        pos = position if position in ("top_left", "top_right", "top_center") else "top_center"
        box_x = (width - pill_w) // 2 if pos == "top_center" else (margin if pos == "top_left" else width - pill_w - margin)
        box_y = margin
        box = (box_x, box_y, pill_w, pill_h)
        if show_avatar:
            avatar_xy = (box_x + pad, box_y + pad)
            text_x = str(avatar_xy[0] + avatar_size + pad)
        else:
            text_x = str(box_x + pad)
        text_y = box_y + (pill_h - font_size) // 2
    else:  # minimal_corner
        bottom_margin = _even(height * 0.20) if burn_subtitles else _even(height * 0.08)
        spots = {
            "top_left": (margin, margin, False),
            "top_right": (width - margin, margin, True),
            "bottom_left": (margin, height - bottom_margin, False),
            "bottom_right": (width - margin, height - bottom_margin, True),
            "top_center": (width // 2, margin, False),
            "center": (margin, margin, False),
        }
        anchor_x, anchor_y, right = spots.get(position, spots["top_left"])
        if right:
            avatar_x = anchor_x - (avatar_size if show_avatar else 0)
            avatar_y = anchor_y if "top" in position else anchor_y - (avatar_size if show_avatar else 0)
            if show_avatar:
                avatar_xy = (avatar_x, avatar_y)
                text_x = f"{avatar_x - pad}-text_w"
                text_y = avatar_y + (avatar_size - font_size) // 2
            else:
                text_x = f"{anchor_x - pad}-text_w"
                text_y = anchor_y
        else:
            avatar_x = anchor_x
            avatar_y = anchor_y if "top" in position or position == "center" else anchor_y - (avatar_size if show_avatar else 0)
            if show_avatar:
                avatar_xy = (avatar_x, avatar_y)
                text_x = str(avatar_x + avatar_size + pad)
                text_y = avatar_y + (avatar_size - font_size) // 2
            else:
                text_x = str(anchor_x)
                text_y = anchor_y

    return BrandPlan(
        template=template, window=(round(start, 3), round(end, 3)),
        bg_blur=bg_blur, blur_sigma=blur, dim=dim,
        avatar_path=Path(avatar_path) if avatar_ok else None,
        show_avatar=show_avatar, show_name=show_name,
        channel_name=channel_name, avatar_size=avatar_size,
        avatar_xy=avatar_xy, font_size=font_size, text_x=text_x,
        text_y=text_y, box=box,
        additional_text=additional if use_additional else None,
        additional_fs=add_fs, additional_xy=additional_xy,
        animation=animation, width=width, height=height, fps=fps,
    )


def window_enable(window: tuple[float, float], total: float) -> str | None:
    """ffmpeg enable expression, or None when the window covers everything."""
    start, end = window
    if start <= 0.01 and end >= total - 0.01:
        return None
    return f"between(t,{start:.3f},{end:.3f})"


def prepare_avatar(src: Path | None, size: int, out_path: Path) -> Path | None:
    """Render a circle-cropped RGBA PNG of ``size`` px. None on any failure
    (caller falls back to name-only — a missing avatar never fails the job)."""
    if src is None:
        return None
    try:
        src = Path(src)
        if not src.is_file() or src.stat().st_size == 0:
            return None
        size = max(16, int(size))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        half = size // 2
        from .media import run_command

        run_command([
            "ffmpeg", "-y", "-i", str(src),
            "-vf",
            f"scale={size}:{size}:force_original_aspect_ratio=increase,"
            f"crop={size}:{size},format=rgba,"
            "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            f"a='if(lt(hypot(X-{half},Y-{half}),{half}),255,0)'",
            "-frames:v", "1",
            str(out_path),
        ])
        if out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
    except Exception as exc:
        logger.warning("brand avatar prep failed (%s); falling back to name-only", exc)
    return None


def _fade_chain(win_start: float, win_end: float, animation: str,
                avatar_size: int, with_outro_fade: bool) -> str:
    """fade (+optional scale-in) chain fragment for the avatar movie input."""
    parts = []
    if animation in ("fade", "fade_scale"):
        parts.append(f"fade=t=in:st={win_start:.3f}:d=0.3:alpha=1")
        if with_outro_fade and win_end - win_start > 0.6:
            parts.append(f"fade=t=out:st={win_end - 0.3:.3f}:d=0.3:alpha=1")
    if animation == "fade_scale":
        expr = f"{avatar_size}*(0.92+0.08*min((t-{win_start:.3f})/0.3\\,1))"
        parts.append(f"scale=w='{expr}':h='-2':eval=frame")
    return "," + ",".join(parts) if parts else ""


def _text_alpha(win_start: float, win_end: float, animation: str,
                with_outro_fade: bool) -> str:
    """drawtext alpha expression for matching fade behavior."""
    if animation not in ("fade", "fade_scale"):
        return ":alpha='1'"
    expr = f"min(1\\,max(0\\,(t-{win_start:.3f})/0.3))"
    if with_outro_fade and win_end - win_start > 0.6:
        expr = (f"min({expr}\\,min(1\\,max(0\\,({win_end:.3f}-t)/0.3)))")
    return f":alpha='{expr}'"


def _drawtext_opts(plan: BrandPlan, text: str, x: str, y: int,
                   fontsize: int, box: bool, win: str | None,
                   font_sel: str, outro_fade: bool) -> str:
    color = "white"
    opts = (
        f"{font_sel}:text='{_escape_drawtext(text)}':"
        f"x={x}:y={y}:fontsize={fontsize}:fontcolor={color}:"
        f"shadowcolor=black@0.6:shadowx=2:shadowy=2"
        f"{_text_alpha(plan.window[0], plan.window[1], plan.animation, outro_fade)}"
    )
    if box:
        opts += ":box=1:boxcolor=black@0.4:boxborderw=8"
    if win is not None:
        opts += f":enable='{win}'"
    return f"drawtext={opts}"


def plan_needs_avatar_input(plan: BrandPlan) -> bool:
    """True when the plan overlays an avatar (caller must add input #1)."""
    return bool(plan.show_avatar and plan.avatar_path is not None
                and plan.avatar_xy is not None)


def avatar_input_chain(plan: BrandPlan, win: str | None,
                         src: str = "[1:v]") -> tuple[str, str]:
    """Build the avatar input chain from a real input stream.

    The caller adds ``-loop 1 -framerate {fps} -i avatar.png`` as input #1;
    a looped image input carries advancing timestamps (unlike a single
    ``movie=`` frame), so fade/scale animations evaluate correctly, and it
    never hangs the graph the way ``movie=...:loop=0`` does. Returns
    (chain, overlay-filter-without-output-label).
    """
    assert plan.avatar_path is not None and plan.avatar_xy is not None
    start, end = plan.window
    outro_fade = plan.template in ENDCARD_TEMPLATES
    chain = (
        f"{src}scale={plan.avatar_size}:{plan.avatar_size},format=rgba,fps={plan.fps:g}"
        f"{_fade_chain(start, end, plan.animation, plan.avatar_size, outro_fade)}[av]"
    )
    ax, ay = plan.avatar_xy
    overlay = f"overlay={ax}:{ay}"
    if win is not None:
        overlay += f":enable='{win}'"
    return chain, overlay


def avatar_movie_chain(plan: BrandPlan, win: str | None, font_unused: str = "") -> tuple[str, str]:
    """Deprecated shim: prefer :func:`avatar_input_chain` (real input #1).

    Kept for unit tests that assert chain shape; NOT used by renders
    because ``movie=...:loop=0`` hangs the graph on this stack.
    """
    chain, overlay = avatar_input_chain(plan, win, src="[1:v]")
    return chain, overlay


def build_brand_chains(
    base: str,
    plan: BrandPlan,
    tail: str,
    font_sel: str,
    total: float,
    avatar_src: str | None = None,
    in_label: str = "[in]",
) -> str:
    """Splice branding into a linear ``-vf`` graph.

    ``base`` is the existing unlabeled chain (crop/scale/fps...),
    ``tail`` is ``""`` or ``",subtitles=..."``. ``avatar_src`` is the
    avatar input label (e.g. ``"[1:v]"``) when the caller added a looped
    avatar input; None = name-only (no avatar chain). Every stage is
    labelled, so the result always ends in ``[vout]``. Returns the complete
    filter string (use with ``-filter_complex`` + explicit ``-map``).
    """
    start, end = plan.window
    win = window_enable(plan.window, total)
    outro_fade = plan.template in ENDCARD_TEMPLATES
    if plan_needs_avatar_input(plan) and avatar_src is None:
        raise ValueError("brand avatar needs a real input stream (avatar_src)")
    parts: list[str] = []
    if plan.bg_blur:
        width, height = plan.width, plan.height
        bw = max(2, (int(width * 1.25) // 2) * 2)
        bh = max(2, (int(height * 1.25) // 2) * 2)
        parts.append(f"{in_label}{base},split=2[bmain][braw]")
        parts.append(
            f"[braw]trim=start={start:.3f}:end={end:.3f},"
            f"scale={bw}:{bh},crop={width}:{height},"
            f"gblur=sigma={plan.blur_sigma},"
            f"eq=brightness=-{plan.dim:.2f}[bend]"
        )
        overlay_win = f":enable='{win}'" if win is not None else ""
        parts.append(f"[bmain][bend]overlay=0:0{overlay_win}[b1]")
        cur = "[b1]"
    else:
        parts.append(f"{in_label}{base}[b0]")
        cur = "[b0]"
    if plan.box is not None:
        bx, by, bw, bh = plan.box
        box_f = f"drawbox=x={bx}:y={by}:w={bw}:h={bh}:color=black@0.45:t=fill"
        if win is not None:
            box_f += f":enable='{win}'"
        parts.append(f"{cur}{box_f}[bbox]")
        cur = "[bbox]"
    if (plan.show_avatar and plan.avatar_path is not None
            and plan.avatar_xy is not None and avatar_src is not None):
        chain, overlay = avatar_input_chain(plan, win, src=avatar_src)
        parts.append(chain)
        parts.append(f"{cur}[av]{overlay}[b2]")
        cur = "[b2]"
    if plan.show_name and plan.channel_name:
        dt = _drawtext_opts(plan, plan.channel_name, plan.text_x, plan.text_y,
                            plan.font_size, plan.template == "minimal_corner",
                            win, font_sel, outro_fade)
        parts.append(f"{cur}{dt}[b3]")
        cur = "[b3]"
    if plan.additional_text and plan.additional_xy is not None and plan.show_name:
        ax_expr, ay = plan.additional_xy
        dt2 = _drawtext_opts(plan, plan.additional_text, ax_expr, ay,
                             plan.additional_fs, False, win, font_sel, outro_fade)
        parts.append(f"{cur}{dt2}[b4]")
        cur = "[b4]"
    tail_nc = tail[1:] if tail.startswith(",") else tail
    if tail_nc:
        parts.append(f"{cur}{tail_nc}[vout]")
    else:
        parts.append(f"{cur}null[vout]")
    return ";".join(parts)


def wrap_vf(base: str, tail: str, plan: BrandPlan, font_sel: str,
            total: float, avatar_src: str | None = None) -> str:
    """Compose ``base`` + branding + ``tail`` into one filter string.

    Name-only path (no avatar input needed). Raises ValueError when the
    plan needs an avatar stream but ``avatar_src`` is None — callers must
    use ``-filter_complex`` with a looped avatar input instead (see
    :func:`avatar_input_chain`).
    """
    return build_brand_chains(base, plan, tail, font_sel, total,
                              avatar_src=avatar_src)


def extend_complex(filters: list[str], head: str, tail: str | None,
                   plan: BrandPlan, font_sel: str, total: float,
                   avatar_src: str = "[1:v]") -> str:
    """Append branding chains to a ``-filter_complex`` list. Returns ``vout``.

    ``head`` is the ready video label (e.g. ``vconcat``), ``tail`` the final
    filter without leading comma (e.g. ``subtitles=...``) or None.
    ``avatar_src`` is the looped avatar input label; the caller must have
    added ``-loop 1 -framerate {fps} -i avatar`` as input #1.
    """
    start, end = plan.window
    win = window_enable(plan.window, total)
    outro_fade = plan.template in ENDCARD_TEMPLATES
    cur = f"[{head}]"
    if plan.bg_blur:
        width, height = plan.width, plan.height
        bw = max(2, (int(width * 1.25) // 2) * 2)
        bh = max(2, (int(height * 1.25) // 2) * 2)
        filters.append(f"[{head}]split=2[bmain][braw]")
        filters.append(
            f"[braw]trim=start={start:.3f}:end={end:.3f},"
            f"scale={bw}:{bh},crop={width}:{height},"
            f"gblur=sigma={plan.blur_sigma},"
            f"eq=brightness=-{plan.dim:.2f}[bend]"
        )
        overlay_win = f":enable='{win}'" if win is not None else ""
        filters.append(f"[bmain][bend]overlay=0:0{overlay_win}[b1]")
        cur = "[b1]"
    if plan.box is not None:
        bx, by, bw, bh = plan.box
        box_f = f"drawbox=x={bx}:y={by}:w={bw}:h={bh}:color=black@0.45:t=fill"
        if win is not None:
            box_f += f":enable='{win}'"
        filters.append(f"{cur}{box_f}[bbox]")
        cur = "[bbox]"
    if plan_needs_avatar_input(plan) and avatar_src is not None:
        chain, overlay = avatar_input_chain(plan, win, src=avatar_src)
        filters.append(chain)
        filters.append(f"{cur}[av]{overlay}[b2]")
        cur = "[b2]"
    if plan.show_name and plan.channel_name:
        dt = _drawtext_opts(plan, plan.channel_name, plan.text_x, plan.text_y,
                            plan.font_size, plan.template == "minimal_corner",
                            win, font_sel, outro_fade)
        filters.append(f"{cur}{dt}[b3]")
        cur = "[b3]"
    if plan.additional_text and plan.additional_xy is not None and plan.show_name:
        ax_expr, ay = plan.additional_xy
        dt2 = _drawtext_opts(plan, plan.additional_text, ax_expr, ay,
                             plan.additional_fs, False, win, font_sel, outro_fade)
        filters.append(f"{cur}{dt2}[b4]")
        cur = "[b4]"
    if tail:
        filters.append(f"{cur}{tail}[vout]")
    else:
        filters.append(f"{cur}null[vout]")
    return "vout"
    return "vout"
