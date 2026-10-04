"use client";

/**
 * Clips library data model — pure helpers over the existing data flow:
 *
 *   Clip --job_id--> Job --video.channel_name--> YouTube metadata
 *   (src/gaeclip/videos.py: channel/uploader; exposed as video.channel_name
 *   in the Job API response; backend also serves /api/v1/channels + ?channel=)
 *
 * Clips carry no channel of their own, so filtering resolves through the
 * job relation (no duplication of channel onto Clip). All helpers are pure
 * and unit-tested; the page combines channel + search + job with AND logic.
 */

import type { Job } from "./types";

export const ALL = "all";
/** Filter value for jobs whose channel metadata is missing. */
export const NO_CHANNEL = "__unknown__";
export const NO_CHANNEL_LABEL = "Tanpa channel";

export interface ClipRow {
  jobId: string;
  jobStatus: string;
  channel: string | null;
  clip_id: string;
  title?: string;
  score?: number;
  duration_seconds?: number | null;
  size_bytes?: number | null;
  download_url: string;
  updated: number;
}

export type ClipSort = "new" | "old" | "score" | "duration_desc" | "duration_asc";

export interface ClipFilter {
  q: string;
  channel: string;
  jobId: string;
}

/** Flatten jobs (with video/channel metadata) into clip rows. */
export function flattenClips(jobs: Job[]): ClipRow[] {
  const rows: ClipRow[] = [];
  for (const j of jobs) {
    for (const c of j.clips ?? []) {
      rows.push({
        jobId: j.job_id,
        jobStatus: j.status,
        channel: j.video?.channel_name ?? null,
        clip_id: c.clip_id,
        title: c.title,
        score: c.score,
        duration_seconds: c.duration_seconds,
        size_bytes: c.size_bytes,
        download_url: c.download_url,
        updated: j.updated_at ?? j.created_at ?? 0,
      });
    }
  }
  return rows;
}

/** Distinct channels present in the rows (deduped, clip counts, sorted). */
export function distinctChannels(rows: ClipRow[]): { value: string; label: string; count: number }[] {
  const map = new Map<string, number>();
  for (const r of rows) {
    const key = r.channel ?? NO_CHANNEL;
    map.set(key, (map.get(key) ?? 0) + 1);
  }
  return [...map.entries()]
    .map(([value, count]) => ({
      value,
      label: value === NO_CHANNEL ? NO_CHANNEL_LABEL : value,
      count,
    }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

/** AND-combination: channel + search (title/id/channel) + job. */
export function filterClips(rows: ClipRow[], f: ClipFilter): ClipRow[] {
  const needle = f.q.trim().toLowerCase();
  return rows.filter((r) => {
    if (f.channel !== ALL) {
      if (f.channel === NO_CHANNEL ? r.channel !== null : r.channel !== f.channel) return false;
    }
    if (f.jobId !== ALL && r.jobId !== f.jobId) return false;
    if (needle && !`${r.clip_id} ${r.title ?? ""} ${r.channel ?? ""}`.toLowerCase().includes(needle)) return false;
    return true;
  });
}

/** All sort modes actually implemented (no decorative options). */
export function sortClips(rows: ClipRow[], sort: ClipSort): ClipRow[] {
  const out = [...rows];
  switch (sort) {
    case "old":
      out.sort((a, b) => a.updated - b.updated);
      break;
    case "score":
      out.sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
      break;
    case "duration_desc":
      out.sort((a, b) => (b.duration_seconds ?? -1) - (a.duration_seconds ?? -1));
      break;
    case "duration_asc":
      out.sort((a, b) => (a.duration_seconds ?? Infinity) - (b.duration_seconds ?? Infinity));
      break;
    default:
      out.sort((a, b) => b.updated - a.updated);
      break;
  }
  return out;
}
