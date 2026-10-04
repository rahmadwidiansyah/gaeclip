"""Enhanced transcript-heuristic curation (fallback level 3, curation-v2).

Pure offline scoring over transcript text + timestamps. No AI, no network,
no heavy NLP. Goal: when Gemini and the audience heatmap are both
unavailable, still pick clips for a *reason* (hook, narrative, payoff,
completeness, clarity, density) instead of blindly taking windows.

Design (see curation_selection for the shared select engine):
- signal anchors (hook/question/number/transition/payoff/emphasis) are
  EXPANDED to setup+payoff spans; a sparse uniform scan guarantees
  coverage so keyword-free hooks ("I lost everything") are not missed;
- pool capped at HEURISTIC_MAX_CANDIDATES (deterministic order);
- final ranking via the shared global selection engine (dedup + diversity).
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

from .schemas import CandidateClip, TranscriptLine

# Final-score weights (§39 initial defaults, centralized here).
WEIGHTS = {
    "hook": 0.18,
    "narrative": 0.14,
    "payoff": 0.14,
    "completeness": 0.12,
    "standalone_clarity": 0.10,
    "specificity": 0.09,
    "curiosity": 0.07,
    "information_density": 0.07,
    "emphasis": 0.04,
    "sentence_quality": 0.03,
    "lexical_novelty": 0.02,
}

# Below this best score the transcript is considered unusable and the
# caller should fall through to the deterministic last resort.
# Overridable for tuning without code changes.
HEURISTIC_MIN_SCORE = float(os.getenv("GAECLIP_HEURISTIC_MIN_SCORE", "0.30"))

# Duration probes tried per anchor (seconds); filtered by job min/max.
DURATION_PROBES = (30, 45, 60, 75, 90, 105, 120)

# Bounded discovery pool (§46): deterministic order, never O(N^2).
HEURISTIC_MAX_CANDIDATES = 100

# Jaccard duplicate threshold for heuristic pool selection.
HEURISTIC_DUPLICATE_THRESHOLD = 0.60

HOOK_PHRASES = (
    "ternyata", "tapi", "namun", "padahal", "masalahnya", "yang mengejutkan",
    "mengejutkan", "tidak menyangka", "tak menyangka", "tak disangka",
    "tidak disangka", "ternyata saya", "cuma", "hanya", "paling", "terbaik",
    "terburuk", "pertama kali", "akhirnya", "hasilnya", "ternyata benar",
    "yang terjadi", "ini alasan", "alasannya", "rahasianya", "rahasia",
    "fakta", "bukti", "jangan", "berhenti", "stop", "bayangkan",
    "jangan sampai", "justru", "what if", "what happens", "the truth",
    "secret", "never", "shocking", "truth", "imagine", "do not",
)

CONTRAST_WORDS = (
    "tapi", "namun", "padahal", "justru", "meskipun", "walaupun", "sedangkan",
    "sementara", "melainkan", "but", "however", "although", "though",
    "whereas", "instead",
)

TRANSITION_WORDS = (
    "tapi", "namun", "ternyata", "kemudian", "akhirnya", "masalahnya",
    "yang terjadi", "setelah itu", "justru", "lalu", "terus", "habis",
    "kemudian", "however", "then", "after", "finally", "later",
)

PAYOFF_WORDS = (
    "akhirnya", "hasilnya", "ternyata", "jadi", "itulah sebabnya",
    "makanya", "kesimpulannya", "jawabannya", "inilah", "buktinya",
    "finally", "result", "therefore", "answer", "conclusion",
)

CURIOSITY_PATTERNS = (
    "kenapa", "mengapa", "bagaimana", "apa yang terjadi", "apakah",
    "benarkah", "gimana", "menurut kalian", "tahu nggak", "tahukah",
    "pernah nggak", "why", "what", "how", "did you know",
)

CAUSAL_WORDS = (
    "karena", "sebab", "akibat", "dampak", "membuat", "menyebabkan",
    "mempengaruhi", "sehingga", "makanya", "buktinya", "contohnya",
    "dibanding", "dibandingkan", "lebih", "paling", "kurang",
    "because", "therefore", "causes", "compared", "more", "less", "most",
)

NUMBER_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:juta|miliar|milyar|triliun|ribu|ratus|puluh|belas|"
    r"m\b|km\b|kg\b|gram|cm\b|mm\b|%|persen|tahun|bulan|minggu|hari|jam|menit|detik|"
    r"detik|kali|umur|dolar|rupiah|\brp\b|\$|ranking|juara|peringkat)?",
    re.IGNORECASE,
)

EMPHASIS_WORDS = frozenset(
    "paling ter sangat banget sekali luar biasa gila parah serius beneran "
    "penting wajib harus definitely extremely insane crazy ultimate best worst "
    "terbesar terkecil tercepat termurah termahal pertama terakhir semua "
    "definitely really very so".split()
)

# Intensifiers that turn repetition into emphasis ("takut, takut banget").
INTENSIFIERS = frozenset(
    "banget sekali beneran sangat paling ter luar biasa really very so "
    "extremely absolutely".split()
)

FILLER_WORDS = frozenset(
    "eh ehm hmm anu ya kan sih dong deh kok loh lho nah terus habis abis "
    "kayak kaya um uh er em like well".split()
)

START_FILLERS = frozenset(
    {"dan", "jadi", "nah", "terus", "kemudian", "lalu", "habis", "and", "so", "well", "then", "after"}
)

# A candidate starting with these is dangling unless the clause is
# completed inside the span (soft penalty, not a veto).
DANGLING_START = frozenset(
    {"karena", "sehingga", "tetapi", "tapi", "dan", "jadi", "yang", "kalau",
     "jika", "atau", "ini", "itu", "tersebut", "terus", "kemudian", "lalu",
     "because", "so", "and", "but", "which", "that", "if", "when", "while", "this"}
)

HANGING_WORDS = frozenset(
    {"karena", "jadi", "tetapi", "tapi", "dan", "yang", "kalau", "jika", "atau",
     "namun", "padahal", "meski", "meskipun", "supaya", "agar", "sehingga",
     "because", "so", "and", "but", "which", "that", "if", "when", "while"}
)

TERMINAL_PUNCT = (".", "!", "?", "…")

# Lightweight ID+EN stopwords for novelty/content ratios (no NLP models).
STOPWORDS = frozenset(
    "yang dan di ke dari untuk dengan pada adalah ini itu ada tidak ada "
    "saya kamu dia mereka kita kami anda beliau nya kah lah pun juga "
    "sudah telah akan sedang bisa dapat harus boleh perlu maupun serta "
    "atau tapi karena kalau jika ketika saat agar supaya bahwa meskipun "
    "sehingga sampai sejak kecuali tanpa tentang kepada oleh antara demi "
    "seperti the a an and or of to in on for with is are was were be been "
    "i you he she it we they this that these those there here what when "
    "where which who how not no do does did will would can could should "
    "have has had very just so as at by from".split()
)


def _words(text: str) -> list[str]:
    return text.split()


def _keys(words: list[str]) -> list[str]:
    return [word.lower().strip(" \t.,!?;:…—–-\"'“”‘’()[]") for word in words]


def hook_score(text: str) -> float:
    """Hook phrases + contrast/reversal + direct address (evidence, not proof)."""
    lowered = text.lower()
    hits = sum(1 for phrase in HOOK_PHRASES if phrase in lowered)
    score = min(0.8, 0.30 * hits)
    for contrast in CONTRAST_WORDS:
        pos = lowered.find(contrast)
        if pos >= 0 and any(phrase in lowered[pos + len(contrast):]
                            for phrase in HOOK_PHRASES + ("benar", "salah", "menang", "kalah", "true", "wrong")):
            score = min(1.0, score + 0.2)
            break
    return min(1.0, score)


def narrative_score(text: str) -> float:
    """Setup -> transition -> different/more-specific content."""
    lowered = text.lower()
    positions = [lowered.find(word) for word in TRANSITION_WORDS]
    positions = [pos for pos in positions if pos >= 0]
    if not positions:
        return 0.0
    first = min(positions)
    before, after = lowered[:first], lowered[first:]
    after_signals = (
        sum(1 for phrase in HOOK_PHRASES if phrase in after)
        + len(NUMBER_RE.findall(after))
        + sum(1 for word in PAYOFF_WORDS if word in after)
    )
    before_len = max(1, len(before.split()))
    density_after = after_signals / max(1, len(after.split()) / 20)
    return min(1.0, 0.5 + 0.5 * min(1.0, density_after + (0.2 if before_len >= 8 else 0.0)))


def payoff_score(text: str) -> float:
    """Results, consequences, answers — weighted toward the latter half."""
    words = _keys(_words(text))
    if not words:
        return 0.0
    hits = sum(1 for word in words if word in PAYOFF_WORDS)
    late = words[len(words) // 2:]
    late_hits = sum(1 for word in late if word in PAYOFF_WORDS)
    numbers = len(NUMBER_RE.findall(text))
    return min(1.0, 0.4 * min(hits, 2) + 0.3 * min(late_hits, 2) + 0.15 * min(numbers, 2))


def curiosity_score(text: str) -> float:
    lowered = text.lower()
    questions = text.count("?") + lowered.count("？")
    patterns = sum(1 for pat in CURIOSITY_PATTERNS if pat in lowered)
    return min(1.0, 0.5 * min(questions, 2) + 0.3 * min(patterns, 2))


def specificity_score(text: str) -> float:
    count = len(NUMBER_RE.findall(text))
    return min(1.0, 0.25 * count)


def emphasis_score(text: str) -> float:
    keys = set(_keys(_words(text)))
    hits = sum(1 for word in keys if word in EMPHASIS_WORDS)
    marks = text.count("!")
    return min(1.0, 0.3 * hits + 0.2 * min(marks, 2))


def speech_density_score(text: str, duration: float) -> float:
    """Words/sec in the ideal speech band (kept as legacy `density` key)."""
    words = _words(text)
    if not words or duration <= 0:
        return 0.0
    wps = len(words) / duration
    if wps <= 0.5 or wps >= 6.0:
        return 0.0
    if wps < 1.5:
        return (wps - 0.5) / 1.0
    if wps <= 3.5:
        return 1.0
    return max(0.0, 1.0 - (wps - 3.5) / 2.5)


def information_density_score(text: str) -> float:
    """Content-bearing tokens per total tokens (lightweight, no NLP)."""
    words = _words(text)
    if not words:
        return 0.0
    keys = _keys(words)
    numbers = 2 * len(NUMBER_RE.findall(text))
    proper = sum(1 for word, key in zip(words, keys)
                 if word[:1].isupper() and key not in STOPWORDS)
    causal = sum(1 for key in keys if key in CAUSAL_WORDS)
    long_words = sum(1 for key in keys if len(key) >= 7 and key not in STOPWORDS)
    hits = numbers + proper + causal + long_words
    return min(1.0, hits / max(8.0, len(words) * 0.25))


def lexical_novelty_score(text: str) -> float:
    """Content-word ratio: generic filler-heavy spans score low."""
    keys = [key for key in _keys(_words(text)) if key]
    if not keys:
        return 0.0
    content = [key for key in keys if key not in STOPWORDS and len(key) > 3]
    return len(content) / len(keys)


def repetition_penalty(text: str) -> float:
    """Unintentional repetition only. Intentional emphasis
    ("takut, takut banget") is exempt via intensifier check."""
    keys = [key for key in _keys(_words(text)) if key]
    if len(keys) < 2:
        return 0.0
    pairs = 0
    for first, second in zip(keys, keys[1:]):
        if first and first == second and second not in INTENSIFIERS:
            # "X, X <intensifier>" reads as emphasis, not a glitch.
            pairs += 1
    adjacent_ratio = pairs / (len(keys) - 1)
    trigrams = {" ".join(keys[i : i + 3]) for i in range(len(keys) - 2)}
    trigram_dup = 1.0 - (len(trigrams) / max(1, len(keys) - 2)) if len(keys) >= 3 else 0.0
    return min(0.6, 0.3 * adjacent_ratio + 0.25 * trigram_dup)


def filler_penalty(text: str, has_hook: bool) -> float:
    """Filler ratio, halved when a hook gives the span structure
    ("nah, masalahnya adalah..." — nah is scaffolding, not noise)."""
    keys = [key for key in _keys(_words(text)) if key]
    if not keys:
        return 0.0
    ratio = sum(1 for key in keys if key in FILLER_WORDS) / len(keys)
    return 0.3 * ratio * (0.5 if has_hook else 1.0)


def sentence_score(text: str) -> float:
    stripped = text.strip()
    if not stripped:
        return 0.0
    first = _keys([stripped.split()[0]])[0]
    good_start = 0.0 if first in START_FILLERS else 1.0
    if stripped.endswith(TERMINAL_PUNCT):
        good_end = 1.0
    elif _keys([stripped.split()[-1]])[0] in HANGING_WORDS:
        good_end = 0.0
    else:
        good_end = 0.5
    return 0.5 + 0.25 * good_start + 0.25 * good_end


def completeness_score(text: str) -> float:
    words = _words(text)
    if not words:
        return 0.0
    first = _keys([words[0]])[0]
    clean_start = 0.0 if first in START_FILLERS else 1.0
    clean_end = 1.0 if text.strip().endswith(TERMINAL_PUNCT) else 0.0
    has_body = 1.0 if len(words) >= 12 else len(words) / 12
    return 0.35 * clean_start + 0.35 * clean_end + 0.3 * has_body


def standalone_clarity_score(text: str, completeness: float) -> float:
    ends = 1.0 if text.strip().endswith(TERMINAL_PUNCT) else 0.3
    return 0.6 * completeness + 0.4 * ends


def dangling_start_penalty(text: str) -> float:
    words = _keys(_words(text))
    if words and words[0] in DANGLING_START:
        return 0.15
    return 0.0


def dangling_end_penalty(text: str) -> float:
    stripped = text.strip()
    if not stripped:
        return 0.0
    if stripped.endswith(TERMINAL_PUNCT):
        return 0.0
    if _keys([stripped.split()[-1]])[0] in HANGING_WORDS:
        return 0.2
    if stripped.endswith((",", ":", "—", "-")):
        return 0.2
    return 0.0


def density_score(text: str, duration: float) -> float:
    """Legacy alias kept for compatibility: speech-band density."""
    return speech_density_score(text, duration)


def position_score(start: float, total_duration: float) -> float:
    """Tiny tie-breaker only: 1.00 -> 0.95 across the video (§38)."""
    norm = start / max(1.0, total_duration)
    return max(0.95, min(1.0, 1.0 - norm * 0.05))


@dataclass
class ScoredCandidate:
    candidate: CandidateClip
    final: float
    breakdown: dict[str, float]


def score_candidate(
    text: str,
    start: float,
    end: float,
    total_duration: float,
) -> tuple[float, dict[str, float]]:
    """Weighted 0-1 score plus per-signal breakdown for debugging."""
    duration = max(0.1, end - start)
    hook = hook_score(text)
    completeness = completeness_score(text)
    breakdown = {
        "hook": hook,
        "narrative": narrative_score(text),
        "payoff": payoff_score(text),
        "completeness": completeness,
        "standalone_clarity": standalone_clarity_score(text, completeness),
        "specificity": specificity_score(text),
        "curiosity": curiosity_score(text),
        "information_density": information_density_score(text),
        "emphasis": emphasis_score(text),
        "sentence_quality": sentence_score(text),
        "lexical_novelty": lexical_novelty_score(text),
        # Legacy-compat keys (same semantics as curation-v1).
        "sentence": sentence_score(text),
        "density": speech_density_score(text, duration),
        "position": position_score(start, total_duration),
    }
    repetition = repetition_penalty(text)
    dangling_start = dangling_start_penalty(text)
    dangling_end = dangling_end_penalty(text)
    filler = filler_penalty(text, hook > 0)
    breakdown["repetition_penalty"] = repetition
    breakdown["dangling_start_penalty"] = dangling_start
    breakdown["dangling_end_penalty"] = dangling_end
    breakdown["filler_penalty"] = filler
    final = (sum(WEIGHTS[name] * breakdown[name] for name in WEIGHTS)
             - repetition - dangling_start - dangling_end - filler)
    final = max(0.0, min(1.0, final))
    breakdown["final"] = round(final, 4)
    for name in breakdown:
        if name != "final":
            breakdown[name] = round(breakdown[name], 4)
    return final, breakdown


def _snap_end(
    lines: list[TranscriptLine],
    start_index: int,
    end_index: int,
    start: float,
    max_duration: float,
) -> int:
    """Extend the end up to 2 lines to land on sentence punctuation."""
    end_index = min(end_index, len(lines) - 1)
    for _ in range(2):
        text = lines[end_index].text.strip()
        if text.endswith(TERMINAL_PUNCT) or text.endswith((",", ";", ":")):
            break
        nxt = end_index + 1
        if nxt >= len(lines) or lines[nxt].end - start > max_duration:
            break
        end_index = nxt
    return end_index


def _anchor_indices(lines: list[TranscriptLine]) -> list[int]:
    """Lines carrying hook/question/number/transition/payoff/emphasis."""
    anchors = []
    for index, line in enumerate(lines):
        text = line.text
        lowered = text.lower()
        if (any(phrase in lowered for phrase in HOOK_PHRASES)
                or "?" in text
                or NUMBER_RE.search(text)
                or any(word in lowered for word in TRANSITION_WORDS)
                or any(word in lowered for word in PAYOFF_WORDS)
                or any(word in _keys(_words(text)) for word in EMPHASIS_WORDS)):
            anchors.append(index)
    return anchors


def _expand_span(lines: list[TranscriptLine], anchor: int, target: float,
                 min_duration: float, max_duration: float) -> tuple[int, int] | None:
    """Grow around an anchor: backward for setup, forward for payoff,
    snapped to sentence boundaries, clamped to duration bounds."""
    start_index = anchor
    back = 0
    while start_index > 0 and back < 4:
        prev = lines[start_index - 1].text.strip()
        if prev.endswith(TERMINAL_PUNCT) and back >= 1:
            break
        start_index -= 1
        back += 1
        if lines[anchor].end - lines[start_index].start > max_duration:
            start_index += 1
            break
    end_index = anchor
    while end_index + 1 < len(lines):
        nxt = lines[end_index + 1]
        if nxt.end - lines[start_index].start > max_duration:
            break
        end_index += 1
        if lines[end_index].text.strip().endswith(TERMINAL_PUNCT):
            if lines[end_index].end - lines[start_index].start >= min(min_duration, target):
                break
        if lines[end_index].end - lines[start_index].start >= target:
            break
    end_index = _snap_end(lines, start_index, end_index,
                          lines[start_index].start, max_duration)
    duration = lines[end_index].end - lines[start_index].start
    if not min_duration - 1e-6 <= duration <= max_duration + 1e-6:
        return None
    return start_index, end_index


def discover_pool(lines: list[TranscriptLine], min_duration: float,
                  max_duration: float) -> list[tuple[int, int]]:
    """Adaptive discovery (§42, §44-46): hook-anchored expansion plus a
    sparse uniform scan (keyword-free hooks are never gated out).
    Deterministic order, spans deduplicated, capped at 100."""
    if not lines:
        return []
    spans: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    def add(span: tuple[int, int] | None) -> None:
        if span is not None and span not in seen:
            seen.add(span)
            spans.append(span)

    anchors = _anchor_indices(lines)
    probes = [target for target in DURATION_PROBES
              if min_duration - 1e-6 <= target <= max_duration + 1e-6]
    if not probes:
        probes = [(min_duration + max_duration) / 2]
    # Stride anchors evenly when over budget (no early bias).
    if len(anchors) * len(probes) > HEURISTIC_MAX_CANDIDATES // 2:
        stride = max(1, (len(anchors) * len(probes)) // (HEURISTIC_MAX_CANDIDATES // 2))
        anchors = anchors[::stride]
    for anchor in anchors:
        for target in probes:
            add(_expand_span(lines, anchor, target, min_duration, max_duration))
    # Sparse baseline scan (§45): uniform coverage independent of keywords.
    stride = max(1, len(lines) // 30)
    for start_index in range(0, len(lines), stride):
        for target in probes:
            end_index = start_index
            while (end_index + 1 < len(lines)
                   and lines[end_index + 1].end - lines[start_index].start < target):
                end_index += 1
            if end_index >= len(lines):
                continue
            end_index = _snap_end(lines, start_index, end_index,
                                  lines[start_index].start, max_duration)
            duration = lines[end_index].end - lines[start_index].start
            if min_duration - 1e-6 <= duration <= max_duration + 1e-6:
                add((start_index, end_index))
    # Deterministic cap: even stride over discovery order.
    if len(spans) > HEURISTIC_MAX_CANDIDATES:
        step = len(spans) / HEURISTIC_MAX_CANDIDATES
        spans = [spans[int(i * step)] for i in range(HEURISTIC_MAX_CANDIDATES)]
    return spans


def discover_heuristic_pool(
    lines: list[TranscriptLine],
    min_duration: float = 15,
    max_duration: float = 90,
) -> tuple[list[dict], dict[str, dict[str, float]], float]:
    """Adaptive pool discovery (§42, §44-46). Returns (pool items with
    span text for global selection, breakdown per pool id, best score)."""
    total_duration = max(1.0, max((line.end for line in lines), default=1.0))
    spans = discover_pool(lines, min_duration, max_duration)
    pool: list[dict] = []
    breakdown: dict[str, dict[str, float]] = {}
    best = 0.0
    for number, (start_index, end_index) in enumerate(spans, start=1):
        start_line, end_line = lines[start_index], lines[end_index]
        text = " ".join(line.text for line in lines[start_index:end_index + 1])
        final, parts = score_candidate(text, start_line.start, end_line.end,
                                       total_duration)
        candidate_id = f"c{number:06d}"
        breakdown[candidate_id] = parts
        best = max(best, final)
        pool.append({
            "candidate_id": candidate_id,
            "start_line_id": start_line.line_id,
            "end_line_id": end_line.line_id,
            "start": start_line.start,
            "end": end_line.end,
            "title": text[:120],
            "reason": f"Transcript heuristic fallback (score {final:.2f})",
            "score": round(final, 3),
            "source": "heuristic",
            "score_breakdown": parts,
            "text": text,
        })
    return pool, breakdown, best


def heuristic_candidates(
    lines: list[TranscriptLine],
    min_duration: float = 15,
    max_duration: float = 90,
    max_clips: int = 3,
) -> tuple[list[CandidateClip], dict[str, dict[str, float]], float]:
    """Score adaptive pool, select globally. Returns (selected
    chronological, per-selected breakdown keyed by candidate id, best
    pool score). Signature preserved for compatibility."""
    from .curation_selection import select_final_candidates

    pool, breakdown, best = discover_heuristic_pool(lines, min_duration, max_duration)
    selected, stats = select_final_candidates(
        pool, max_clips, duplicate_threshold=HEURISTIC_DUPLICATE_THRESHOLD)
    _ = stats
    chosen = []
    for item in selected:
        chosen.append(CandidateClip(
            candidate_id=item["candidate_id"],
            start_line_id=item["start_line_id"],
            end_line_id=item["end_line_id"],
            start=item["start"], end=item["end"],
            title=item["title"][:120] or f"Clip {item['candidate_id']}",
            reason=item["reason"],
            score=item["score"],
        ))
    chosen.sort(key=lambda item: item.start)
    return chosen, {item.candidate_id: breakdown[item.candidate_id] for item in chosen}, best


def sentence_boundary_bonus(text: str) -> float:
    """0-1 tiebreak for deterministic fallback: prefer clean boundaries."""
    stripped = text.strip()
    if not stripped:
        return 0.0
    words = stripped.split()
    bonus = 0.0
    if _keys([words[0]])[0] not in START_FILLERS:
        bonus += 0.5
    if stripped.endswith(TERMINAL_PUNCT):
        bonus += 0.5
    return bonus


def content_stopwords() -> frozenset:
    return STOPWORDS
