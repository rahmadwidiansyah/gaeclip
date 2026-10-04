"""Benchmark curation: runtime, memory, validity on representative transcripts.

Synthetic but representative: Indonesian podcast style with hooks,
questions, numbers, fillers, subtitle repetition; clean + noisy variants;
EN + mixed. Run BEFORE changes (baseline) and AFTER, then compare.
Usage: .venv/bin/python benchmarks/curation_bench.py [--out PATH]
"""
import json
import sys
import time
import tracemalloc

sys.path.insert(0, "src")

from gaeclip.curation import curate_with_fallback, deterministic_candidates
from gaeclip.heuristic import heuristic_candidates
from gaeclip.schemas import TranscriptLine


def make_lines(specs, language="id"):
    return [TranscriptLine(line_id=f"l{i + 1:06d}", start=s, end=e, text=t,
                           language=language, source="faster_whisper")
            for i, (s, e, t) in enumerate(specs)]


HOOKS = [
    "Ternyata harganya 300 juta! Kenapa bisa semahal itu?",
    "Rahasia yang tidak pernah diceritakan siapapun akhirnya terungkap hari ini.",
    "Jangan sampai kalian melakukan kesalahan fatal ini, berhenti sekarang juga.",
    "Fakta mengejutkan terungkap: 500 unit terjual dalam 2 hari saja.",
    "Bagaimana cara mereka menghasilkan 2 miliar rupiah dalam seminggu?",
]
FILLER = "Eh anu ya kan gitu dong nah terus habis itu kayak kaya gitu loh."
NOISY = "saya makan ini saya makan ini saya makan ini cuma 3 kali, jadi menurut saya"


def build_transcript(n_lines, seed=0, noisy=False, lang="id", line_secs=10):
    import random
    rng = random.Random(seed)
    specs = []
    for i in range(n_lines):
        r = rng.random()
        if r < 0.06:
            text = rng.choice(HOOKS)
        elif r < 0.12:
            text = f"Kenapa hal ini bisa terjadi? Menurut kalian gimana?"
        elif r < 0.20:
            text = f"Data menunjukkan {rng.randint(2, 900)} juta rupiah dalam {rng.randint(1, 12)} bulan."
        elif noisy and r < 0.35:
            text = NOISY
        elif r < 0.45:
            text = FILLER
        else:
            text = (f"Kemudian kami membahas perkembangan pembangunan wilayah tersebut "
                    f"secara bertahap dan berkelanjutan bagian {i}.")
        if lang == "en" and r < 0.3:
            text = "So what happens next will shock you, this is the ultimate truth."
        elif lang == "mixed" and r < 0.3:
            text = "Ternyata the truth ispyx shocking banget, never seen before."
        s = i * line_secs
        specs.append((s, s + line_secs, text))
    return make_lines(specs, language="id" if lang != "en" else "en")


class FailingCurator:
    provider_name = "gemini"

    def curate(self, transcript):
        raise TimeoutError("timed out")


def validity(cands, min_d, max_d, max_clips):
    problems = []
    if len(cands) > max_clips:
        problems.append("too_many")
    for c in cands:
        d = c.end - c.start
        if not (min_d - 1e-6 <= d <= max_d + 1e-6):
            problems.append(f"bad_duration:{c.candidate_id}")
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            if a.start < b.end and b.start < a.end:
                problems.append(f"overlap:{a.candidate_id}/{b.candidate_id}")
    dangling_start = sum(1 for c in cands if c.title[:1].islower() or c.title.startswith(("karena", "dan ", "tapi ")))
    return problems, dangling_start


def bench_case(name, lines, min_d=30, max_d=60, max_clips=3):
    out = {"case": name, "lines": len(lines)}
    tracemalloc.start()
    t0 = time.perf_counter()
    sel, breakdown, best = heuristic_candidates(lines, min_d, max_d, max_clips)
    t1 = time.perf_counter()
    det = deterministic_candidates(lines, min_d, max_d, max_clips)
    t2 = time.perf_counter()
    res = curate_with_fallback(lines, FailingCurator(), min_d, max_d, max_clips)
    t3 = time.perf_counter()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    out.update({
        "heuristic_ms": round((t1 - t0) * 1000, 1),
        "deterministic_ms": round((t2 - t1) * 1000, 1),
        "fallback_ms": round((t3 - t2) * 1000, 1),
        "peak_kb": round(peak / 1024, 1),
        "heuristic_n": len(sel), "heuristic_best": round(best, 4),
        "deterministic_n": len(det),
        "fallback_method": res.curation_method,
        "fallback_n": len(res.candidates),
    })
    probs, dangling = validity(res.candidates, min_d, max_d, max_clips)
    out["fallback_problems"] = probs
    out["fallback_dangling_starts"] = dangling
    hprobs, _ = validity(sel, min_d, max_d, max_clips)
    out["heuristic_problems"] = hprobs
    return out


def main():
    cases = [
        ("short-id", build_transcript(36)),
        ("medium-30min-id", build_transcript(180)),
        ("long-1h-id", build_transcript(360)),
        ("xlong-2h-id", build_transcript(720)),
        ("xxlong-3h-id", build_transcript(1080)),
        ("noisy-30min", build_transcript(180, noisy=True)),
        ("english-30min", build_transcript(180, lang="en")),
        ("mixed-30min", build_transcript(180, lang="mixed")),
    ]
    results = [bench_case(name, lines) for name, lines in cases]
    out_path = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    print(json.dumps(results, indent=1, ensure_ascii=False))
    if out_path:
        with open(out_path, "w") as fh:
            json.dump(results, fh, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
