import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_PAGE_SIZE,
  clampPage,
  pageCount,
  pageRangeEnd,
  pageRangeStart,
  paginate,
} from "./pagination.ts";

describe("pagination math", () => {
  it("defaults to 20 items per page", () => {
    assert.equal(DEFAULT_PAGE_SIZE, 20);
  });

  it("61 models with page size 20 produces 4 pages", () => {
    assert.equal(pageCount(61, 20), 4);
  });

  it("61 models paginate to 20 / 20 / 20 / 1", () => {
    const all = Array.from({ length: 61 }, (_, i) => `model-${i + 1}`);
    assert.equal(paginate(all, 1, 20).length, 20);
    assert.equal(paginate(all, 2, 20).length, 20);
    assert.equal(paginate(all, 3, 20).length, 20);
    assert.deepEqual(paginate(all, 4, 20), ["model-61"]);
    assert.equal(paginate(all, 1, 20)[0], "model-1");
    assert.equal(paginate(all, 2, 20)[0], "model-21");
    assert.equal(paginate(all, 4, 20)[0], "model-61");
  });

  it("empty list still reports a single (empty) page", () => {
    assert.equal(pageCount(0, 20), 1);
    assert.deepEqual(paginate([], 1, 20), []);
  });

  it("clamps out-of-range pages", () => {
    assert.equal(clampPage(0, 4), 1);
    assert.equal(clampPage(-3, 4), 1);
    assert.equal(clampPage(1, 4), 1);
    assert.equal(clampPage(4, 4), 4);
    assert.equal(clampPage(9, 4), 4);
  });

  it("range labels describe the visible slice", () => {
    assert.equal(pageRangeStart(61, 1, 20), 1);
    assert.equal(pageRangeEnd(61, 1, 20), 20);
    assert.equal(pageRangeStart(61, 4, 20), 61);
    assert.equal(pageRangeEnd(61, 4, 20), 61);
    assert.equal(pageRangeStart(0, 1, 20), 0);
    assert.equal(pageRangeEnd(0, 1, 20), 0);
  });

  it("filtered lists paginate on the filtered total, not the raw total", () => {
    const all = Array.from({ length: 61 }, (_, i) => `gemini-flash-${i}`);
    const filtered = all.filter((id) => id.includes("flash-1"));
    // flash-1, flash-10..19  => 11 hits
    assert.equal(filtered.length, 11);
    assert.equal(pageCount(filtered.length, 20), 1);
    assert.equal(paginate(filtered, 1, 20).length, 11);
  });

  it("page-size selector changes page count", () => {
    assert.equal(pageCount(61, 10), 7);
    assert.equal(pageCount(61, 20), 4);
    assert.equal(pageCount(61, 50), 2);
  });
});
