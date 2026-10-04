import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  FRAME_ASPECT_H,
  FRAME_ASPECT_W,
  PREVIEW_FONT_BOOST,
  SUBTITLE_FONT_CQW,
  SUBTITLE_LOGICAL_CANVAS,
  SUBTITLE_SIDE_INSET_PCT,
  resolveSubtitlePreview,
} from "./subtitles.ts";
import type { SubtitlePosition, SubtitleStyle } from "./types.ts";

const STYLES: SubtitleStyle[] = ["bold_pop", "dynamic_highlight", "casual_center", "elegant_serif"];
const POSITIONS: SubtitlePosition[] = ["center", "bottom_center", "bottom"];

describe("subtitle logical canvas mirrors burn renderer", () => {
  it("canvas is 1080x1920 portrait (portrait ladder head)", () => {
    assert.equal(SUBTITLE_LOGICAL_CANVAS.w, 1080);
    assert.equal(SUBTITLE_LOGICAL_CANVAS.h, 1920);
  });
  it("frame aspect is 9:16", () => {
    assert.equal(FRAME_ASPECT_W, 9);
    assert.equal(FRAME_ASPECT_H, 16);
  });
  it("font derives from subtitle_font_size(1080,1920)=42px", () => {
    assert.ok(Math.abs(SUBTITLE_FONT_CQW - (42 / 1080) * 100) < 1e-9);
  });
  it("side inset mirrors renderer MarginL/R 6%", () => {
    assert.equal(SUBTITLE_SIDE_INSET_PCT, 6);
  });
});

describe("all 12 style x position combinations resolve explicitly", () => {
  const expectedLayout: Record<SubtitlePosition, { justify: string; padBottomPct: number; alignment: number }> = {
    // center -> ASS Alignment 5 (middle-center); bottom_* -> Alignment 2.
    // MarginV: 12% bottom_center, 5% bottom (subtitle_style.py).
    center: { justify: "center", padBottomPct: 0, alignment: 5 },
    bottom_center: { justify: "flex-end", padBottomPct: 12, alignment: 2 },
    bottom: { justify: "flex-end", padBottomPct: 5, alignment: 2 },
  };
  for (const style of STYLES) {
    for (const position of POSITIONS) {
      it(`${style} x ${position}`, () => {
        const spec = resolveSubtitlePreview(style, position);
        const exp = expectedLayout[position];
        assert.equal(spec.layout.justify, exp.justify);
        assert.equal(spec.layout.padBottomPct, exp.padBottomPct);
        assert.equal(spec.layout.alignment, exp.alignment);
        // CSS padding-bottom % resolves against WIDTH: convert 16/9 for the 9:16 frame.
        assert.ok(
          Math.abs(spec.padBottomCssPct - (exp.padBottomPct * FRAME_ASPECT_H) / FRAME_ASPECT_W) < 1e-9,
          `padBottomCssPct=${spec.padBottomCssPct}`
        );
      });
    }
  }
  it("bottom differs from bottom_center (renderer uses 5% vs 12% MarginV)", () => {
    const bc = resolveSubtitlePreview("casual_center", "bottom_center");
    const b = resolveSubtitlePreview("casual_center", "bottom");
    assert.notEqual(b.padBottomCssPct, bc.padBottomCssPct);
    assert.ok(b.padBottomCssPct < bc.padBottomCssPct);
  });
  it("center is vertically centered with no bottom push", () => {
    const c = resolveSubtitlePreview("casual_center", "center");
    assert.equal(c.layout.justify, "center");
    assert.equal(c.padBottomCssPct, 0);
  });
});

describe("style rendering mirrors ASS style rows", () => {
  it("bold_pop: yellow, bold, uppercase, thickest outline, no shadow", () => {
    const { render } = resolveSubtitlePreview("bold_pop", "bottom_center");
    assert.equal(render.color, "#ffff00");
    assert.equal(render.textTransform, "uppercase");
    assert.ok(render.fontWeight >= 800);
    assert.ok(!render.serif);
    assert.equal(render.shadow, false);
  });
  it("dynamic_highlight: white base, bold, uppercase", () => {
    const { render } = resolveSubtitlePreview("dynamic_highlight", "bottom_center");
    assert.equal(render.color, "#ffffff");
    assert.equal(render.textTransform, "uppercase");
    assert.ok(render.fontWeight >= 800);
  });
  it("casual_center: white, transcript casing", () => {
    const { render } = resolveSubtitlePreview("casual_center", "bottom_center");
    assert.equal(render.color, "#ffffff");
    assert.equal(render.textTransform, "none");
  });
  it("elegant_serif: truly serif, regular weight, transcript casing, thinnest outline", () => {
    const { render } = resolveSubtitlePreview("elegant_serif", "bottom_center");
    assert.equal(render.serif, true);
    assert.match(render.fontFamily, /serif/i);
    assert.equal(render.fontWeight, 400);
    assert.equal(render.textTransform, "none");
  });
  it("outline thickness follows renderer Outline px (3/2/2/1 over 42px font)", () => {
    const em = (s: SubtitleStyle) => resolveSubtitlePreview(s, "bottom_center").render.outlineEm;
    assert.ok(Math.abs(em("bold_pop") - 3 / 42) < 1e-9);
    assert.ok(Math.abs(em("dynamic_highlight") - 2 / 42) < 1e-9);
    assert.ok(Math.abs(em("casual_center") - 2 / 42) < 1e-9);
    assert.ok(Math.abs(em("elegant_serif") - 1 / 42) < 1e-9);
    assert.ok(em("bold_pop") > em("casual_center") && em("casual_center") > em("elegant_serif"));
  });
});

describe("preview font scaling (logical canvas)", () => {
  it("fontCqw scales with frame width; px fallback keeps text legible", () => {
    const spec = resolveSubtitlePreview("casual_center", "bottom_center");
    assert.ok(Math.abs(spec.fontCqw - SUBTITLE_FONT_CQW * PREVIEW_FONT_BOOST) < 1e-9);
    assert.equal(spec.fontPxFallback, 13);
  });
});

describe("unknown names throw (never silent fallback)", () => {
  it("unknown position throws", () => {
    assert.throws(() => resolveSubtitlePreview("casual_center", "top" as SubtitlePosition));
  });
  it("unknown style throws", () => {
    assert.throws(() => resolveSubtitlePreview("neon" as SubtitleStyle, "center"));
  });
});
