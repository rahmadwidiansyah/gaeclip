import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  ALL,
  NO_CHANNEL,
  distinctChannels,
  filterClips,
  flattenClips,
  sortClips,
} from "./clips.ts";
import type { Job } from "./types.ts";

function job(id: string, channel: string | null, clips: { clip_id: string; title?: string; score?: number; duration_seconds?: number | null }[], updated = 100): Job {
  return {
    job_id: id,
    source_url: `https://youtu.be/${id}`,
    language: "id",
    video: channel
      ? { youtube_video_id: id, url: "", channel_name: channel, title: `Video ${id}` }
      : { youtube_video_id: id, url: "", channel_name: null },
    status: "completed",
    stage: "completed",
    progress: 100,
    clips: clips.map((c) => ({ ...c, size_bytes: 1000, download_url: `/dl/${c.clip_id}` })),
    created_at: updated,
    updated_at: updated,
  } as Job;
}

const jobs = [
  job("job1", "Podcast X", [
    { clip_id: "c1", title: "Dokter bicara sehat", score: 0.9, duration_seconds: 120 },
    { clip_id: "c2", title: "Resep harian", score: 0.5, duration_seconds: 40 },
  ], 300),
  job("job2", "Podcast X", [{ clip_id: "c3", title: "Olahraga pagi", score: 0.7, duration_seconds: 65 }], 200),
  job("job3", "Ngaji Yuk", [{ clip_id: "c4", title: "Kajian malam", score: 0.8, duration_seconds: 200 }], 100),
  job("job4", null, [{ clip_id: "c5", title: "Tanpa meta", duration_seconds: null }], 50),
];

describe("flattenClips resolves channel through the job relation", () => {
  it("carries channel + job id per clip", () => {
    const rows = flattenClips(jobs);
    assert.equal(rows.length, 5);
    assert.equal(rows.find((r) => r.clip_id === "c1")?.channel, "Podcast X");
    assert.equal(rows.find((r) => r.clip_id === "c5")?.channel, null);
  });
});

describe("distinctChannels is dynamic and deduped", () => {
  it("one entry per channel with clip counts", () => {
    const ch = distinctChannels(flattenClips(jobs));
    assert.deepEqual(
      ch.map((c) => [c.label, c.count]),
      [["Podcast X", 3], ["Ngaji Yuk", 1], ["Tanpa channel", 1]]
    );
  });
});

describe("filterClips combines channel + search + job with AND", () => {
  const rows = flattenClips(jobs);
  it("channel alone", () => {
    assert.equal(filterClips(rows, { q: "", channel: "Podcast X", jobId: ALL }).length, 3);
  });
  it("channel + search intersect (not union)", () => {
    const out = filterClips(rows, { q: "dokter", channel: "Podcast X", jobId: ALL });
    assert.deepEqual(out.map((r) => r.clip_id), ["c1"]);
  });
  it("search matching another channel is excluded by channel filter", () => {
    assert.equal(filterClips(rows, { q: "kajian", channel: "Podcast X", jobId: ALL }).length, 0);
  });
  it("job + channel intersect", () => {
    const out = filterClips(rows, { q: "", channel: "Podcast X", jobId: "job2" });
    assert.deepEqual(out.map((r) => r.clip_id), ["c3"]);
  });
  it("unknown-channel bucket", () => {
    assert.deepEqual(
      filterClips(rows, { q: "", channel: NO_CHANNEL, jobId: ALL }).map((r) => r.clip_id),
      ["c5"]
    );
  });
  it("search covers title, id, and channel", () => {
    assert.equal(filterClips(rows, { q: "c4", channel: ALL, jobId: ALL }).length, 1);
    assert.equal(filterClips(rows, { q: "ngaji", channel: ALL, jobId: ALL }).length, 1);
  });
});

describe("sortClips implements every offered mode", () => {
  const rows = flattenClips(jobs);
  it("new/old", () => {
    assert.deepEqual(sortClips(rows, "new").map((r) => r.clip_id), ["c1", "c2", "c3", "c4", "c5"]);
    assert.deepEqual(sortClips(rows, "old").map((r) => r.clip_id), ["c5", "c4", "c3", "c1", "c2"]);
  });
  it("score desc, duration desc/asc (nulls last)", () => {
    assert.equal(sortClips(rows, "score")[0].clip_id, "c1");
    assert.equal(sortClips(rows, "duration_desc")[0].clip_id, "c4");
    const asc = sortClips(rows, "duration_asc").map((r) => r.clip_id);
    assert.deepEqual(asc, ["c2", "c3", "c1", "c4", "c5"]);
  });
});
