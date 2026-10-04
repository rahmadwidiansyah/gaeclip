"""Shared curation engine: normalize -> validate -> dedup -> select.

Every curation level (AI, heatmap, heuristic) produces a candidate POOL of
plain dicts and runs it through the same pipeline. Deterministic last
resort keeps its own legacy path (documented): simplicity is the point
there, with the same no-overlap guarantee.

Pool item contract (all keys present after normalization):
    candidate_id, start_line_id, end_line_id, start, end, title, reason,
    score, source, score_breakdown (dict|None), text (span text, internal)
"""

from __future__ import annotations

import hashlib
import logging
import math
import re

logger = logging.getLogger(__name__)

# Backend semantic weights for Gemini discovery breakdowns (§15).
# Configurable in one place; normalized when fields are missing.
SEMANTIC_WEIGHTS = {
    "hook_strength": 0.20,
    "payoff_strength": 0.20,
    "context_completeness": 0.15,
    "standalone_clarity": 0.15,
    "information_density": 0.10,
    "novelty": 0.10,
    "clipability": 0.10,
}

SEMANTIC_FIELDS = tuple(SEMANTIC_WEIGHTS)

# Global selection bounds (§52, §57).
GLOBAL_TOP_K = 30
DUPLICATE_THRESHOLD = 0.60

# Reason codes for per-candidate rejection (auditable, never fuzzy).
INVALID_LINE_REFERENCE = "INVALID_LINE_REFERENCE"
INVALID_DURATION = "INVALID_DURATION"
DUPLICATE_ID = "DUPLICATE_ID"
EMPTY_TEXT = "EMPTY_TEXT"


def semantic_score(candidate) -> float | None:
    """Weighted semantic score from a Gemini breakdown, or None when the
    candidate carries no semantic fields (heatmap/heuristic/deterministic
    keep their own scores; source scores are never compared raw, §81)."""
    values = []
    for field, weight in SEMANTIC_WEIGHTS.items():
        value = candidate.get(field) if isinstance(candidate, dict) else getattr(candidate, field, None)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number:
            continue
        values.append((weight, max(0.0, min(1.0, number))))
    if not values:
        return None
    total = sum(weight for weight, _ in values)
    return max(0.0, min(1.0, sum(weight * value for weight, value in values) / total))


def transcript_hash(lines) -> str:
    """Stable identity of transcript content for cache compatibility (§69)."""
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.line_id.encode())
        digest.update(b"\0")
        digest.update(f"{line.start:.3f}-{line.end:.3f}".encode())
        digest.update(b"\0")
        digest.update(line.text.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def resolve_candidate_timing(start_line_id: str, end_line_id: str,
                             lines_by_id: dict,
                             line_order: dict | None = None) -> tuple[float, float]:
    """Line IDs are the authority (§4-5). LLM-sent timestamps are never
    trusted: backend resolves start/end from the transcript. Rejects
    unknown IDs, reversed ranges, and non-finite times (no fuzzy match)."""
    try:
        start_line = lines_by_id[start_line_id]
    except KeyError:
        raise ValueError(f"{INVALID_LINE_REFERENCE}: {start_line_id}") from None
    try:
        end_line = lines_by_id[end_line_id]
    except KeyError:
        raise ValueError(f"{INVALID_LINE_REFERENCE}: {end_line_id}") from None
    start, end = float(start_line.start), float(end_line.end)
    if not (math.isfinite(start) and math.isfinite(end)) or not end > start:
        raise ValueError(f"{INVALID_LINE_REFERENCE}: {start_line_id}->{end_line_id}")
    # Strict index order (§6): end line must not precede start line, even
    # when timestamps happen to overlap. No fuzzy fixups.
    if line_order is not None:
        start_order = line_order.get(start_line_id)
        end_order = line_order.get(end_line_id)
        if (start_order is not None and end_order is not None
                and end_order < start_order):
            raise ValueError(f"{INVALID_LINE_REFERENCE}: {start_line_id}->{end_line_id}")
    return start, end


def validate_one(item: dict, lines_by_id: dict, min_duration: float,
                 max_duration: float, seen_ids: set,
                 line_order: dict | None = None) -> tuple[dict | None, str | None]:
    """Validate a single pool candidate independently (§61, §79).

    Returns (normalized_item, None) or (None, reason_code). Invalid items
    are dropped with a logged reason; valid siblings are retained.
    """
    candidate_id = item.get("candidate_id", "")
    if not candidate_id or candidate_id in seen_ids:
        return None, DUPLICATE_ID
    # A pre-diagnosed reason from the discovery parser (schema mismatch
    # vs unknown line reference) survives validation verbatim.
    pre_reason = item.get("_pre_reason")
    if pre_reason:
        return None, pre_reason
    try:
        start, end = resolve_candidate_timing(
            item.get("start_line_id", ""), item.get("end_line_id", ""),
            lines_by_id, line_order)
    except (ValueError, TypeError, AttributeError) as exc:
        return None, str(exc).split(":")[0] or INVALID_LINE_REFERENCE
    text = item.get("text", "") or ""
    if not text.strip():
        return None, EMPTY_TEXT
    duration = end - start
    if not min_duration - 1e-6 <= duration <= max_duration + 1e-6:
        return None, INVALID_DURATION
    normalized = dict(item)
    normalized["start"] = start
    normalized["end"] = end
    return normalized, None


def filter_valid_candidates(pool: list[dict], lines_by_id: dict,
                            min_duration: float, max_duration: float,
                            source_duration: float | None = None,
                            line_order: dict | None = None,
                            ) -> tuple[list[dict], list[dict]]:
    """Split a pool into (valid, invalid[{candidate_id, reason}]).

    Candidates beyond the source duration are rejected (never invented
    timestamps, §51). Deterministic order: valid sorted by start.
    """
    valid: list[dict] = []
    invalid: list[dict] = []
    seen: set[str] = set()
    for item in pool:
        candidate_id = item.get("candidate_id", "?")
        normalized, reason = validate_one(item, lines_by_id, min_duration,
                                          max_duration, seen, line_order)
        if normalized is None:
            invalid.append({"candidate_id": candidate_id, "reason": reason})
            logger.info("[CURATION_VALIDATION] rejected %s reason=%s",
                        candidate_id, reason)
            continue
        if source_duration is not None and normalized["end"] > source_duration + 1e-6:
            invalid.append({"candidate_id": candidate_id,
                            "reason": "BEYOND_SOURCE_DURATION"})
            continue
        seen.add(normalized["candidate_id"])
        valid.append(normalized)
    valid.sort(key=lambda item: (item["start"], item["candidate_id"]))
    return valid, invalid


_WORD_RE = re.compile(r"[a-z0-9']+", re.IGNORECASE)


def content_words(text: str, stopwords: frozenset | None = None) -> frozenset:
    """Lightweight content tokens (no NLP models, §73)."""
    tokens = {token.lower() for token in _WORD_RE.findall(text or "") if len(token) > 2}
    if stopwords:
        tokens -= stopwords
    return frozenset(tokens)


def jaccard(first: frozenset, second: frozenset) -> float:
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def _duplicate_penalty(similarity: float, duplicate_threshold: float) -> float:
    """Only STRONGLY similar candidates are penalized significantly (§56);
    related topics pass nearly untouched."""
    if similarity < duplicate_threshold:
        return 0.25 * similarity
    return min(0.6, (similarity - 0.35) * 1.5)


def select_final_candidates(pool: list[dict], max_clips: int,
                            top_k: int = GLOBAL_TOP_K,
                            duplicate_threshold: float = DUPLICATE_THRESHOLD,
                            ) -> tuple[list[dict], dict]:
    """Bounded global selection (§52-57): rank top-K, then maximal-marginal-
    relevance rounds — each round picks the remaining non-overlapping
    candidate with the best score-minus-duplicate-penalty. Deterministic
    tie-breaks throughout (§89). Returns (selected chronological, stats)."""
    stats = {"pool": len(pool), "selected": 0, "duplicates_removed": 0,
             "overlap_rejected": 0, "considered": 0}
    if not pool or max_clips <= 0:
        return [], stats
    ranked = sorted(pool, key=lambda item: (-item["score"], item["start"],
                                            item["candidate_id"]))[:max(1, top_k)]
    stats["considered"] = len(ranked)
    token_cache = {item["candidate_id"]: content_words(item.get("text", "")) for item in ranked}
    remaining = list(ranked)
    selected: list[dict] = []
    while remaining and len(selected) < max_clips:
        best_item: dict | None = None
        best_utility = float("-inf")
        for item in remaining:
            if any(item["start"] < chosen["end"] and chosen["start"] < item["end"]
                   for chosen in selected):
                continue
            penalty = 0.0
            for chosen in selected:
                penalty = max(penalty, _duplicate_penalty(
                    jaccard(token_cache[item["candidate_id"]],
                            token_cache[chosen["candidate_id"]]),
                    duplicate_threshold))
            utility = item["score"] - penalty
            key = (utility, item["score"])
            best_key = (best_utility, best_item["score"]) if best_item else None
            if best_item is None or key > best_key or (
                    key == best_key and (item["start"], item["candidate_id"])
                    < (best_item["start"], best_item["candidate_id"])):
                best_item, best_utility = item, utility
        if best_item is None:
            break
        remaining.remove(best_item)
        selected.append(dict(best_item, _utility=round(best_utility, 4)))
    # Reap leftovers for stats: overlapping first, then near-duplicates.
    for item in remaining:
        if any(item["start"] < chosen["end"] and chosen["start"] < item["end"]
               for chosen in selected):
            stats["overlap_rejected"] += 1
        elif any(jaccard(token_cache[item["candidate_id"]],
                         token_cache[chosen["candidate_id"]]) >= duplicate_threshold
                 for chosen in selected):
            stats["duplicates_removed"] += 1
    selected.sort(key=lambda item: (item["start"], item["candidate_id"]))
    stats["selected"] = len(selected)
    logger.info("[CURATION_SELECTION] pool=%d selected=%d duplicates_removed=%d "
                "overlap_rejected=%d", stats["pool"], stats["selected"],
                stats["duplicates_removed"], stats["overlap_rejected"])
    return selected, stats


def normalize_heatmap(heatmap: list[dict] | None) -> tuple[list[dict], dict]:
    """Validate heatmap markers (§19): finite times, end > start, value
    finite and >= 0. Bad markers are ignored (never fail the job, §92)."""
    stats = {"markers": 0, "positive_markers": 0, "max_value": 0.0,
             "dropped_markers": 0}
    if not heatmap:
        return [], stats
    clean: list[dict] = []
    for marker in heatmap:
        if not isinstance(marker, dict):
            stats["dropped_markers"] += 1
            continue
        try:
            start = float(marker.get("start_time", 0))
            end = float(marker.get("end_time", start))
            value = float(marker.get("value", 0))
        except (TypeError, ValueError):
            stats["dropped_markers"] += 1
            continue
        if not (math.isfinite(start) and math.isfinite(end) and math.isfinite(value)):
            stats["dropped_markers"] += 1
            continue
        if not end > start or value < 0:
            stats["dropped_markers"] += 1
            continue
        clean.append({"start_time": start, "end_time": end, "value": value})
        stats["markers"] += 1
        if value > 0:
            stats["positive_markers"] += 1
        stats["max_value"] = max(stats["max_value"], value)
    clean.sort(key=lambda marker: marker["start_time"])
    return clean, stats


def heatmap_overlap_score(start: float, end: float, markers: list[dict]) -> float:
    """Overlap-weighted audience value (§21). 0.0 when nothing overlaps."""
    weighted = 0.0
    covered = 0.0
    for marker in markers:
        overlap = max(0.0, min(end, marker["end_time"]) - max(start, marker["start_time"]))
        if overlap:
            weighted += overlap * marker["value"]
            covered += overlap
    return weighted / covered if covered else 0.0
