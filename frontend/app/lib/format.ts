"use client";

/**
 * Centralized duration / date presentation helpers.
 *
 * Backend contract stays in seconds (float ok). These formatters only
 * affect the presentation layer — never mutate API payloads.
 *
 * - formatDuration: human-readable source duration (id-ID):
 *     42   -> "42 dtk"
 *     65   -> "1m 05dtk"
 *     125  -> "2m 05dtk"
 *     3599 -> "59m 59dtk"
 *     3600 -> "1j"
 *     3661 -> "1j 01m 01dtk"
 *     3997 -> "1j 06m 37dtk"
 *   Returns null for null/undefined/NaN/Infinity/negative so callers
 *   can render a "—" placeholder.
 * - formatClock: media-player timestamp for clip previews:
 *     42   -> "00:42"
 *     65   -> "01:05"
 *     3661 -> "1:01:01"
 * - formatPublished: "2026-09-21" -> "21 Sep 2026" (best effort).
 */

function toFiniteSeconds(input: unknown): number | null {
  if (typeof input !== "number") return null;
  if (!isFinite(input)) return null;
  if (input < 0) return null;
  return Math.floor(input);
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

export function formatDuration(seconds?: number | null): string | null {
  const s = toFiniteSeconds(seconds);
  if (s === null) return null;
  if (s === 0) return "0 dtk";
  if (s < 60) return `${s} dtk`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `${m}m ${pad2(sec)}dtk`;
  }
  const h = Math.floor(s / 3600);
  const rest = s % 3600;
  const m = Math.floor(rest / 60);
  const sec = rest % 60;
  if (m === 0 && sec === 0) return `${h}j`;
  return `${h}j ${pad2(m)}m ${pad2(sec)}dtk`;
}

export function formatClock(seconds?: number | null): string | null {
  const s = toFiniteSeconds(seconds);
  if (s === null) return null;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `${pad2(m)}:${pad2(sec)}`;
  }
  const h = Math.floor(s / 3600);
  const rest = s % 3600;
  const m = Math.floor(rest / 60);
  const sec = rest % 60;
  return `${h}:${pad2(m)}:${pad2(sec)}`;
}

export function formatPublished(input?: string | null): string {
  if (!input) return "—";
  // Try ISO date first ("2026-09-21" / full ISO).
  const d = new Date(input.length <= 10 ? `${input}T00:00:00` : input);
  if (isNaN(d.getTime())) return input;
  try {
    return d.toLocaleDateString("id-ID", {
      day: "2-digit",
      month: "short",
      year: "numeric",
    });
  } catch {
    return input;
  }
}
