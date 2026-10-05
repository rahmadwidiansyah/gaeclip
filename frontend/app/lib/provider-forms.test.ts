import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  buildProviderEditPayload,
  isDuplicateModel,
  normalizeModelId,
  validateProviderName,
} from "./provider-forms.ts";

describe("provider edit payload (id stays stable, key preserved)", () => {
  it("renames without touching the secret when key field is blank", () => {
    const payload = buildProviderEditPayload({
      name: "Gemini Utama",
      apiKey: "",
      enabled: true,
      priority: 1,
    });
    assert.equal(payload.name, "Gemini Utama");
    assert.ok(!("api_key" in payload), "blank key field must not send api_key");
  });

  it("whitespace-only key field also preserves the secret", () => {
    const payload = buildProviderEditPayload({
      name: "fengki",
      apiKey: "   ",
      enabled: true,
      priority: 2,
    });
    assert.ok(!("api_key" in payload));
  });

  it("sends api_key only when the user typed a replacement", () => {
    const payload = buildProviderEditPayload({
      name: "fengki",
      apiKey: "AIza-new-secret",
      enabled: false,
      priority: 3,
    });
    assert.equal(payload.api_key, "AIza-new-secret");
    assert.equal(payload.enabled, false);
    assert.equal(payload.priority, 3);
  });

  it("trims the display name; id is never part of the payload", () => {
    const payload = buildProviderEditPayload({
      name: "  Gemini Utama  ",
      apiKey: "",
      enabled: true,
      priority: 1,
    });
    assert.equal(payload.name, "Gemini Utama");
    assert.ok(!("id" in payload));
  });

  it("clamps priority to >= 1", () => {
    assert.equal(buildProviderEditPayload({ name: "a", apiKey: "", enabled: true, priority: 0 }).priority, 1);
    assert.equal(buildProviderEditPayload({ name: "a", apiKey: "", enabled: true, priority: -5 }).priority, 1);
  });
});

describe("provider name validation", () => {
  it("rejects blank names", () => {
    assert.equal(validateProviderName(""), "Nama provider tidak boleh kosong");
    assert.equal(validateProviderName("   "), "Nama provider tidak boleh kosong");
  });

  it("accepts renames like fengki -> Gemini Utama", () => {
    assert.equal(validateProviderName("Gemini Utama"), null);
    assert.equal(validateProviderName("fengki"), null);
  });

  it("rejects overlong names", () => {
    assert.equal(validateProviderName("x".repeat(81)), "Nama provider maksimal 80 karakter");
    assert.equal(validateProviderName("x".repeat(80)), null);
  });
});

describe("duplicate model prevention", () => {
  it("detects exact duplicates after trimming", () => {
    assert.equal(isDuplicateModel(["a", "b"], "a"), true);
    assert.equal(isDuplicateModel(["a", "b"], "  a  "), true);
    assert.equal(isDuplicateModel(["a", "b"], "c"), false);
  });

  it("blank candidate is not a duplicate (button stays disabled instead)", () => {
    assert.equal(isDuplicateModel(["a"], ""), false);
    assert.equal(isDuplicateModel(["a"], "   "), false);
  });

  it("normalizeModelId trims user input", () => {
    assert.equal(normalizeModelId("  gemini-2.0-flash  "), "gemini-2.0-flash");
  });
});
