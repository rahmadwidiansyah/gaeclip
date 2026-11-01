import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { ICONS, resolveIcon } from "./icons.ts";

// Every short name referenced by <Icon name="..."> across the app.
const USED = [
  "dashboard", "jobs", "clips", "settings",
  "plus", "search", "download", "close", "check", "alert",
  "clock", "chevL", "chevR", "chevD", "chevU", "refresh", "trash",
  "edit",
  "sun", "moon", "monitor", "play", "link", "filter", "sort",
  "group", "calendar", "image", "copy",
  "star", "info", "circle", "loader", "upload",
  "youtube", "tiktok", "instagram", "facebook",
  "badge", "palette", "type", "eye",
];

describe("single icon system (Lucide)", () => {
  it("every name used in the app resolves to a Lucide component", () => {
    for (const name of USED) {
      assert.ok((ICONS as Record<string, unknown>)[name], `missing icon: ${name}`);
    }
  });
  it("all mapped values are renderable components", () => {
    for (const [name, Cmp] of Object.entries(ICONS)) {
      const t = typeof Cmp;
      assert.ok(t === "function" || t === "object", `${name} is ${t}`);
    }
  });
  it("unknown names fall back instead of crashing", () => {
    assert.ok(resolveIcon("does-not-exist"));
  });
});
