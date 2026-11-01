from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .schemas import TranscriptLine

TIMESTAMP = re.compile(r"(?:(\d{1,2}):)?(\d{2}):(\d{2})[,.](\d{3})")


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str
    index: int


def parse_timestamp(value: str) -> float:
    match = TIMESTAMP.search(value.strip())
    if not match:
        raise ValidationError(f"Invalid subtitle timestamp: {value}")
    hours, minutes, seconds, millis = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text.replace("\n", " "))
    text = re.sub(r"<[^>]+>", "", text)
    # Auto-caption pause artifacts ("...", "....") carry no meaning on screen.
    text = re.sub(r"\s*\.{2,}\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _collapse_repeats(text)


def _word_key(word: str) -> str:
    """Normalized comparison key: case- and punctuation-insensitive.

    Speech-recognition overlaps usually differ only in casing or trailing
    punctuation ("Kali," vs "kali"), so dedup must compare keys, never raw
    strings. Display always keeps the original words.
    """
    return word.lower().strip(" \t.,!?;:…—–-\"'“”‘’()[]")


def _collapse_repeats(text: str) -> str:
    """Drop consecutive duplicated word sequences from auto-captions.

    Overlapping auto-caption cues often repeat whole phrases
    ("wah kamu dia ... wah kamu") once merged; keep a single copy.
    Matching uses normalized keys; output keeps original spelling.
    """
    words = text.split()
    keys = [_word_key(word) for word in words]
    total = len(words)
    if total < 2:
        return text
    # Full-text repetition: "P P" or "P P + partial tail of P".
    for size in range(1, total // 2 + 1):
        if all(keys[i] == keys[i % size] for i in range(total)):
            return " ".join(words[:size])
    # Any adjacent duplicated phrase ("head P P tail" included): keep one copy.
    for size in range(total // 2, 0, -1):
        collapsed_words: list[str] = []
        collapsed_keys: list[str] = []
        i = 0
        while i < len(words):
            if keys[i : i + size] == keys[i + size : i + 2 * size]:
                collapsed_words.extend(words[i : i + size])
                collapsed_keys.extend(keys[i : i + size])
                i += 2 * size
            else:
                collapsed_words.append(words[i])
                collapsed_keys.append(keys[i])
                i += 1
        words, keys = collapsed_words, collapsed_keys
    return " ".join(words)


def _strip_overlap(previous: str, incoming: str, max_words: int = 12) -> str:
    """Remove the head of `incoming` that repeats the tail of `previous`.

    Rolling-window captions re-emit the last words of the previous cue
    ("...cuma 3 kali," / "dan saya makan ini cuma 3 kali, jadi...").
    The longest normalized suffix/prefix overlap is dropped so the
    sentence continues instead of restarting.
    """
    prev = previous.split()
    nxt = incoming.split()
    if not prev or not nxt:
        return incoming
    prev_keys = [_word_key(word) for word in prev]
    next_keys = [_word_key(word) for word in nxt]
    best = 0
    for size in range(1, min(max_words, len(prev), len(nxt)) + 1):
        if prev_keys[-size:] == next_keys[:size]:
            best = size
    return " ".join(nxt[best:])


def _join_without_overlap(previous: str, incoming: str) -> str:
    """Merge two cue texts, continuing the sentence instead of restarting it."""
    stripped = _strip_overlap(previous, incoming)
    if not stripped:
        return previous
    return _collapse_repeats(f"{previous} {stripped}")


def strip_cross_cue_repeats(cues: list[Cue], max_gap: float = 0.5, min_words: int = 2) -> list[Cue]:
    """Remove rolling-window repeats across consecutive final cues.

    Time-gap merging only joins near-adjacent cues, but speech-recognition
    windows also repeat words across overlapping or slightly gapped cues
    ("...cuma 3 kali," / "dan saya makan ini cuma 3 kali, jadi...").
    When the next cue starts with the tail of the previous one, drop the
    repeated head so the sentence continues ("jadi menurut saya...").
    Only applies to overlapping/adjacent cues to avoid touching genuine
    repetitions spoken minutes apart. Display words are never invented;
    the repeated head is the same acoustic content, shown once.
    """
    result: list[Cue] = list(cues)
    for i in range(1, len(result)):
        previous, current = result[i - 1], result[i]
        if current.start - previous.end > max_gap:
            continue
        stripped = _strip_overlap(previous.text, current.text)
        if stripped == current.text:
            continue
        dropped = len(current.text.split()) - len(stripped.split())
        if dropped < min_words:
            continue
        if not stripped:
            result[i] = Cue(current.start, current.end, "", current.index)
        else:
            result[i] = Cue(current.start, current.end, stripped, current.index)
    return [cue for cue in result if cue.text and cue.end > cue.start]


def parse_srt(text: str, source: str = "youtube_auto", language: str = "id") -> list[TranscriptLine]:
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    cues: list[Cue] = []
    for index, block in enumerate(blocks):
        lines = block.splitlines()
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        timing = lines[timing_index].split("-->")
        if len(timing) != 2:
            raise ValidationError("Malformed SRT timing line")
        start, end = parse_timestamp(timing[0]), parse_timestamp(timing[1])
        normalized = _normalize_text(" ".join(lines[timing_index + 1 :]))
        if not normalized:
            continue
        if start < 0 or end <= start:
            raise ValidationError("Subtitle cue has invalid range")
        cues.append(Cue(start, end, normalized, index))
    return normalize_cues(cues, source=source, language=language)


def parse_vtt(text: str, source: str = "youtube_auto", language: str = "id") -> list[TranscriptLine]:
    """Parse WebVTT cues using the same normalized transcript contract as SRT."""
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    cues: list[Cue] = []
    for index, block in enumerate(blocks):
        lines = [line for line in block.splitlines() if line.strip() != "WEBVTT"]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        timing = lines[timing_index].split("-->", 1)
        if len(timing) != 2:
            raise ValidationError("Malformed WebVTT timing line")
        start, end = parse_timestamp(timing[0]), parse_timestamp(timing[1])
        normalized = _normalize_text(" ".join(lines[timing_index + 1:]))
        if not normalized:
            continue
        if end <= start:
            raise ValidationError("Subtitle cue has invalid range")
        cues.append(Cue(start, end, normalized, index))
    return normalize_cues(cues, source=source, language=language)


def normalize_cues(
    cues: list[Cue],
    source: str = "youtube_auto",
    language: str = "id",
    merge_gap: float = 0.25,
) -> list[TranscriptLine]:
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.end, cue.index))
    unique: list[Cue] = []
    for cue in ordered:
        if any(
            cue.text == old.text
            and cue.start >= old.start
            and cue.end <= old.end
            for old in unique
        ):
            continue
        if unique and cue.start - unique[-1].end <= 0.05 and cue.text == unique[-1].text:
            previous = unique[-1]
            unique[-1] = Cue(previous.start, max(previous.end, cue.end), previous.text, previous.index)
            continue
        unique.append(cue)
    merged: list[Cue] = []
    for cue in unique:
        if (
            merged
            and cue.start - merged[-1].end <= merge_gap
            and cue.end - merged[-1].start <= 4
            and len(merged[-1].text) + 1 + len(cue.text) <= 80
        ):
            previous = merged[-1]
            combined = _join_without_overlap(previous.text, cue.text)
            merged[-1] = Cue(previous.start, max(previous.end, cue.end), combined, previous.index)
        else:
            merged.append(cue)
    resegmented = resegment_sentences(merged)
    deduped = strip_cross_cue_repeats(resegmented)
    return [
        TranscriptLine(
            line_id=f"l{index:06d}",
            start=cue.start,
            end=cue.end,
            text=cue.text,
            language=language,
            source=source,
        )
        for index, cue in enumerate(deduped, start=1)
    ]


SENTENCE_MAX_CHARS = 80


def _distribute_time(start: float, end: float, parts: list[str]) -> list[Cue]:
    """Split [start, end] across text parts proportionally to character length.

    Word-level timestamps are usually unavailable, so each part gets a share
    of the span matching its share of characters. Timing still follows the
    speech span; only internal boundaries are estimated.
    """
    total = sum(len(part) for part in parts) or 1
    out: list[Cue] = []
    cursor = start
    for i, part in enumerate(parts):
        boundary = end if i == len(parts) - 1 else start + (end - start) * (
            sum(len(item) for item in parts[: i + 1]) / total
        )
        # Guard against zero-length spans from rounding.
        boundary = min(max(boundary, cursor + 0.1), end) if end - cursor > 0.2 else end
        out.append(Cue(cursor, boundary, part.strip(), i))
        cursor = boundary
    return [cue for cue in out if cue.end > cue.start and cue.text]


def _split_sentence_long(start: float, end: float, text: str, max_chars: int) -> list[Cue]:
    """Split an overlong sentence at clause boundaries (comma, colon, dash)."""
    stripped = text.strip()
    if not stripped or len(stripped) <= max_chars:
        return [Cue(start, end, stripped, 0)] if stripped else []
    clauses = [part for part in re.split(r"(?<=[,;:—–-])\s+", stripped) if part]
    if len(clauses) <= 1:
        return [Cue(start, end, stripped, 0)]
    parts: list[Cue] = []
    for part in _distribute_time(start, end, clauses):
        parts.extend(_split_sentence_long(part.start, part.end, part.text, max_chars))
    return parts


def resegment_sentences(cues: list[Cue], max_chars: int = SENTENCE_MAX_CHARS) -> list[Cue]:
    """Re-cut cues along sentence/clause boundaries instead of time slices.

    Time-gap merging produces arbitrary slices that cut mid-sentence and
    restart repeated fragments. This pass re-cuts each merged span at
    sentence-ending punctuation first, then at clause punctuation for
    sentences that are still too long for 1-2 display lines. A sentence
    that crosses the original cue boundary keeps flowing naturally: the
    next cue continues the remainder instead of repeating the head.
    Display words are never dropped; only timing boundaries move.
    """
    result: list[Cue] = []
    for cue in cues:
        sentences = [
            part for part in re.split(r"(?<=[.!?…])\s+", cue.text.strip()) if part
        ]
        if len(sentences) <= 1:
            result.extend(_split_sentence_long(cue.start, cue.end, cue.text, max_chars))
            continue
        for part in _distribute_time(cue.start, cue.end, sentences):
            result.extend(
                _split_sentence_long(part.start, part.end, part.text, max_chars)
            )
    ordered = sorted(result, key=lambda cue: (cue.start, cue.end))
    return [
        Cue(cue.start, cue.end, cue.text, index)
        for index, cue in enumerate(ordered)
    ]


def _wrap_text(text: str, max_chars: int = 34, max_lines: int = 2) -> str:
    """Wrap into at most `max_lines` lines, preserving word order.

    Short text uses greedy fill; overflow is re-balanced sequentially so no
    line grows without bound and text never stacks off-frame.
    """
    words = text.split()
    if not words:
        return ""
    lines: list[str] = []
    current: list[str] = []
    current_length = 0
    for word in words:
        next_length = current_length + (1 if current else 0) + len(word)
        if current and next_length > max_chars:
            lines.append(" ".join(current))
            current = [word]
            current_length = len(word)
        else:
            current.append(word)
            current_length = next_length
    if current:
        lines.append(" ".join(current))
    if len(lines) <= max_lines:
        return "\n".join(lines)
    total = sum(len(word) for word in words) + max(0, len(words) - 1)
    target = total / max_lines
    balanced: list[str] = []
    current = []
    current_length = 0
    for word in words:
        add = len(word) + (1 if current else 0)
        if current and len(balanced) < max_lines - 1 and current_length + add > target:
            balanced.append(" ".join(current))
            current = [word]
            current_length = len(word)
        else:
            current.append(word)
            current_length += add
    if current:
        balanced.append(" ".join(current))
    return "\n".join(balanced[:max_lines])


# Words that naturally start a new clause vs a mere phrase: clause starters
# (karena, jadi, tapi, ...) outrank prepositions (dengan, di, ...) so the cut
# lands on a real clause boundary ("...makanan ini / karena rasanya...").
_CLAUSE_STARTERS = frozenset(
    "karena jadi tapi namun sedangkan sementara lalu kemudian kalau jika apabila "
    "saat ketika agar supaya bahwa meskipun walaupun sehingga sampai sejak "
    "melainkan kecuali where when while because and or but then so".split()
)
_PHRASE_STARTERS = frozenset(
    "dan atau sambil yang di ke dari untuk dengan pada sebagai tanpa tentang "
    "kepada oleh antara demi seperti telah sudah akan sedang bisa dapat harus "
    "boleh perlu maupun serta".split()
)


def _best_cut(words: list[str]) -> int:
    """Word index to split before: clause punctuation first, then clause
    starters, then phrase starters, then plain middle. Within a class,
    the boundary closest to the middle wins."""
    total = sum(len(word) for word in words) + max(0, len(words) - 1)
    best = (3, float("inf"), 1)
    for cut in range(1, len(words)):
        left_len = sum(len(word) for word in words[:cut]) + (cut - 1)
        left_last = words[cut - 1]
        right_first = _word_key(words[cut])
        if left_last and left_last[-1] in ",;:—–-!?":
            rank = 0
        elif right_first in _CLAUSE_STARTERS:
            rank = 1
        elif right_first in _PHRASE_STARTERS:
            rank = 2
        else:
            rank = 3
        score = (rank, abs(left_len - total / 2), cut)
        if score < best:
            best = score
    return best[2]


def _split_long_cue(start: float, end: float, text: str, max_chars: int) -> list[tuple[float, float, str]]:
    """Split a cue that would need more than 2 lines into timed halves.

    The cut prefers clause punctuation, then natural phrase boundaries, so
    a sentence is never chopped mid-phrase just because it is long. Word
    timestamps are estimated proportionally to character counts so no text
    is lost and every rendered cue fits in 2 lines.
    """
    capacity = 2 * max_chars
    if len(text) <= capacity:
        return [(start, end, text)]
    words = text.split()
    if len(words) < 2:
        return [(start, end, text)]
    cut = min(max(_best_cut(words), 1), len(words) - 1)
    left = " ".join(words[:cut])
    right = " ".join(words[cut:])
    mid = start + (end - start) * (len(left) / max(1, len(left) + 1 + len(right)))
    mid = min(max(mid, start + 0.1), end - 0.1) if end - start > 0.2 else (start + end) / 2
    return _split_long_cue(start, mid, left, max_chars) + _split_long_cue(mid, end, right, max_chars)


# Punctuation that signals a natural phrase break: a line ending here reads
# as a finished thought even when the hard word cap would allow more words.
_SENTENCE_END_PUNCT = frozenset(".!?…")

# Shortest display slice worth emitting (seconds). Guards against
# zero/negative durations on very short cues; word order is preserved and
# the final chunk always ends exactly at the cue end.
_MIN_CHUNK_SECONDS = 0.05

DEFAULT_MAX_WORDS_PER_LINE = 3
MIN_MAX_WORDS_PER_LINE = 1
MAX_MAX_WORDS_PER_LINE = 3


def normalize_max_words_per_line(value: Any, default: int = DEFAULT_MAX_WORDS_PER_LINE) -> int:
    """Normalize the per-line word cap into [1, 3].

    ``None``/unparseable values fall back to ``default`` (3); anything above
    3 is clamped to 3, anything below 1 is clamped to 1. Normalization (not
    rejection) keeps CLI/pipeline paths robust; the HTTP API additionally
    validates the raw range.
    """
    try:
        number = int(value) if value is not None else int(default)
    except (TypeError, ValueError):
        return int(default)
    return max(MIN_MAX_WORDS_PER_LINE, min(MAX_MAX_WORDS_PER_LINE, number))


def split_words(text: str) -> list[str]:
    """Split display text into word tokens.

    Whitespace-only split: punctuation stays glued to its word, hyphenated
    tokens (``gift-gift``), numbers, abbreviations, apostrophes and Unicode
    all count as exactly one token each. Punctuation alone is never a word.
    """
    return [token for token in str(text or "").split() if token]


def chunk_words(words: list[str], max_words: int = DEFAULT_MAX_WORDS_PER_LINE) -> list[list[str]]:
    """Pack words into lines of at most ``max_words`` tokens.

    Greedy fill, but when the window holds more words than fit, the cut
    prefers the last word ending in sentence punctuation (``. ! ? …``) so
    phrase breaks land on natural pauses instead of mid-phrase. Word order
    is preserved; no word is added, removed, duplicated, or split.
    """
    cap = normalize_max_words_per_line(max_words)
    chunks: list[list[str]] = []
    index = 0
    total = len(words)
    while index < total:
        take = min(cap, total - index)
        if take == cap and total - index > cap:
            for size in range(take, 0, -1):
                if words[index + size - 1][-1:] in _SENTENCE_END_PUNCT:
                    take = size
                    break
        chunks.append(words[index:index + take])
        index += take
    return [chunk for chunk in chunks if chunk]


def split_cue_words(
    start: float,
    end: float,
    text: str,
    max_words: int = DEFAULT_MAX_WORDS_PER_LINE,
) -> list[tuple[float, float, str]]:
    """Split one cue into timed word-count chunks.

    Timing: proportional to character share (spaces included), same rule as
    the existing char-based splitter. First chunk starts exactly at
    ``start``, last ends exactly at ``end``; boundaries are strictly
    increasing (0.05s floor) so cues stay chronological with no overlap and
    no zero/negative durations. Invalid spans or empty text yield [].
    """
    try:
        start_f = float(start)
        end_f = float(end)
    except (TypeError, ValueError):
        return []
    words = split_words(text)
    if not words or not (end_f > start_f):
        return []
    chunks = chunk_words(words, max_words)
    if not chunks:
        return []
    if len(chunks) == 1:
        return [(start_f, end_f, " ".join(chunks[0]))]
    sizes = [sum(len(word) for word in chunk) + max(0, len(chunk) - 1) for chunk in chunks]
    total = sum(sizes) or 1
    span = end_f - start_f
    bounds = [start_f]
    cumulative = 0
    for size in sizes[:-1]:
        cumulative += size
        bounds.append(start_f + span * cumulative / total)
    bounds.append(end_f)
    out: list[tuple[float, float, str]] = []
    for i, chunk in enumerate(chunks):
        chunk_start = bounds[i] if i == 0 else max(bounds[i], out[-1][1] + _MIN_CHUNK_SECONDS / 2)
        chunk_end = bounds[i + 1]
        if i > 0:
            chunk_start = min(chunk_start, end_f - _MIN_CHUNK_SECONDS)
        chunk_start = round(max(start_f, chunk_start), 3)
        chunk_end = round(min(end_f, chunk_end), 3)
        if chunk_end - chunk_start < _MIN_CHUNK_SECONDS and i < len(chunks) - 1:
            chunk_end = round(min(end_f, chunk_start + _MIN_CHUNK_SECONDS), 3)
        if chunk_end > chunk_start:
            out.append((chunk_start, chunk_end, " ".join(chunk)))
    if not out:
        return [(start_f, end_f, " ".join(words))]
    # The final chunk always owns the cue end (no drift from rounding).
    last_start, _, last_text = out[-1]
    out[-1] = (last_start, round(end_f, 3), last_text)
    return out

def _select_cues(
    lines: list[TranscriptLine],
    offset: float = 0,
    clip_duration: float | None = None,
    max_chars: int = 34,
    max_words: int = DEFAULT_MAX_WORDS_PER_LINE,
) -> list[tuple[float, float, str]]:
    window = clip_duration if clip_duration is not None else 3600
    cap = normalize_max_words_per_line(max_words)
    cues: list[tuple[float, float, str]] = []
    for line in lines:
        if not (line.end > offset and line.start < offset + window):
            continue
        start = max(0, line.start - offset)
        end = line.end - offset
        if clip_duration is not None:
            end = min(end, clip_duration)
        end = max(0, end)
        if end <= start:
            continue
        for piece_start, piece_end, piece_text in _split_long_cue(start, end, line.text, max_chars):
            cues.extend(split_cue_words(piece_start, piece_end, piece_text, cap))
    return cues


def write_srt(
    lines: list[TranscriptLine],
    path,
    offset: float = 0,
    clip_duration: float | None = None,
    max_chars: int = 34,
    max_words: int = DEFAULT_MAX_WORDS_PER_LINE,
) -> None:
    def stamp(seconds: float) -> str:
        millis = max(0, round(seconds * 1000))
        hours, remainder = divmod(millis, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    cues = _select_cues(lines, offset, clip_duration, max_chars, max_words)
    path.write_text(
        "\n\n".join(
            f"{index}\n{stamp(start)} --> {stamp(end)}\n{_wrap_text(text, max_chars)}"
            for index, (start, end, text) in enumerate(cues, start=1)
        ) + ("\n" if cues else ""),
        encoding="utf-8",
    )


def write_ass(
    lines: list[TranscriptLine],
    path,
    offset: float = 0,
    clip_duration: float | None = None,
    max_chars: int = 34,
    width: int = 1080,
    height: int = 1920,
    font_size: int | None = None,
    style: str = "casual_center",
    position: str = "bottom_center",
    max_words: int = DEFAULT_MAX_WORDS_PER_LINE,
) -> None:
    """Write an ASS file with explicit PlayRes so FontSize is exact pixels.

    Plain SRT has no PlayRes header, so libass falls back to 384x288 and
    scales fonts up ~6.7x on 1080x1920 video. ASS with matching PlayRes
    gives true 1:1 pixel sizing on any aspect ratio.

    ``style``/``position`` select a SubtitleRenderConfig; ``dynamic_highlight``
    additionally emits one Dialogue event per word with inline {\\c} tags so
    the active word renders green while the rest stays white (approximate
    word timing, see subtitle_style.approximate_word_timing).
    """
    from .subtitle_style import apply_case, ass_style_row, karaoke_events, resolve_subtitle_config

    cfg = resolve_subtitle_config(style, position, width, height)
    if font_size is not None:
        import dataclasses

        cfg = dataclasses.replace(cfg, font_size=font_size)

    def stamp(seconds: float) -> str:
        centis = max(0, round(seconds * 100))
        hours, remainder = divmod(centis, 360_000)
        minutes, remainder = divmod(remainder, 6_000)
        secs, centis = divmod(remainder, 100)
        return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"

    cues = _select_cues(lines, offset, clip_duration, max_chars, max_words)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 2\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"{ass_style_row(cfg)}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    parts: list[str] = []
    for start, end, text in cues:
        text = apply_case(cfg, text)
        if cfg.highlight:
            for word_start, word_end, body in karaoke_events(start, end, text, max_chars):
                parts.append(
                    f"Dialogue: 0,{stamp(word_start)},{stamp(word_end)},Gaeclip,,0,0,0,,{body}\n"
                )
        else:
            body = _wrap_text(text, max_chars).replace(chr(10), chr(92) + "N")
            parts.append(
                f"Dialogue: 0,{stamp(start)},{stamp(end)},Gaeclip,,0,0,0,,{body}\n"
            )
    Path(path).write_text(header + "".join(parts), encoding="utf-8")
