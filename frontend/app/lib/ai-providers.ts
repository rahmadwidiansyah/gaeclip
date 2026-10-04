import type { GeminiProviderInfo } from "./types";

/** Health state -> badge label + tone class suffix. Pure, unit-tested. */
export function healthBadge(state: string | undefined): { label: string; tone: string } {
  switch ((state ?? "UNKNOWN").toUpperCase()) {
    case "HEALTHY":
      return { label: "Healthy", tone: "mini-chip-done" };
    case "DEGRADED":
      return { label: "Degraded", tone: "" };
    case "RATE_LIMITED":
      return { label: "Rate Limited", tone: "mini-chip-failed" };
    case "UNAVAILABLE":
      return { label: "Unavailable", tone: "mini-chip-failed" };
    case "INVALID":
      return { label: "Invalid Key", tone: "mini-chip-failed" };
    case "DISABLED":
      return { label: "Disabled", tone: "" };
    default:
      return { label: "Unknown", tone: "" };
  }
}

/** Cooldown countdown text for a health snapshot. */
export function cooldownText(cooldownUntil: number | undefined): string | null {
  if (!cooldownUntil) return null;
  const left = Math.max(0, Math.ceil(cooldownUntil - Date.now() / 1000));
  if (left <= 0) return null;
  return `Cooldown: ${left}s`;
}

/** Next priority number for a fresh provider card. */
export function nextPriority(providers: GeminiProviderInfo[]): number {
  return providers.reduce((m, p) => Math.max(m, p.priority), 0) + 1;
}

/** Move an item in an ordered id list (up/down buttons). */
export function moveId(ids: string[], index: number, dir: -1 | 1): string[] {
  const next = [...ids];
  const j = index + dir;
  if (j < 0 || j >= next.length) return next;
  [next[index], next[j]] = [next[j], next[index]];
  return next;
}

/** Toggle an id in an open-set (provider cards stay open across reloads). */
export function toggleOpenId(openIds: string[], id: string): string[] {
  return openIds.includes(id) ? openIds.filter((x) => x !== id) : [...openIds, id];
}

/** Accordion: only one provider expanded at a time (null = all collapsed). */
export function accordionOpenId(current: string | null, id: string): string | null {
  return current === id ? null : id;
}

/** Short relative time for "last success/failure" subtitles ("2m lalu"). */
export function timeAgo(epochSeconds: number | null | undefined, nowMs = Date.now()): string | null {
  if (typeof epochSeconds !== "number" || !isFinite(epochSeconds) || epochSeconds <= 0) return null;
  const seconds = Math.max(0, Math.floor(nowMs / 1000 - epochSeconds));
  if (seconds < 60) return `${seconds} dtk lalu`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m lalu`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} jam lalu`;
  const days = Math.floor(hours / 24);
  return `${days} hari lalu`;
}

/** Quota honesty notice (spec: keys are redundancy, not quota math). */
export const QUOTA_NOTE =
  "Beberapa API key memberikan redundansi dan routing. Project Google Cloud/AI yang terpisah mungkin diperlukan untuk kuota tingkat project yang benar-benar independen.";
