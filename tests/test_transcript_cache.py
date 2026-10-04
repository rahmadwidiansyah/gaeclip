"""Persistent Whisper transcript cache: keys, compatibility, locks, validation."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "tests")

from test_video_metadata import FakeRedis

from gaeclip.schemas import TranscriptLine
from gaeclip.transcript_cache import (
    TRANSCRIPT_SCHEMA_VERSION,
    TranscriptCache,
    source_key_for,
    transcription_config_hash,
    validate_transcript_payload,
)


def _line(i, start=None, end=None, text=None):
    return TranscriptLine(
        line_id=f"l{i:06d}",
        start=float(i * 10) if start is None else start,
        end=float(i * 10 + 9) if end is None else end,
        text=text or f"kalimat {i}",
        language="id",
        source="faster_whisper",
    )


def _cache(storage_root=None, client=None):
    if storage_root is None:
        storage_root = Path(tempfile.mkdtemp())
    return TranscriptCache(client or FakeRedis(), Path(storage_root))


URL_WATCH = "https://www.youtube.com/watch?v=ABC123"
URL_SHORT = "https://youtu.be/ABC123"
URL_TRACKED = "https://www.youtube.com/watch?v=ABC123&utm_source=share&t=10s"


class SourceKeyTests(unittest.TestCase):
    def test_all_url_forms_share_one_key(self):
        keys = {source_key_for("ABC123", url)[0] for url in ("x", "y", "z")}
        self.assertEqual(keys, {"youtube:ABC123"})

    def test_different_video_different_key(self):
        self.assertNotEqual(source_key_for("AAA", "u1")[0], source_key_for("BBB", "u2")[0])

    def test_unresolvable_url_falls_back_to_hash(self):
        key, kind = source_key_for(None, "https://www.youtube.com/watch?foo=bar")
        self.assertEqual(kind, "url")
        self.assertTrue(key.startswith("url:"))


class ConfigHashTests(unittest.TestCase):
    def test_same_config_same_hash(self):
        self.assertEqual(
            transcription_config_hash("small", "id"),
            transcription_config_hash("Small", "ID"),
        )

    def test_model_difference_misses(self):
        self.assertNotEqual(
            transcription_config_hash("small", "id"),
            transcription_config_hash("medium", "id"),
        )

    def test_language_difference_misses(self):
        self.assertNotEqual(
            transcription_config_hash("small", "id"),
            transcription_config_hash("small", "en"),
        )


class StoreLookupTests(unittest.TestCase):
    def test_store_then_hit_across_url_forms(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            lines = [_line(1), _line(2)]
            record = cache.store(URL_WATCH, "small", "id", lines, duration=200.0)
            self.assertIsNotNone(record)
            for url in (URL_WATCH, URL_SHORT, URL_TRACKED):
                with self.subTest(url=url):
                    found, rec, outcome = cache.lookup(url, "small", "id", duration=200.0)
                    self.assertEqual(outcome, "hit")
                    self.assertEqual(len(found), 2)
                    self.assertEqual(rec.cache_id, record.cache_id)
                    self.assertEqual(found[0].text, "kalimat 1")

    def test_different_video_misses(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            cache.store(URL_WATCH, "small", "id", [_line(1)])
            _, _, outcome = cache.lookup("https://youtu.be/OTHER999", "small", "id")
            self.assertEqual(outcome, "miss")

    def test_incompatible_model_and_language_miss(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            cache.store(URL_WATCH, "small", "id", [_line(1)])
            self.assertEqual(cache.lookup(URL_SHORT, "medium", "id")[2], "miss")
            self.assertEqual(cache.lookup(URL_SHORT, "small", "en")[2], "miss")

    def test_duration_drift_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            cache.store(URL_WATCH, "small", "id", [_line(1)], duration=200.0)
            _, _, outcome = cache.lookup(URL_SHORT, "small", "id", duration=5000.0)
            self.assertEqual(outcome, "invalid")
            # Index dropped: next lookup is a plain miss, not invalid.
            self.assertEqual(cache.lookup(URL_SHORT, "small", "id")[2], "miss")

    def test_missing_artifact_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            record = cache.store(URL_WATCH, "small", "id", [_line(1)])
            (Path(directory) / record.transcript_path).unlink()
            _, _, outcome = cache.lookup(URL_SHORT, "small", "id")
            self.assertEqual(outcome, "invalid")

    def test_corrupt_artifact_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            record = cache.store(URL_WATCH, "small", "id", [_line(1)])
            (Path(directory) / record.transcript_path).write_text("{not json", encoding="utf-8")
            _, _, outcome = cache.lookup(URL_SHORT, "small", "id")
            self.assertEqual(outcome, "invalid")

    def test_empty_transcript_never_stored(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            self.assertIsNone(cache.store(URL_WATCH, "small", "id", []))
            self.assertEqual(cache.lookup(URL_SHORT, "small", "id")[2], "miss")

    def test_old_schema_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            record = cache.store(URL_WATCH, "small", "id", [_line(1)])
            path = Path(directory) / record.transcript_path
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = TRANSCRIPT_SCHEMA_VERSION + 99
            path.write_text(json.dumps(payload), encoding="utf-8")
            _, _, outcome = cache.lookup(URL_SHORT, "small", "id")
            self.assertEqual(outcome, "invalid")

    def test_db_error_degrades_to_miss(self):
        class BrokenRedis(FakeRedis):
            def get(self, key):
                raise ConnectionError("redis down")

        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory, client=BrokenRedis())
            lines, record, outcome = cache.lookup(URL_WATCH, "small", "id")
            self.assertEqual(outcome, "miss")
            self.assertIsNone(lines)
            self.assertIsNone(record)

    def test_store_failure_never_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            with patch.object(Path, "mkdir", side_effect=OSError("disk full")):
                self.assertIsNone(cache.store(URL_WATCH, "small", "id", [_line(1)]))


class PayloadValidationTests(unittest.TestCase):
    def test_valid_payload(self):
        payload = {
            "schema_version": TRANSCRIPT_SCHEMA_VERSION,
            "lines": [_line(1).model_dump(mode="json"), _line(2).model_dump(mode="json")],
        }
        self.assertEqual(len(validate_transcript_payload(payload)), 2)

    def test_rejects_garbage(self):
        for bad in (None, [], {}, {"schema_version": TRANSCRIPT_SCHEMA_VERSION},
                    {"schema_version": 999, "lines": []},
                    {"schema_version": TRANSCRIPT_SCHEMA_VERSION, "lines": []}):
            self.assertIsNone(validate_transcript_payload(bad))


class LockTests(unittest.TestCase):
    def test_acquire_release_owner_semantics(self):
        redis = FakeRedis()
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory, client=redis)
            self.assertTrue(cache.acquire_lock("youtube:A", "cfg", "job-1"))
            self.assertFalse(cache.acquire_lock("youtube:A", "cfg", "job-2"))
            cache.release_lock("youtube:A", "cfg", "job-2")  # stranger cannot release
            self.assertFalse(cache.acquire_lock("youtube:A", "cfg", "job-2"))
            cache.release_lock("youtube:A", "cfg", "job-1")
            self.assertTrue(cache.acquire_lock("youtube:A", "cfg", "job-2"))

    def test_wait_returns_hit_when_peer_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            cache.store(URL_WATCH, "small", "id", [_line(1)])
            lines, record = cache.wait_for_transcript(URL_WATCH, "small", "id", timeout_seconds=5)
            self.assertEqual(len(lines), 1)
            self.assertIsNotNone(record)

    def test_wait_timeout_returns_empty(self):
        import gaeclip.transcript_cache as cache_mod

        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            with patch.object(cache_mod, "TRANSCRIPT_WAIT_POLL_SECONDS", 0.01):
                lines, record = cache.wait_for_transcript(
                    URL_WATCH, "small", "id", timeout_seconds=0.05)
            self.assertIsNone(lines)
            self.assertIsNone(record)


class AdoptTests(unittest.TestCase):
    def _write_job_transcript(self, directory, job_id, engine, model="small", lang="id"):
        path = Path(directory) / "jobs" / job_id / "source" / "transcript.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": 1,
            "engine": engine,
            "whisper_model": model,
            "language": lang,
            "lines": [_line(1).model_dump(mode="json")],
        }), encoding="utf-8")
        return path

    def test_adopt_registers_historical_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            path = self._write_job_transcript(directory, "job_old", "faster_whisper")
            lines, record = cache.adopt_job_transcript(path, URL_WATCH, "small", "id")
            self.assertEqual(len(lines), 1)
            self.assertIsNotNone(record)
            # ...and the next lookup hits.
            found, _, outcome = cache.lookup(URL_SHORT, "small", "id")
            self.assertEqual(outcome, "hit")
            self.assertEqual(len(found), 1)

    def test_adopt_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            lines, record = cache.adopt_job_transcript(
                Path(directory) / "nope.json", URL_WATCH, "small", "id")
            self.assertIsNone(lines)
            self.assertIsNone(record)

    def test_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = _cache(directory)
            cache.store(URL_WATCH, "small", "id", [_line(1), _line(2)])
            stats = cache.stats()
            self.assertEqual(stats["cached_transcripts"], 1)
            self.assertEqual(stats["successful"], 1)
            self.assertEqual(stats["total_lines"], 2)
            self.assertGreater(stats["storage_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
