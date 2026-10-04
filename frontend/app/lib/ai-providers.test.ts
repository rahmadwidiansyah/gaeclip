import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { accordionOpenId, cooldownText, healthBadge, moveId, nextPriority, timeAgo, toggleOpenId } from "./ai-providers.ts";

describe("provider health badges", () => {
  it("maps states to labels", () => {
    assert.equal(healthBadge("HEALTHY").label, "Healthy");
    assert.equal(healthBadge("RATE_LIMITED").label, "Rate Limited");
    assert.equal(healthBadge("INVALID").label, "Invalid Key");
    assert.equal(healthBadge(undefined).label, "Unknown");
  });

  it("cooldown text only while cooling", () => {
    assert.equal(cooldownText(0), null);
    assert.equal(cooldownText(Date.now() / 1000 - 5), null);
    assert.match(cooldownText(Date.now() / 1000 + 20) ?? "", /Cooldown: \d+s/);
  });
});

describe("open-card state", () => {
  it("toggleOpenId adds and removes by stable id", () => {
    assert.deepEqual(toggleOpenId([], "p2"), ["p2"]);
    assert.deepEqual(toggleOpenId(["p2"], "p2"), []);
    assert.deepEqual(toggleOpenId(["p1"], "p2"), ["p1", "p2"]);
  });

  it("accordionOpenId keeps at most one open (null = all collapsed)", () => {
    assert.equal(accordionOpenId(null, "p2"), "p2");
    assert.equal(accordionOpenId("p2", "p2"), null);
    assert.equal(accordionOpenId("p1", "p2"), "p2");
  });
});

describe("relative time", () => {
  it("timeAgo formats short Indonesian durations", () => {
    const now = 1_700_000_000_000;
    assert.equal(timeAgo(now / 1000 - 5, now), "5 dtk lalu");
    assert.equal(timeAgo(now / 1000 - 120, now), "2m lalu");
    assert.equal(timeAgo(now / 1000 - 7200, now), "2 jam lalu");
    assert.equal(timeAgo(now / 1000 - 90000, now), "1 hari lalu");
    assert.equal(timeAgo(null), null);
    assert.equal(timeAgo(0), null);
  });
});
describe("provider ordering helpers", () => {
  it("next priority continues the max", () => {
    assert.equal(nextPriority([]), 1);
    assert.equal(nextPriority([{ priority: 2 } as never, { priority: 5 } as never]), 6);
  });

  it("moveId swaps in bounds only", () => {
    assert.deepEqual(moveId(["a", "b", "c"], 1, -1), ["b", "a", "c"]);
    assert.deepEqual(moveId(["a", "b", "c"], 0, -1), ["a", "b", "c"]);
    assert.deepEqual(moveId(["a", "b", "c"], 2, 1), ["a", "b", "c"]);
  });
});
