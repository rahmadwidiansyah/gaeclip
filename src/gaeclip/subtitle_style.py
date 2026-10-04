"""Burn subtitle style + position configuration.

Single source of truth for subtitle appearance, shared by the ASS writer
(subtitles.py) and the ffmpeg ``force_style`` builder (media.py) so preview,
file style, and burned output can never drift apart.

Styles (all responsive: size/margins derive from output resolution):
  bold_pop          uppercase, bold, yellow + thick black outline
  dynamic_highlight uppercase, bold, white base + per-word green highlight
  casual_center     transcript casing, bold white + black outline (legacy default)
  elegant_serif     transcript casing, serif, white, thin outline, soft shadow

Positions (margins derive from output resolution, never hardcoded pixels):
  center         middle-center (ASS Alignment 5)
  bottom_center  bottom-center with 12% safe margin (legacy default)
  bottom         bottom-center with 5% safe margin
"""

from __future__ import annotations

from dataclasses import dataclass

SUBTITLE_STYLES = ("bold_pop", "dynamic_highlight", "casual_center", "elegant_serif")
SUBTITLE_POSITIONS = ("center", "bottom_center", "bottom")

DEFAULT_SUBTITLE_STYLE = "casual_center"
DEFAULT_SUBTITLE_POSITION = "bottom_center"

# ASS BBGGRR colors.
WHITE = "&H00FFFFFF"
YELLOW = "&H0000FFFF"
GREEN = "&H0000FF00"
BLACK = "&H00000000"
SEMI_BLACK = "&H80000000"

# libass has no such font on disk; verified present via fc-match in worker.
FONT_SANS = "DejaVu Sans"
FONT_SERIF = "DejaVu Serif"

HIGHLIGHT_GREEN = GREEN
HIGHLIGHT_RESET = r"{\c}"


@dataclass(frozen=True)
class SubtitleRenderConfig:
    """Resolved, resolution-aware render parameters for one clip."""

    style: str
    position: str
    font_name: str
    font_size: int
    bold: bool
    primary: str
    outline_color: str
    outline: int
    shadow: int
    alignment: int
    margin_l: int
    margin_r: int
    margin_v: int
    uppercase: bool
    highlight: bool


def resolve_subtitle_config(
    style: str,
    position: str,
    width: int = 1080,
    height: int = 1920,
) -> SubtitleRenderConfig:
    """Validate names and resolve all render parameters for WxH output."""
    if style not in SUBTITLE_STYLES:
        raise ValueError(f"Unknown subtitle style: {style!r}")
    if position not in SUBTITLE_POSITIONS:
        raise ValueError(f"Unknown subtitle position: {position!r}")

    from .media import subtitle_font_size

    size = subtitle_font_size(width, height)
    margin_lr = round(width * 0.06)
    if position == "center":
        alignment, margin_v = 5, round(height * 0.12)
    elif position == "bottom":
        alignment, margin_v = 2, round(height * 0.05)
    else:  # bottom_center == legacy behavior
        alignment, margin_v = 2, round(height * 0.12)

    if style == "bold_pop":
        return SubtitleRenderConfig(
            style, position, FONT_SANS, size, True, YELLOW, BLACK, 3, 0,
            alignment, margin_lr, margin_lr, margin_v, True, False,
        )
    if style == "dynamic_highlight":
        return SubtitleRenderConfig(
            style, position, FONT_SANS, size, True, WHITE, BLACK, 2, 1,
            alignment, margin_lr, margin_lr, margin_v, True, True,
        )
    if style == "elegant_serif":
        return SubtitleRenderConfig(
            style, position, FONT_SERIF, size, False, WHITE, BLACK, 1, 1,
            alignment, margin_lr, margin_lr, margin_v, False, False,
        )
    return SubtitleRenderConfig(
        style, position, FONT_SANS, size, True, WHITE, BLACK, 2, 1,
        alignment, margin_lr, margin_lr, margin_v, False, False,
    )


def force_style_string(cfg: SubtitleRenderConfig) -> str:
    """ffmpeg ``force_style`` fragment matching the ASS file style row."""
    return (
        f"FontName={cfg.font_name},FontSize={cfg.font_size},"
        f"Bold={1 if cfg.bold else 0},"
        f"PrimaryColour={cfg.primary},"
        f"OutlineColour={cfg.outline_color},BorderStyle=1,"
        f"Outline={cfg.outline},Shadow={cfg.shadow},"
        f"Alignment={cfg.alignment},"
        f"MarginL={cfg.margin_l},MarginR={cfg.margin_r},MarginV={cfg.margin_v},"
        "WrapStyle=2"
    )


def ass_style_row(cfg: SubtitleRenderConfig) -> str:
    """ASS [V4+ Styles] row matching ``force_style_string``."""
    back = SEMI_BLACK if cfg.shadow else BLACK
    return (
        f"Style: Gaeclip,{cfg.font_name},{cfg.font_size},{cfg.primary},"
        f"&H000019FF,{cfg.outline_color},{back},"
        f"{-1 if cfg.bold else 0},0,0,0,100,100,0,0,1,"
        f"{cfg.outline},{cfg.shadow},{cfg.alignment},"
        f"{cfg.margin_l},{cfg.margin_r},{cfg.margin_v},1"
    )


def apply_case(cfg: SubtitleRenderConfig, text: str) -> str:
    return text.upper() if cfg.uppercase else text


def approximate_word_timing(
    start: float, end: float, words: list[str]
) -> list[tuple[str, float, float]]:
    """Split [start, end] across words proportionally to character length.

    APPROXIMATE word timing: the transcript pipeline carries no per-word
    timestamps (TranscriptLine.words is always empty), so karaoke highlight
    uses char-proportional estimates, never invented precision.
    """
    weights = [max(1, len(word)) for word in words]
    total = sum(weights)
    out: list[tuple[str, float, float]] = []
    cursor = start
    for index, word in enumerate(words):
        nxt = end if index == len(words) - 1 else start + (end - start) * sum(weights[: index + 1]) / total
        out.append((word, cursor, nxt))
        cursor = nxt
    return out


def _plain_lines(text: str, max_chars: int) -> list[list[str]]:
    """Greedy wrap on plain words; returns lines as word lists (stable)."""
    lines: list[list[str]] = []
    current: list[str] = []
    current_length = 0
    for word in text.split():
        next_length = current_length + (1 if current else 0) + len(word)
        if current and next_length > max_chars:
            lines.append(current)
            current, current_length = [word], len(word)
        else:
            current.append(word)
            current_length = next_length
    if current:
        lines.append(current)
    return lines


def karaoke_events(
    start: float, end: float, text: str, max_chars: int
) -> list[tuple[float, float, str]]:
    """One Dialogue event per word: active word green, rest white.

    Line breaks are computed once on plain words so layout never shifts;
    only the {\\c} tag moves. Each event spans [word.start, next.start]
    so the green highlight steps word by word as the cue plays.
    """
    words = text.split()
    if not words:
        return []
    timed = approximate_word_timing(start, end, words)
    line_words = _plain_lines(text, max_chars)
    word_line = [0] * len(words)
    cursor = 0
    for line_index, line in enumerate(line_words):
        for _ in line:
            word_line[cursor] = line_index
            cursor += 1
    events: list[tuple[float, float, str]] = []
    for index, (word, word_start, _) in enumerate(timed):
        event_end = timed[index + 1][1] if index + 1 < len(timed) else end
        rendered: list[list[str]] = [[] for _ in line_words]
        for i, w in enumerate(words):
            target = f"{{\\c&H0000FF00&}}{w}{{\\c}}" if i == index else w
            rendered[word_line[i]].append(target)
        body = r"\N".join(" ".join(line) for line in rendered)
        events.append((word_start, event_end, body))
    return events
