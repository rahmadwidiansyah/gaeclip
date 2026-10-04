"use client";

/**
 * Pure presentation helpers for the backend-driven model list.
 * All availability/eligibility truth comes from the backend registry —
 * nothing here hardcodes model names. Sorting mirrors the backend policy
 * (eligible → channel → variant → version-aware) for display only.
 */

import type { AiModelInfo } from "./types";

export type ModelFacet = "all" | "free" | "stable" | "preview" | "flash" | "pro" | "lite";

export function versionTuple(generation: string): number[] {
  const out: number[] = [];
  for (const piece of (generation || "").split(".")) {
    const n = parseInt(piece, 10);
    if (Number.isNaN(n)) break;
    out.push(n);
  }
  return out;
}

function compareGeneration(a: string, b: string): number {
  const ta = versionTuple(a);
  const tb = versionTuple(b);
  const n = Math.max(ta.length, tb.length);
  for (let i = 0; i < n; i++) {
    const d = (ta[i] ?? 0) - (tb[i] ?? 0);
    if (d !== 0) return d;
  }
  return 0;
}

const CHANNEL_RANK: Record<string, number> = {
  stable: 0,
  latest_alias: 1,
  unknown: 2,
  preview: 3,
  experimental: 4,
};

const VARIANT_RANK: Record<string, number> = {
  flash: 0,
  "flash-lite": 1,
  pro: 2,
  ultra: 3,
  other: 4,
};

/** Dropdown ordering: eligible → stable → flash → newer generation → id. */
export function sortModels(models: AiModelInfo[]): AiModelInfo[] {
  return [...models].sort((a, b) => {
    const ea = a.eligible_for_gaeclip ? 0 : 1;
    const eb = b.eligible_for_gaeclip ? 0 : 1;
    if (ea !== eb) return ea - eb;
    const ca = CHANNEL_RANK[a.release_channel] ?? 9;
    const cb = CHANNEL_RANK[b.release_channel] ?? 9;
    if (ca !== cb) return ca - cb;
    const va = VARIANT_RANK[a.variant] ?? 9;
    const vb = VARIANT_RANK[b.variant] ?? 9;
    if (va !== vb) return va - vb;
    const g = compareGeneration(b.generation, a.generation);
    if (g !== 0) return g;
    return a.id.localeCompare(b.id);
  });
}

/** Selectable ids for primary/fallback: eligible (+free when gated). */
export function selectableModels(models: AiModelInfo[], freeTierOnly: boolean): AiModelInfo[] {
  return sortModels(
    models.filter(
      (m) =>
        m.available &&
        m.eligible_for_gaeclip &&
        (!freeTierOnly || m.free_tier === "free")
    )
  );
}

export function filterModels(models: AiModelInfo[], q: string, facet: ModelFacet): AiModelInfo[] {
  const needle = q.trim().toLowerCase();
  return models.filter((m) => {
    if (facet === "free" && m.free_tier !== "free") return false;
    if (facet === "stable" && m.release_channel !== "stable") return false;
    if (facet === "preview" && !(m.release_channel === "preview" || m.is_preview)) return false;
    if (facet === "flash" && m.variant !== "flash") return false;
    if (facet === "pro" && m.variant !== "pro") return false;
    if (facet === "lite" && m.variant !== "flash-lite") return false;
    if (needle && !`${m.id} ${m.display_name}`.toLowerCase().includes(needle)) return false;
    return true;
  });
}

export function channelBadge(channel: string): string {
  if (channel === "stable") return "STABLE";
  if (channel === "preview") return "PREVIEW";
  if (channel === "experimental") return "EXPERIMENTAL";
  if (channel === "latest_alias") return "LATEST";
  return "UNKNOWN";
}

export function pricingBadge(freeTier: string): string {
  if (freeTier === "free") return "FREE";
  if (freeTier === "paid") return "PAID";
  return "PRICING UNKNOWN";
}
