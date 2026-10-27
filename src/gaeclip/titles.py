"""Short hook titles for short-form publishing (spec §29-§37).

The Gemini `title` field is a PUBLIC hook (YouTube Shorts / TikTok /
Reels caption seed), never a summary. `reason` stays the internal
explanation — the two are never mixed.

Policy:
- hard max 60 chars; preferred 20-45 chars, 3-8 words;
- never raw-truncate (smart cut at clause boundary);
- invalid/empty/generic -> hook sentence extracted from the clip span;
- language follows the transcript; no invented facts (extractive only).
"""

from __future__ import annotations

import re

TITLE_MAX_LEN = 60
TITLE_MIN_PREFERRED = 20
TITLE_MAX_PREFERRED = 45
WORD_MIN_PREFERRED = 3
WORD_MAX_PREFERRED = 8

_GENERIC_PREFIXES = (
    r"^\s*(clip|video|konten|part|episode|ep|bagian)\b[\s\-#:]*",
    r"^\s*part\s*\d+\b[\s\-#:]*",
    r"^\s*clip\s*\d+\b[\s\-#:]*",
)

_EXCESS_PUNCT = re.compile(r"[!?.,;:…—–-]{2,}")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]",
    flags=re.UNICODE,
)

_QUESTION_WORDS_ID = ("kenapa", "mengapa", "bagaimana", "apa", "siapa",
                      "kapan", "dimana", "berapa", "apakah", "kok", "gimana")
_QUESTION_WORDS_EN = ("why", "how", "what", "who", "when", "where")
_TRIGGER_ID = ("ternyata", "jangan", "stop", "awas", "fakta", "rahasia",
               "salah", "keliru", "bahaya", "gratis", "cuma", "hanya")
_TRIGGER_EN = ("stop", "never", "secret", "mistake", "truth", "free",
               "warning", "wrong")

TITLE_PROMPT_RULES = """\
TITLE RULES (the title is the public hook for Shorts/TikTok/Reels captions):
- Maximum 60 characters; prefer 20-45 characters and 3-8 words.
- One strong idea: curiosity, surprise, conflict, question, claim, or payoff.
- Do NOT summarize the whole clip; do NOT write long descriptive titles.
- Do NOT invent facts, numbers, names, or claims absent from the transcript.
- Do NOT start with "Clip:", "Video:", "Part 1:", or similar labels.
- Avoid excessive punctuation and unnecessary emojis.
- Use natural Indonesian when the transcript is Indonesian; otherwise
  match the clip language. Never copy a whole long transcript sentence.
- Make each candidate's title pattern different from the others.
Examples of GOOD titles: "Kenapa Ini Bisa Terjadi?", "Jangan Lakukan Ini",
"Ternyata Bukan Karena Itu", "Kesalahan yang Sering Terjadi".
BAD: "Clip ini menarik karena pembicara menjelaskan tentang..." \
"""


def _words(text: str) -> list[str]:
    return text.split()


def word_count(text: str) -> int:
    return len(_words(text))


def is_generic(title: str) -> bool:
    """True for label-like non-hooks (Clip 1, Part 2, ...)."""
    text = (title or "").strip()
    if not text:
        return True
    lowered = text.lower()
    for pattern in _GENERIC_PREFIXES:
        if re.match(pattern, lowered):
            return True
    if re.fullmatch(r"[\W\d\s]+", text):
        return True
    return False


def validate_title(title: str) -> tuple[bool, list[str]]:
    """Check publish-readiness. Returns (ok, [issue codes])."""
    issues: list[str] = []
    text = (title or "").strip()
    if not text:
        return False, ["empty"]
    if len(text) > TITLE_MAX_LEN:
        issues.append("too_long")
    if is_generic(text):
        issues.append("generic")
    words = word_count(text)
    if words < WORD_MIN_PREFERRED or words > WORD_MAX_PREFERRED:
        issues.append("word_count")
    return (not issues), issues


def _strip_generic_prefix(text: str) -> str:
    lowered = text.lower()
    for pattern in _GENERIC_PREFIXES:
        match = re.match(pattern, lowered)
        if match:
            return text[match.end():].strip()
    return text


def _clean_text(text: str) -> str:
    text = _EMOJI.sub("", text or "")
    text = _EXCESS_PUNCT.sub(lambda m: m.group(0)[0], text)
    return re.sub(r"\s+", " ", text).strip()


def smart_truncate(text: str, limit: int = TITLE_MAX_LEN) -> str:
    """Cut to <= limit at a clause/sentence/word boundary (never mid-word)."""
    text = _clean_text(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "! ", "? ", "; ", ": ", ", ", " - ", " "):
        index = cut.rfind(sep)
        if index >= max(10, limit // 2):
            return cut[:index].rstrip(" .,;:!?-–—").strip()
    return cut.rstrip().rstrip(" .,;:!?-–—")


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+|\n+", (text or "").strip())
    return [part.strip() for part in parts if part.strip()]


def _language_is_indonesian(text: str) -> bool:
    lowered = f" {text.lower()} "
    hits_id = sum(1 for word in _QUESTION_WORDS_ID + _TRIGGER_ID if f" {word} " in lowered)
    hits_en = sum(1 for word in _QUESTION_WORDS_EN + _TRIGGER_EN if f" {word} " in lowered)
    return hits_id >= hits_en


def hook_title_from_text(span_text: str, limit: int = TITLE_MAX_LEN,
                           exclude: tuple[str, ...] = ()) -> str:
    """Extractive hook fallback: question > exclamation/trigger > first
    strong sentence. Skips candidates whose signature is in `exclude`
    (diversity). Never invents content; caps at `limit`."""
    sentences = _sentences(span_text)
    if not sentences:
        return "Momen Menarik Ini"
    indonesian = _language_is_indonesian(span_text)

    def _score(sentence: str) -> tuple[int, int]:
        lowered = sentence.lower()
        questions = _QUESTION_WORDS_ID if indonesian else _QUESTION_WORDS_EN
        triggers = _TRIGGER_ID if indonesian else _TRIGGER_EN
        question = 2 if sentence.rstrip().endswith("?") or \
            any(lowered.startswith(word) for word in questions) else 0
        trigger = 1 if any(word in lowered for word in triggers) or \
            sentence.rstrip().endswith("!") else 0
        return question, trigger

    ranked = sorted(sentences, key=lambda s: (_score(s), -len(s)), reverse=True)
    excluded = set(exclude)
    for sentence in ranked:
        candidate = smart_truncate(sentence.rstrip("."), limit)
        if candidate and title_signature(candidate) not in excluded:
            return candidate
    best = smart_truncate(ranked[0].rstrip("."), limit)
    return best or "Momen Menarik Ini"


def normalize_title(title: str, span_text: str = "",
                    limit: int = TITLE_MAX_LEN) -> str:
    """Make any title publish-ready: clean, de-label, smart-cap; fall back
    to an extractive hook when empty/generic/still too long."""
    text = _clean_text(_strip_generic_prefix(title or ""))
    if not text or is_generic(text):
        return hook_title_from_text(span_text, limit)
    if len(text) > limit:
        cut = smart_truncate(text, limit)
        # A cut that destroyed the hook (too short/generic) -> extract.
        if len(cut) < 12 or is_generic(cut):
            return hook_title_from_text(span_text or text, limit)
        return cut
    return text


def title_signature(title: str, words: int = 4) -> str:
    """First-N-words fingerprint for diversity checks."""
    return " ".join(_words(title.lower())[:words])


def finalize_titles(candidates, span_texts: dict) -> list:
    """Normalize every candidate title + break exact pattern duplicates.

    `candidates`: list of CandidateClip (mutated in place via model_copy).
    `span_texts`: candidate_id -> transcript span text for fallbacks.
    Returns the same list (sorted untouched).
    """
    seen: set[str] = set()
    out = []
    for clip in candidates:
        title = normalize_title(clip.title, span_texts.get(clip.candidate_id, ""))
        signature = title_signature(title)
        if signature in seen:
            # Same hook pattern as an earlier pick: re-extract strictly
            # from this clip's own span, excluding taken signatures, so
            # each title stays truthful yet distinct.
            alt = hook_title_from_text(span_texts.get(clip.candidate_id, ""),
                                       exclude=tuple(seen))
            if alt and title_signature(alt) not in seen:
                title = alt
                signature = title_signature(title)
        seen.add(signature)
        out.append(clip.model_copy(update={"title": title or clip.title}))
    return out
