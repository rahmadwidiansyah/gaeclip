# 6. Testing dan Quality Assurance

## Test pyramid

### Unit tests

Test URL normalization, limits, schema validation, line normalization, timestamp
clamp/merge, candidate validation, crop geometry, hysteresis state machine,
progress aggregation, idempotency, path safety, and cleanup selection.

### Contract tests

Validate API request/response/error schemas against OpenAPI. Test every documented
status code, invalid job ID, not-ready results, cancellation, duplicate
idempotency key, and malformed request.

### Integration tests

Use fake yt-dlp, fake LLM, fake Whisper, and temporary storage. Test complete
pipeline without network/provider. Redis integration runs in a disposable
container or configured local service.

### Media tests

Keep small, legally usable fixtures: single speaker, two speakers, silence,
overlap, portrait source, landscape source, missing audio, subtitle gap, and
non-ASCII text. Assert ffprobe streams, dimensions, duration, codec, audio sync,
subtitle safe area, and output decodability.

### Provider tests

Use recorded/sanitized fixtures or mock clients; never call paid providers in
default test suite. Contract test model response schema and provider error mapping.

## Hysteresis table tests

Required vectors:

1. one speaker stable -> same target;
2. guest active 0.5s -> no switch;
3. guest active exactly below 2.0s -> no switch;
4. guest active >= 2.0s -> switch once;
5. guest disappears before confirmation -> pending reset;
6. host and guest overlap -> split only after split confirmation;
7. no face -> hold then wide;
8. confidence falls below exit threshold -> fallback;
9. rapid alternating detections -> no camera chatter;
10. clip start/end -> deterministic initial/final layout.

## LLM adversarial tests

Cover invalid JSON, unknown IDs, duplicate IDs, negative/NaN values, giant title,
zero-length clip, overlapping clips, prompt injection in transcript text, empty
response, provider timeout, rate limit, and hallucinated language.

## Quality metrics

Track transcript coverage and timestamp validity; candidate validity rate; clip
render success; audio/video sync; face detection recall on labeled sample;
active-speaker precision/recall/F1; unnecessary camera switches per minute; switch
latency; subtitle overflow rate; median/p95 processing time; peak memory; and cost
per completed job.

## CI and release gates

Every pull request runs formatting, lint, type checking, unit tests, schema/contract
tests, and a small FFmpeg smoke test. Nightly/local hardware runs integration,
GPU, load, and media regression suites. A release cannot advance if any P0 test
fails or if artifact safety/limits regress.

## Benchmark protocol

Maintain a fixed, licensed evaluation set with source metadata, expected speaker
intervals, face annotations where possible, and expected crop/layout properties.
Version dataset and metrics. Do not optimize only against one podcast or one
accent/language.
