import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  fallbackOrder,
  searchSettings,
  splitGeminiAdvanced,
  GEMINI_ADVANCED_KEYS,
} from "./settings-nav.ts";
import type { AppSetting } from "./types.ts";

const fields = [
  { key: "GEMINI_TIMEOUT_SECONDS", label: "Gemini Timeout", description: "Max wait per call", category: "AI & Models" },
  { key: "RENDER_PROFILE", label: "Render Quality", description: "Encode tier", category: "Processing" },
];

const catFor = (b: string) =>
  b === "AI & Models" ? ("ai" as const) : b === "Processing" ? ("video" as const) : ("advanced" as const);
const tabFor = (k: string) => (GEMINI_ADVANCED_KEYS.includes(k) ? ("advanced" as const) : undefined);

describe("settings search", () => {
  it("finds by title, description, and key", () => {
    const hits = searchSettings("timeout", fields, tabFor, catFor);
    assert.ok(hits.some((h) => h.title === "Gemini Timeout"));
    const fp = searchSettings("fps", fields, tabFor, catFor);
    assert.ok(fp.length >= 0);
  });

  it("maps gemini areas to tabs", () => {
    const hits = searchSettings("circuit", fields, tabFor, catFor);
    assert.ok(hits.some((h) => h.geminiTab === "advanced"));
  });

  it("short queries return nothing", () => {
    assert.deepEqual(searchSettings("x", fields, tabFor, catFor), []);
  });

  it("finds social areas by provider and intent keywords", () => {
    for (const q of ["youtube", "tiktok", "oauth", "credential", "publish", "account"]) {
      const hits = searchSettings(q, fields, tabFor, catFor);
      assert.ok(hits.some((h) => h.category === "social"), `no social hit for ${q}`);
    }
  });
});

describe("fallback order", () => {
  it("expands priority-ordered provider x model steps", () => {
    const steps = fallbackOrder([
      { id: "p2", name: "B", enabled: true, priority: 2, key_suffix: "", key_configured: true, models: [{ id: "m1", enabled: true, priority: 1 }], unavailable_models: [] },
      { id: "p1", name: "A", enabled: true, priority: 1, key_suffix: "", key_configured: true, models: [{ id: "m1", enabled: true, priority: 1 }, { id: "m2", enabled: true, priority: 2 }], unavailable_models: [] },
      { id: "p0", name: "Off", enabled: false, priority: 0, key_suffix: "", key_configured: false, models: [{ id: "m9", enabled: true, priority: 1 }], unavailable_models: [] },
    ]);
    assert.deepEqual(
      steps.map((s) => `${s.providerId}/${s.modelId}`),
      ["p1/m1", "p1/m2", "p2/m1"]
    );
    assert.deepEqual(steps.map((s) => s.step), [1, 2, 3]);
  });

  it("skips disabled and unavailable models", () => {
    const steps = fallbackOrder([
      { id: "p1", name: "A", enabled: true, priority: 1, key_suffix: "", key_configured: true, models: [{ id: "m1", enabled: false, priority: 1 }, { id: "m2", enabled: true, priority: 2 }], unavailable_models: ["m2"] },
    ]);
    assert.deepEqual(steps, []);
  });
});

describe("splitGeminiAdvanced", () => {
  it("separates knob keys", () => {
    const mk = (key: string): AppSetting => ({ key, label: key, description: "", category: "AI & Models" }) as AppSetting;
    const { advanced, rest } = splitGeminiAdvanced([mk("GEMINI_MAX_ATTEMPTS"), mk("GEMINI_MODEL")]);
    assert.deepEqual(advanced.map((f) => f.key), ["GEMINI_MAX_ATTEMPTS"]);
    assert.deepEqual(rest.map((f) => f.key), ["GEMINI_MODEL"]);
  });
});
