import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  channelBadge,
  filterModels,
  pricingBadge,
  selectableModels,
  sortModels,
  versionTuple,
} from "./ai-models.ts";
import type { AiModelInfo } from "./types.ts";

function m(id: string, over: Partial<AiModelInfo> = {}): AiModelInfo {
  return {
    id,
    display_name: id,
    provider: "gemini",
    available: true,
    supports_generate_content: true,
    family: "gemini",
    generation: "",
    variant: "flash",
    tier: "flash",
    release_channel: "stable",
    is_preview: false,
    is_experimental: false,
    is_latest_alias: false,
    category: "multimodal",
    free_tier: "free",
    free_tier_source: "local_policy",
    eligible_for_gaeclip: true,
    ...over,
  };
}

describe("version compare is not lexical", () => {
  it("3.10 > 3.9", () => {
    assert.deepEqual(versionTuple("3.10"), [3, 10]);
    const sorted = sortModels([m("gemini-3.9-flash", { generation: "3.9" }), m("gemini-3.10-flash", { generation: "3.10" })]);
    assert.equal(sorted[0].id, "gemini-3.10-flash");
  });
});

describe("dropdown sorting", () => {
  it("eligible first, then stable, then flash, then newer", () => {
    const models = [
      m("gemini-3.8-pro", { generation: "3.8", variant: "pro", tier: "pro" }),
      m("gemini-2.0-flash-preview", { generation: "2.0", release_channel: "preview", is_preview: true }),
      m("text-embedding-004", { eligible_for_gaeclip: false, category: "embedding", variant: "other", tier: "other" }),
      m("gemini-3.7-flash", { generation: "3.7" }),
    ];
    assert.deepEqual(
      sortModels(models).map((x) => x.id),
      ["gemini-3.7-flash", "gemini-3.8-pro", "gemini-2.0-flash-preview", "text-embedding-004"]
    );
  });
});

describe("selectable gating", () => {
  const models = [
    m("gemini-3.8-flash"),
    m("gemini-3.8-pro", { variant: "pro", tier: "pro", free_tier: "paid" }),
    m("mystery", { free_tier: "unknown" }),
    m("gone", { available: false }),
  ];
  it("free-tier-only excludes paid/unknown/unavailable", () => {
    assert.deepEqual(selectableModels(models, true).map((x) => x.id), ["gemini-3.8-flash"]);
  });
  it("open mode keeps paid/unknown but not unavailable", () => {
    assert.deepEqual(selectableModels(models, false).map((x) => x.id), [
      "gemini-3.8-flash",
      "mystery",
      "gemini-3.8-pro",
    ]);
  });
});

describe("list filtering (UX only)", () => {
  const models = [
    m("gemini-3.8-flash", { generation: "3.8" }),
    m("gemini-3.8-pro", { generation: "3.8", variant: "pro", tier: "pro", free_tier: "paid" }),
    m("gemini-2.0-flash-lite", { generation: "2.0", variant: "flash-lite", tier: "flash-lite" }),
  ];
  it("search matches id and display name", () => {
    assert.equal(filterModels(models, "lite", "all").length, 1);
    assert.equal(filterModels(models, "gemini-3.8", "all").length, 2);
  });
  it("facets narrow the list", () => {
    assert.equal(filterModels(models, "", "free").length, 2);
    assert.equal(filterModels(models, "", "pro").length, 1);
    assert.equal(filterModels(models, "", "lite").length, 1);
    assert.equal(filterModels(models, "", "flash").length, 1);
  });
});

describe("badges", () => {
  it("channel + pricing labels", () => {
    assert.equal(channelBadge("stable"), "STABLE");
    assert.equal(channelBadge("preview"), "PREVIEW");
    assert.equal(channelBadge("experimental"), "EXPERIMENTAL");
    assert.equal(channelBadge("latest_alias"), "LATEST");
    assert.equal(channelBadge("unknown"), "UNKNOWN");
    assert.equal(pricingBadge("free"), "FREE");
    assert.equal(pricingBadge("paid"), "PAID");
    assert.equal(pricingBadge("unknown"), "PRICING UNKNOWN");
  });
});
