"use client";

/**
 * Shared subtitle preview model — frontend mirror of the burn renderer.
 *
 * SOURCE OF TRUTH (backend, do not drift):
 * - src/gaeclip/subtitle_style.py :: resolve_subtitle_config
 * - src/gaeclip/media.py          :: subtitle_font_size / subtitle_chars_per_line
 * - src/gaeclip/subtitles.py      :: write_ass (PlayResX/Y == frame size)
 *
 * Renderer facts for the standard 1080x1920 portrait canvas:
 * - Logical canvas is ALWAYS 9:16 (compute_vertical_ladder); ASS PlayRes == frame size.
 * - Font size  = subtitle_font_size(1080,1920) = 42px  -> 42/1080 = 3.8889% of width.
 * - Max line   = subtitle_chars_per_line      = 41 chars ~= 88% of width.
 * - MarginL/R  = 6% of width; text is center-aligned inside the 88% safe width.
 * - center        -> ASS Alignment 5 (middle-center), MarginV 12% of height.
 * - bottom_center -> ASS Alignment 2 (bottom-center), MarginV 12% of height.
 * - bottom        -> ASS Alignment 2 (bottom-center), MarginV 5% of height.
 *
 * The preview frame below uses aspect-ratio 9:16 at ANY display size and
 * positions text with the SAME normalized percentages, so coordinates scale
 * uniformly: renderX = logicalX * previewW / 1080 (same for Y / 1920).
 * Percentages — never fixed pixels — keep X/Y distortion-free.
 *
 * This file is pure (no DOM) so every style x position combination is
 * unit-testable without a browser.
 */

import type { SubtitlePosition, SubtitleStyle } from "./types";

/** Logical burn canvas (portrait ladder head). Preview scales from this. */
export const SUBTITLE_LOGICAL_CANVAS = { w: 1080, h: 1920 } as const;

/** Frame is always 9:16, so a height-% MarginV converts to width-% for CSS padding
 *  (CSS vertical padding % resolves against width): cssPct = hPct * 16/9. */
export const FRAME_ASPECT_W = 9;
export const FRAME_ASPECT_H = 16;

/** 42px @1080w expressed for container-query scaling (cqw = 1% of frame width). */
export const SUBTITLE_FONT_CQW = (42 / 1080) * 100; // 3.8889

/**
 * Legibility boost: true-scale glyphs (3.89cqw -> ~6px on a 152px preview)
 * are too small to validate style. Position stays exact; only glyph scale
 * is boosted so font/weight/color/outline remain reviewable.
 */
export const PREVIEW_FONT_BOOST = 2.2;

/** Horizontal safe inset == renderer MarginL/R (6% each side -> 88% text width). */
export const SUBTITLE_SIDE_INSET_PCT = 6;

export interface SubtitlePositionLayout {
  /** Vertical placement inside the 9:16 frame. */
  justify: "center" | "flex-end";
  /** Bottom safe margin as % of frame height == renderer MarginV. */
  padBottomPct: number;
  /** Renderer ASS alignment number (for reference / debugging). */
  alignment: 5 | 2;
}

export const SUBTITLE_POSITION_LAYOUT: Record<SubtitlePosition, SubtitlePositionLayout> = {
  center: { justify: "center", padBottomPct: 0, alignment: 5 },
  bottom_center: { justify: "flex-end", padBottomPct: 12, alignment: 2 },
  bottom: { justify: "flex-end", padBottomPct: 5, alignment: 2 },
};

export interface SubtitleStyleRender {
  fontFamily: string;
  fontWeight: number;
  textTransform: "uppercase" | "none";
  color: string;
  /** Outline thickness as fraction of font size (renderer Outline px / 42px font). */
  outlineEm: number;
  shadow: boolean;
  /** Serif flag for tests (elegant_serif must be truly serif). */
  serif: boolean;
}

export const SUBTITLE_STYLE_RENDER: Record<SubtitleStyle, SubtitleStyleRender> = {
  // Outline 3, Shadow 0, yellow, DejaVu Sans bold, uppercase.
  bold_pop: {
    fontFamily: `"DejaVu Sans", Arial, sans-serif`,
    fontWeight: 800,
    textTransform: "uppercase",
    color: "#ffff00",
    outlineEm: 3 / 42,
    shadow: false,
    serif: false,
  },
  // Outline 2, Shadow 1, white base + green active word, DejaVu Sans bold, uppercase.
  dynamic_highlight: {
    fontFamily: `"DejaVu Sans", Arial, sans-serif`,
    fontWeight: 800,
    textTransform: "uppercase",
    color: "#ffffff",
    outlineEm: 2 / 42,
    shadow: true,
    serif: false,
  },
  // Outline 2, Shadow 1, white, DejaVu Sans bold, transcript casing.
  casual_center: {
    fontFamily: `"DejaVu Sans", Arial, sans-serif`,
    fontWeight: 700,
    textTransform: "none",
    color: "#ffffff",
    outlineEm: 2 / 42,
    shadow: true,
    serif: false,
  },
  // Outline 1, Shadow 1, white, DejaVu Serif regular, transcript casing.
  elegant_serif: {
    fontFamily: `"DejaVu Serif", Georgia, serif`,
    fontWeight: 400,
    textTransform: "none",
    color: "#ffffff",
    outlineEm: 1 / 42,
    shadow: true,
    serif: true,
  },
};

export interface SubtitlePreviewSpec {
  layout: SubtitlePositionLayout;
  render: SubtitleStyleRender;
  /** px fallback when container-query units are unavailable. */
  fontPxFallback: number;
  /** font-size in cqw so glyphs scale exactly with frame width (logical canvas). */
  fontCqw: number;
  sideInsetPct: number;
  /** padBottomPct converted to CSS padding-bottom % (of frame width). */
  padBottomCssPct: number;
}

/** Resolve one style x position combination. Throws on unknown names —
// every combination must map explicitly, never silently fall back. */
export function resolveSubtitlePreview(
  style: SubtitleStyle,
  position: SubtitlePosition
): SubtitlePreviewSpec {
  const layout = SUBTITLE_POSITION_LAYOUT[position];
  const render = SUBTITLE_STYLE_RENDER[style];
  if (!layout) throw new Error(`Unknown subtitle position: ${String(position)}`);
  if (!render) throw new Error(`Unknown subtitle style: ${String(style)}`);
  return {
    layout,
    render,
    fontPxFallback: 13,
    fontCqw: SUBTITLE_FONT_CQW * PREVIEW_FONT_BOOST,
    sideInsetPct: SUBTITLE_SIDE_INSET_PCT,
    padBottomCssPct: (layout.padBottomPct * FRAME_ASPECT_H) / FRAME_ASPECT_W,
  };
}
