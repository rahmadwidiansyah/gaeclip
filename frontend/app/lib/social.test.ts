import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { aggregatePublishStatus, determinateProgress, failureKind, formatHashtagsInput, initialCaption, initialPublishValues, isActivePublishStatus, isTerminalPublishStatus, localTimeZone, normalizeHashtag, parseHashtagsInput, phaseMessage, publishSummary, statusLabel, statusTone, summarizeJobs, timeZoneList, zonedLocalToEpoch } from "./social.ts";

describe("statusLabel/statusTone", () => {
  it("labels every lifecycle state with text (never color alone)", () => {
    for (const s of ["connected", "needs_reconnect", "disconnected", "pending", "queued", "preparing", "uploading", "uploaded", "processing", "reconciling", "published", "draft", "scheduled", "failed", "cancelled"]) {
      assert.match(statusLabel(s), /\S/);
      assert.match(statusTone(s), /\S/);
    }
  });
  it("never returns bare Progress", () => {
    for (const s of ["pending", "uploading", "uploaded", "processing", "reconciling", "queued", "preparing"]) {
      assert.notEqual(statusLabel(s).toLowerCase(), "progress");
    }
  });
  it("falls back to raw status text", () => {
    assert.equal(statusLabel("weird"), "weird");
  });
});

describe("isTerminalPublishStatus", () => {
  it("published/draft/failed/cancelled are terminal", () => {
    for (const s of ["published", "draft", "failed", "cancelled"]) assert.equal(isTerminalPublishStatus(s), true);
  });
  it("active states are not terminal", () => {
    for (const s of ["pending", "queued", "preparing", "uploading", "uploaded", "processing", "reconciling", "scheduled"]) assert.equal(isTerminalPublishStatus(s), false);
  });
  it("active helper covers polling states", () => {
    for (const s of ["pending", "uploading", "uploaded", "processing", "reconciling", "scheduled"]) assert.equal(isActivePublishStatus(s), true);
    for (const s of ["published", "failed", "cancelled"]) assert.equal(isActivePublishStatus(s), false);
  });
});

describe("initialCaption", () => {
  it("title + hashtags", () => {
    assert.equal(initialCaption("Ternyata Bukan Karena Itu", ["shorts", "indonesia"]), "Ternyata Bukan Karena Itu\n#shorts #indonesia");
  });
  it("title only without hashtags", () => {
    assert.equal(initialCaption("Hook Saja", []), "Hook Saja");
  });
  it("cleans tag input", () => {
    assert.equal(initialCaption("T", ["#a b", "  c  "]), "T\n#ab #c");
  });
});

describe("publishSummary", () => {
  it("counts published over total (3/4 case)", () => {
    assert.equal(
      publishSummary([{ status: "published" }, { status: "published" }, { status: "failed" }, { status: "published" }]),
      "3/4 platforms published",
    );
  });
});

describe("normalizeHashtag/parseHashtagsInput", () => {
  it("normalizes to #word", () => {
    assert.equal(normalizeHashtag("#Shorts"), "shorts");
    assert.equal(normalizeHashtag("  #a b "), "ab");
    assert.equal(normalizeHashtag("!!!"), "");
  });
  it("parses input, dedupes, caps at 8", () => {
    assert.deepEqual(parseHashtagsInput("#shorts, shorts #SHORTS ok"), ["#shorts", "#ok"]);
    const many = Array.from({ length: 12 }, (_, i) => `t${i}`).join(" ");
    assert.equal(parseHashtagsInput(many).length, 8);
  });
  it("formats tokens for input", () => {
    assert.equal(formatHashtagsInput(["#a", "#b"]), "#a #b");
  });
});

describe("zonedLocalToEpoch", () => {
  it("converts wall time in Asia/Jakarta (UTC+7, no DST)", () => {
    // 2026-10-06 10:30 WIB == 03:30 UTC
    assert.equal(zonedLocalToEpoch("2026-10-06", "10:30", "Asia/Jakarta"), Date.UTC(2026, 9, 6, 3, 30) / 1000);
  });
  it("UTC is identity", () => {
    assert.equal(zonedLocalToEpoch("2026-01-01", "00:00", "UTC"), Date.UTC(2026, 0, 1, 0, 0) / 1000);
  });
  it("rejects malformed input and unknown zones", () => {
    assert.equal(zonedLocalToEpoch("not-a-date", "10:30", "UTC"), null);
    assert.equal(zonedLocalToEpoch("2026-10-06", "xx", "UTC"), null);
    assert.equal(zonedLocalToEpoch("2026-10-06", "10:30", "Mars/Olympus"), null);
  });
});

describe("timeZoneList", () => {
  it("returns a non-empty list containing UTC", () => {
    const list = timeZoneList();
    assert.ok(list.length > 0);
    assert.ok(list.includes("UTC"));
  });
  it("localTimeZone falls back to a non-empty string", () => {
    assert.ok(typeof localTimeZone() === "string" && localTimeZone().length > 0);
  });
});

describe("aggregatePublishStatus/global", () => {
  it("single success -> completed", () => {
    assert.equal(aggregatePublishStatus([{ status: "published" }]), "completed");
  });
  it("single failure -> failed", () => {
    assert.equal(aggregatePublishStatus([{ status: "failed" }]), "failed");
  });
  it("2 platforms mixed -> partial_success", () => {
    assert.equal(aggregatePublishStatus([{ status: "published" }, { status: "failed" }]), "partial_success");
  });
  it("3 platforms with processing stays publishing", () => {
    assert.equal(aggregatePublishStatus([{ status: "published" }, { status: "processing" }, { status: "failed" }]), "publishing");
  });
  it("4 platforms mixed active stays publishing", () => {
    assert.equal(aggregatePublishStatus([{ status: "published" }, { status: "processing" }, { status: "published" }, { status: "failed" }]), "publishing");
  });
  it("all processing -> processing", () => {
    assert.equal(aggregatePublishStatus([{ status: "processing" }, { status: "processing" }]), "processing");
  });
  it("all failed -> failed, all success -> completed", () => {
    assert.equal(aggregatePublishStatus([{ status: "failed" }, { status: "failed" }]), "failed");
    assert.equal(aggregatePublishStatus([{ status: "published" }, { status: "draft" }]), "completed");
  });
  it("summarizeJobs counts honestly (no averaged %)", () => {
    const s = summarizeJobs([{ status: "published" }, { status: "processing" }, { status: "failed" }]);
    assert.equal(s.total, 3);
    assert.equal(s.completed, 1);
    assert.equal(s.processing, 1);
    assert.equal(s.failed, 1);
  });
});

describe("phaseMessage/determinateProgress", () => {
  const base = { id: "1", clip_id: "c", job_id: "j", provider: "youtube", social_account_id: "a", title: "", caption: "", hashtags: [], privacy: "public", scheduled_at: null, started_at: null, completed_at: null, attempts: 0, error_code: "", error_message: "", remote_media_id: "", remote_url: "", created_at: 0, updated_at: 0 } as const;
  it("uploading 72% shows percentage", () => {
    assert.match(phaseMessage({ ...base, status: "uploading", progress: 0.72 }), /72%/);
  });
  it("processing shows provider message, not Uploading", () => {
    const msg = phaseMessage({ ...base, status: "processing" });
    assert.match(msg, /processing/i);
    assert.doesNotMatch(msg.toLowerCase(), /^uploading/);
  });
  it("uploaded distinct from uploading", () => {
    assert.match(phaseMessage({ ...base, status: "uploaded" }), /upload complete/i);
  });
  it("reconciling shows checking", () => {
    assert.match(phaseMessage({ ...base, status: "reconciling" }), /checking/i);
  });
  it("determinate only for real uploading fraction", () => {
    assert.equal(determinateProgress({ ...base, status: "uploading", progress: 0.5 }), 50);
    assert.equal(determinateProgress({ ...base, status: "processing", progress: 0.5 }), null);
    assert.equal(determinateProgress({ ...base, status: "uploading", progress: null }), null);
  });
  it("failureKind maps codes", () => {
    assert.equal(failureKind("auth_failed"), "auth");
    assert.equal(failureKind("processing_failed"), "processing");
  });
});
describe("initialPublishValues", () => {
  it("prefills stored llm metadata without any fetch", () => {
    const v = initialPublishValues({
      description: "Cara membuat rendang empuk.",
      hashtags: ["#Rendang", "#masakan", "#RENDANG"],
      metadata_source: "llm",
    });
    assert.equal(v.caption, "Cara membuat rendang empuk.");
    assert.equal(v.hashtags, "#rendang #masakan");
    assert.equal(v.aiSource, "gemini");
  });
  it("maps deterministic_fallback source through", () => {
    const v = initialPublishValues({
      description: "D.",
      hashtags: [],
      metadata_source: "deterministic_fallback",
    });
    assert.equal(v.aiSource, "deterministic_fallback");
    assert.equal(v.hashtags, "");
  });
  it("legacy clips without metadata start blank (manual flow unchanged)", () => {
    for (const stored of [null, undefined, {}, { description: null, hashtags: null, metadata_source: null }]) {
      const v = initialPublishValues(stored);
      assert.equal(v.caption, "");
      assert.equal(v.hashtags, "");
      assert.equal(v.aiSource, null);
    }
  });
  it("unknown sources never claim AI origin", () => {
    const v = initialPublishValues({ description: "D", hashtags: [], metadata_source: "failed" });
    assert.equal(v.aiSource, null);
    assert.equal(v.caption, "D");
  });
  it("ignores non-string hashtag entries", () => {
    const v = initialPublishValues({ description: "", hashtags: ["#ok", 42, null], metadata_source: null });
    assert.equal(v.hashtags, "#ok");
  });
});
