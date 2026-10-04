import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { formatClock, formatDuration, formatPublished } from "./format.ts";

describe("formatDuration (source, human-readable)", () => {
  const cases: Array<[unknown, string | null]> = [
    [42, "42 dtk"],
    [60, "1m 00dtk"],
    [65, "1m 05dtk"],
    [125, "2m 05dtk"],
    [3599, "59m 59dtk"],
    [3600, "1j"],
    [3661, "1j 01m 01dtk"],
    [3997, "1j 06m 37dtk"],
    [3997.0, "1j 06m 37dtk"],
    [3997.9, "1j 06m 37dtk"],
    [0, "0 dtk"],
    [null, null],
    [undefined, null],
    [NaN, null],
    [Infinity, null],
    [-5, null],
  ];
  for (const [input, expected] of cases) {
    it(`${String(input)} -> ${expected}`, () => {
      assert.equal(formatDuration(input as number), expected);
    });
  }
});

describe("formatClock (clip player MM:SS)", () => {
  const cases: Array<[unknown, string | null]> = [
    [42, "00:42"],
    [65, "01:05"],
    [125, "02:05"],
    [3599, "59:59"],
    [3661, "1:01:01"],
    [0, "00:00"],
    [null, null],
    [NaN, null],
    [-1, null],
  ];
  for (const [input, expected] of cases) {
    it(`${String(input)} -> ${expected}`, () => {
      assert.equal(formatClock(input as number), expected);
    });
  }
});

describe("formatPublished", () => {
  it("formats ISO date to id-ID", () => {
    assert.equal(formatPublished("2026-09-21"), "21 Sep 2026");
  });
  it("passes through unknown strings, dashes empty", () => {
    assert.equal(formatPublished(null), "—");
    assert.equal(formatPublished("not-a-date"), "not-a-date");
  });
});
