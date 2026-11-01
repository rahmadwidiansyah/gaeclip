import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_BRANDING,
  brandWindowLabel,
  clampBrandDuration,
  isCornerTemplate,
  isEndcardTemplate,
  resolveBrandingPreview,
} from "./branding.ts";
import type { BrandingForm } from "./branding.ts";

const base: BrandingForm = {
  ...DEFAULT_BRANDING,
  enabled: true,
  channelName: "Kreator Hebat",
};

describe("branding preview model", () => {
  it("resolves all template x position combos", () => {
    const templates = ["glass_end", "minimal_corner", "creator_tag", "creator_outro"] as const;
    const positions = [
      "top_left",
      "top_right",
      "bottom_left",
      "bottom_right",
      "top_center",
      "center",
    ] as const;
    for (const template of templates) {
      for (const position of positions) {
        const spec = resolveBrandingPreview({ ...base, template, position });
        assert.ok(spec.ariaLabel.includes(template));
        assert.ok(["flex-start", "center", "flex-end"].includes(spec.justify));
      }
    }
  });
  it("throws on unknown template/position (never silent)", () => {
    assert.throws(() => resolveBrandingPreview({ ...base, template: "nope" as never }));
    assert.throws(() => resolveBrandingPreview({ ...base, position: "nope" as never }));
  });
  it("endcard templates get blur backdrop + centered layout", () => {
    assert.equal(isEndcardTemplate("glass_end"), true);
    assert.equal(isEndcardTemplate("creator_outro"), true);
    assert.equal(isEndcardTemplate("minimal_corner"), false);
    assert.equal(isCornerTemplate("creator_tag"), true);
    const spec = resolveBrandingPreview({ ...base, template: "glass_end" });
    assert.equal(spec.hasBlurBg, true);
    assert.equal(spec.justify, "center");
    const corner = resolveBrandingPreview({ ...base, template: "minimal_corner", position: "top_left" });
    assert.equal(corner.hasBlurBg, false);
    assert.equal(corner.justify, "flex-start");
  });
  it("null channel falls back to initial ?", () => {
    const spec = resolveBrandingPreview({ ...base, channelName: "   " });
    assert.equal(spec.fallbackInitial, "?");
    assert.equal(spec.showName, false);
  });
  it("avatar_only hides name, name_only hides avatar", () => {
    const a = resolveBrandingPreview({ ...base, displayMode: "avatar_only" });
    assert.equal(a.showAvatar, true);
    assert.equal(a.showName, false);
    const n = resolveBrandingPreview({ ...base, displayMode: "name_only" });
    assert.equal(n.showAvatar, false);
    assert.equal(n.avatarPct, 0);
    assert.equal(n.showName, true);
  });
  it("clamps durations like the backend", () => {
    assert.equal(clampBrandDuration(2), 2);
    assert.equal(clampBrandDuration(0.1), 0.5);
    assert.equal(clampBrandDuration(99), 10);
    assert.equal(clampBrandDuration(Number.NaN), 2);
  });
  it("window labels describe the timing", () => {
    assert.equal(brandWindowLabel({ ...base, template: "glass_end", duration: 2 }), "2 dtk terakhir");
    assert.equal(
      brandWindowLabel({ ...base, template: "minimal_corner", timing: "full" }),
      "Sepanjang video",
    );
    assert.equal(
      brandWindowLabel({ ...base, template: "creator_tag", timing: "first", duration: 1.5 }),
      "1.5 dtk pertama",
    );
  });
});
describe("isProbablyYoutubeUrl", () => {
  it("accepts watch/shorts/live/be URLs", async () => {
    const { isProbablyYoutubeUrl } = await import("./branding.ts");
    assert.equal(isProbablyYoutubeUrl("https://www.youtube.com/watch?v=abc123XYZ_-"), true);
    assert.equal(isProbablyYoutubeUrl("https://youtu.be/abc123"), true);
    assert.equal(isProbablyYoutubeUrl("https://m.youtube.com/watch?v=abc"), true);
    assert.equal(isProbablyYoutubeUrl("https://www.youtube.com/shorts/abc"), true);
  });
  it("rejects non-youtube and empty input", async () => {
    const { isProbablyYoutubeUrl } = await import("./branding.ts");
    assert.equal(isProbablyYoutubeUrl(""), false);
    assert.equal(isProbablyYoutubeUrl("not a url"), false);
    assert.equal(isProbablyYoutubeUrl("https://vimeo.com/123"), false);
    assert.equal(isProbablyYoutubeUrl("https://www.youtube.com/channel/UCxx"), false);
  });
});
