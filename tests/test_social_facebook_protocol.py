"""Facebook upload-protocol audit tests (no network, no credentials).

Fake Meta server scenario coverage (§11 of the audit):

1. start -> upload session; chunks -> offsets; final chunk -> valid handle;
   finish accepts that exact handle (session id is NEVER substituted,
   Page token is used, title/description correct, graph-video host).
2. final chunk with NO handle -> finalization is NOT called, job fails
   with facebook_upload_handle_missing, UI never sees "uploaded".
3. finish HTTP 500 code=1 -> unknown_provider_error, fail fast, exactly
   one finish call (no blind retry).
4. extract_upload_handle shapes + safe schema + log-secret regression.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, "tests")

FAKE_SESSION = "upload:FAKESESSION999"
FAKE_HANDLE = "2:ZmFrZS1oYW5kbGUtZnJvbS1tZXRh"
FAKE_PAGE_TOKEN = "page-tok-PROTOCOL-SECRET"
FAKE_USER_TOKEN = "user-tok-PROTOCOL-SECRET"


def _ctx(clip_path, account_meta, stages=None, token=FAKE_USER_TOKEN):
    from gaeclip.social.publishers.base import PublishContext
    from gaeclip.social.states import PublishRequest

    stages = stages if stages is not None else []

    def on_progress(frac, stage):
        stages.append((frac, stage))

    return PublishContext(
        request=PublishRequest(
            clip_id="c-proto", job_id="j-proto", provider="facebook",
            account_id="acc-proto", title="Proto Title", caption="Proto cap",
            hashtags=("gaeclip",), privacy="public", scheduled_at=None,
            idempotency_key="k-proto"),
        clip_path=Path(clip_path),
        access_token=lambda: token,
        on_progress=on_progress,
        on_uploaded=lambda *a, **k: None,
        should_cancel=lambda: False,
        account_meta=dict(account_meta),
    )


def _clip(tmp, size=2048):
    p = Path(tmp) / "clip.mp4"
    p.write_bytes(b"v" * size)
    return p


def _no_verify():
    return patch("gaeclip.social.publishers.meta.validate_clip_file",
                 return_value=None)


class _ChunkResp:
    """Fake urlopen response for resumable chunk POSTs / offset GETs."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class FakeMetaServer:
    """Mocked Meta protocol peer.

    start() mimics POST /{APP_ID}/uploads.
    chunk posts mimic POST /upload:<SESSION> (Authorization: OAuth header).
    finish recorder mimics POST /{PAGE_ID}/videos on the graph-video host.
    Secrets are stored here for the log-leak regression test — they must
    NEVER appear in captured logs.
    """

    def __init__(self, test, final_body=b'{"unused": true}',
                 stuck_offset=None):
        self.test = test
        self.final_body = final_body
        # stuck_offset emulates a server that stops committing bytes
        # (diagnoses upload_incomplete vs handle-missing).
        self.stuck_offset = stuck_offset
        self.committed = 0
        self.chunk_requests = []  # (method, url, headers-names-only, size)
        self.finish_calls = []  # (path, token, fields, host)
        self.finish_returns = {"id": "vid-fake-1"}

    def graph_call(self, method, path, token, params=None, **kw):
        if path.endswith("/uploads"):
            self.test.assertEqual(method, "POST")
            return {"id": FAKE_SESSION}
        raise AssertionError(f"unexpected graph call {method} {path}")

    def urlopen(self, request, timeout=300):
        import urllib.request as _u

        method = request.get_method()
        url = request.full_url
        if method == "GET":
            # Server-authoritative committed offset.
            committed = (self.stuck_offset if self.stuck_offset is not None
                         else self.committed)
            return _ChunkResp(('{"file_offset": %d}' % committed).encode())
        # Chunk POST: record method/path/auth-shape WITHOUT secret values.
        auth = request.get_header("Authorization") or ""
        self.test.assertTrue(auth.startswith("OAuth "),
                             "chunk auth must be OAuth header")
        self.test.assertNotIn("access_token", url)
        self.test.assertIn(FAKE_SESSION, url)
        size = len(request.data or b"")
        names = sorted(k.lower() for k, _ in request.header_items())
        self.chunk_requests.append((method, "POST /upload:<SESSION>",
                                    names, size))
        self.committed += size
        return _ChunkResp(self.final_body)

    def post_multipart(self, path, token, fields, file_field="",
                       filename="", filebytes=b"", host="", **kw):
        self.finish_calls.append((path, token, dict(fields), host))
        phase = (fields or {}).get("upload_phase")
        if phase == "start":
            # Page-scoped phased upload emulation (fallback path).
            self.test.assertIn("file_size", fields)
            return {"video_id": "vid-phased", "upload_session_id": "sess-1",
                    "start_offset": 0,
                    "end_offset": int(fields["file_size"])}
        if phase == "transfer":
            self.test.assertEqual(fields.get("upload_session_id"), "sess-1")
            return {"video_id": "vid-phased",
                    "start_offset": (int(fields.get("start_offset", 0))
                                     + len(filebytes or b""))}
        if phase == "finish":
            return {"video_id": "vid-phased", "success": True}
        return dict(self.finish_returns)


class ProtocolFlowTests(unittest.TestCase):
    def test_handle_passed_to_finish_not_session_id(self):
        import gaeclip.social.publishers.meta as meta_mod
        from gaeclip.social.publishers.meta import FacebookPublisher

        server = FakeMetaServer(
            self, final_body=('{"h": "%s"}' % FAKE_HANDLE).encode())
        stages = []
        with TemporaryDirectory() as tmp:
            clip = _clip(tmp)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "page_token": FAKE_PAGE_TOKEN,
                              "meta_app_id": "app-1"}, stages=stages)
            with _no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=server.graph_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=server.post_multipart), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=server.urlopen):
                res = FacebookPublisher().publish(ctx)
        self.assertEqual(res.remote_media_id, "vid-fake-1")
        # Exactly one finish call, carrying the HANDLE (never session id).
        self.assertEqual(len(server.finish_calls), 1)
        path, token, fields, host = server.finish_calls[0]
        self.assertEqual(path, "page-1/videos")
        self.assertEqual(fields.get("fbuploader_video_file_chunk"), FAKE_HANDLE)
        self.assertNotIn("upload:", fields.get("fbuploader_video_file_chunk", ""))
        self.assertNotIn("file_upload_handle", fields)
        # Page token used for finish; title/description passed through.
        self.assertEqual(token, FAKE_PAGE_TOKEN)
        self.assertEqual(fields.get("title"), "Proto Title")
        self.assertIn("Proto cap", fields.get("description", ""))
        self.assertIn("gaeclip", fields.get("description", ""))
        # Finish targets the graph-video host (docs requirement).
        self.assertEqual(host, meta_mod.GRAPH_VIDEO)
        # State semantics: uploading fractions, then finalizing — the
        # provider must NEVER emit "uploaded" before finish succeeds
        # (service persists UPLOADED itself via on_uploaded).
        emitted = [s for _, s in stages]
        self.assertIn("uploading", emitted)
        self.assertIn("finalizing", emitted)
        self.assertNotIn("uploaded", emitted)
        # "published" only as the terminal progress AFTER finish returned
        # a video id (service then persists PUBLISHED via the result).

    def test_missing_handle_falls_back_to_phased(self):
        # Resumable yields no handle (proven live: 206/debug_info-only).
        # The publisher must fall back to phased instead of terminally
        # failing — but the RESUMABLE finish (session id as handle) must
        # NEVER be attempted.
        from gaeclip.social.publishers.meta import FacebookPublisher

        server = FakeMetaServer(self, final_body=b'{"file_offset": 2048}')
        stages = []
        with TemporaryDirectory() as tmp:
            clip = _clip(tmp)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "page_token": FAKE_PAGE_TOKEN,
                              "meta_app_id": "app-1"}, stages=stages)
            with _no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=server.graph_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=server.post_multipart), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=server.urlopen), \
                 self.assertLogs("gaeclip.social.publishers.meta",
                                 level="INFO") as logs:
                res = FacebookPublisher().publish(ctx)
        # Published via the phased fallback...
        self.assertEqual(res.remote_media_id, "vid-phased")
        phases = [f.get("upload_phase") for _, _, f, _ in server.finish_calls]
        self.assertIn("start", phases)
        self.assertIn("transfer", phases)
        self.assertIn("finish", phases)
        # ...and no resumable finish ever carried a session id as handle.
        for _, _, fields, _ in server.finish_calls:
            handle = fields.get("fbuploader_video_file_chunk", "")
            self.assertFalse(str(handle).startswith("upload:"))
        # UI never told "uploaded" before remote acceptance.
        emitted = [s for _, s in stages]
        self.assertNotIn("uploaded", emitted)
        # Safe metadata log: handle=missing + fallback, field names only.
        combined = "\n".join(logs.output)
        self.assertIn("handle=missing", combined)
        self.assertIn("fallback=phased_upload", combined)
        self.assertNotIn(FAKE_HANDLE, combined)
        self.assertNotIn(FAKE_SESSION, combined)
        self.assertNotIn(FAKE_PAGE_TOKEN, combined)

    def test_phased_failure_is_terminal(self):
        # If the phased fallback itself fails, the error propagates —
        # no loop back to resumable, no blind retry.
        from gaeclip.social.publishers.meta import FacebookPublisher
        from gaeclip.social.states import SocialError, TransientError

        server = FakeMetaServer(self, final_body=b"{}")

        def broken_phased(path, token, fields, file_field="",
                          filename="", filebytes=b"", host="", **kw):
            server.finish_calls.append((path, token, dict(fields), host))
            if (fields or {}).get("upload_phase") == "start":
                return {"unexpected": "shape"}
            return {"id": "vid-x"}

        with TemporaryDirectory() as tmp:
            clip = _clip(tmp)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "page_token": FAKE_PAGE_TOKEN,
                              "meta_app_id": "app-1"})
            with _no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=server.graph_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=broken_phased), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=server.urlopen):
                with self.assertRaises(SocialError):
                    FacebookPublisher().publish(ctx)
        # Exactly one phased start attempted; no resumable finish.
        starts = [f for _, _, f, _ in server.finish_calls
                  if f.get("upload_phase") == "start"]
        self.assertEqual(len(starts), 1)
        self.assertFalse(any("fbuploader_video_file_chunk" in f
                             for _, _, f, _ in server.finish_calls))

    def test_finish_500_code1_fails_fast_no_retry(self):
        import urllib.error

        from gaeclip.social.publishers.meta import (
            _classify_graph_http_error, FacebookPublisher)
        from gaeclip.social.states import SocialError, TransientError

        # Classification first: HTTP 500 + OAuthException code=1 is NOT
        # transient — it signals a malformed/unknown provider rejection.
        err = _classify_graph_http_error(
            500, '{"error": {"message": "An unknown error occurred", '
                 '"code": 1, "type": "OAuthException"}}')
        self.assertNotIsInstance(err, TransientError)
        self.assertEqual(getattr(err, "code", ""), "unknown_provider_error")
        self.assertFalse(getattr(err, "retryable", True))

        # End to end: exactly ONE finish attempt, then fail fast.
        server = FakeMetaServer(
            self, final_body=('{"h": "%s"}' % FAKE_HANDLE).encode())
        sleeps = []

        def boom_finish(path, token, fields, file_field="",
                        filename="", filebytes=b"", host="", **kw):
            server.finish_calls.append((path, token, dict(fields), host))
            raise err

        with TemporaryDirectory() as tmp:
            clip = _clip(tmp)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "page_token": FAKE_PAGE_TOKEN,
                              "meta_app_id": "app-1"})
            with _no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=server.graph_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=boom_finish), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=server.urlopen), \
                 patch("gaeclip.social.publishers.meta.time.sleep",
                       side_effect=lambda s: sleeps.append(s)):
                with self.assertRaises(SocialError) as got:
                    FacebookPublisher().publish(ctx)
        self.assertEqual(got.exception.code, "unknown_provider_error")
        self.assertEqual(len(server.finish_calls), 1)
        self.assertEqual(sleeps, [])

    def test_debug_info_only_final_reports_structured_diagnostic(self):
        # The real incident: HTTP 200s all the way, final body carries
        # only debug_info, server DID commit the bytes. Must fall back to
        # phased and publish — plus a structured safe diagnostic with
        # server_offset, total, and debug type (no secrets).
        from gaeclip.social.publishers.meta import FacebookPublisher

        server = FakeMetaServer(
            self, final_body=b'{"debug_info": {"type": "UploadDebugMarker", '
                             b'"retriable": false}}')
        stages = []
        with TemporaryDirectory() as tmp:
            clip = _clip(tmp)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "page_token": FAKE_PAGE_TOKEN,
                              "meta_app_id": "app-1"}, stages=stages)
            with _no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=server.graph_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=server.post_multipart), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=server.urlopen), \
                 self.assertLogs("gaeclip.social.publishers.meta",
                                 level="INFO") as logs:
                res = FacebookPublisher().publish(ctx)
        # Recovered via the phased fallback...
        self.assertEqual(res.remote_media_id, "vid-phased")
        self.assertNotIn("uploaded", [s for _, s in stages])
        combined = "\n".join(logs.output)
        # ...but the structured diagnostic is still emitted, no secrets.
        self.assertIn("handle=missing", combined)
        self.assertIn("server_offset=2048", combined)
        self.assertIn("total=2048", combined)
        self.assertIn("UploadDebugMarker", combined)
        self.assertIn("fallback=phased_upload", combined)
        for secret in (FAKE_SESSION, FAKE_HANDLE, FAKE_PAGE_TOKEN,
                       FAKE_USER_TOKEN):
            self.assertNotIn(secret, combined)

    def test_error_shaped_200_body_raises_immediately(self):
        # Meta can reject a chunk with HTTP 200 + {"error": ...}. The
        # local offset must NOT advance past it to a fake 100%.
        from gaeclip.social.publishers.meta import FacebookPublisher
        from gaeclip.social.states import SocialError

        server = FakeMetaServer(
            self, final_body=b'{"error": {"message": "An unknown error", '
                             b'"code": 1, "type": "OAuthException"}}')
        with TemporaryDirectory() as tmp:
            clip = _clip(tmp)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "page_token": FAKE_PAGE_TOKEN,
                              "meta_app_id": "app-1"})
            with _no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=server.graph_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=server.post_multipart), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=server.urlopen):
                with self.assertRaises(SocialError) as got:
                    FacebookPublisher().publish(ctx)
        # Unknown provider error: fail fast, finish never attempted, only
        # ONE chunk posted (no march to 100%).
        self.assertEqual(got.exception.code, "unknown_provider_error")
        self.assertEqual(server.finish_calls, [])
        self.assertEqual(len(server.chunk_requests), 1)

    def test_server_offset_short_recovers_via_phased(self):
        # Server stops committing mid-transfer (upload_incomplete on the
        # resumable path): must recover via the phased fallback, which
        # transfers the bytes itself. Resumable finish never attempted.
        from gaeclip.social.publishers.meta import FacebookPublisher

        server = FakeMetaServer(self, final_body=b"{}",
                                stuck_offset=0)
        with TemporaryDirectory() as tmp:
            clip = _clip(tmp)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "page_token": FAKE_PAGE_TOKEN,
                              "meta_app_id": "app-1"})
            with _no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=server.graph_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=server.post_multipart), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=server.urlopen), \
                 self.assertLogs("gaeclip.social.publishers.meta",
                                 level="INFO") as logs:
                res = FacebookPublisher().publish(ctx)
        self.assertEqual(res.remote_media_id, "vid-phased")
        self.assertIn("fallback=phased_upload", "\n".join(logs.output))
        for _, _, fields, _ in server.finish_calls:
            self.assertFalse(str(fields.get("fbuploader_video_file_chunk",
                                            "")).startswith("upload:"))

    def test_incomplete_then_broken_phased_propagates(self):
        # Stuck resumable triggers the fallback; if phased start is also
        # malformed, that error propagates (terminal, single attempt).
        from gaeclip.social.publishers.meta import FacebookPublisher
        from gaeclip.social.states import SocialError, TransientError

        server = FakeMetaServer(self, final_body=b"{}",
                                stuck_offset=0)

        def broken_start(path, token, fields, file_field="",
                         filename="", filebytes=b"", host="", **kw):
            server.finish_calls.append((path, token, dict(fields), host))
            if (fields or {}).get("upload_phase") == "start":
                return {"unexpected": "shape"}
            return {"id": "vid-x"}

        with TemporaryDirectory() as tmp:
            clip = _clip(tmp)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "page_token": FAKE_PAGE_TOKEN,
                              "meta_app_id": "app-1"})
            with _no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=server.graph_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=broken_start), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=server.urlopen):
                with self.assertRaises(TransientError):
                    FacebookPublisher().publish(ctx)
        starts = [1 for _, _, f, _ in server.finish_calls
                  if f.get("upload_phase") == "start"]
        self.assertEqual(len(starts), 1)

    def test_chunk_400_wrong_offset_maps_invalid_request(self):
        import urllib.error

        from gaeclip.social.publishers.meta import FacebookPublisher
        from gaeclip.social.states import SocialError

        server = FakeMetaServer(self, final_body=b"{}")

        class FP:
            def read(self):
                return (b'{"error": {"message": "Invalid offset", '
                        b'"code": 100, "type": "OAuthException"}}')

            def close(self):
                pass

            def info(self):
                return {}

        def boom(request, timeout=300):
            if request.get_method() == "GET":
                return server.urlopen(request, timeout)
            raise urllib.error.HTTPError(request.full_url, 400,
                                         "Bad Request", {}, FP())

        with TemporaryDirectory() as tmp:
            clip = _clip(tmp)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "page_token": FAKE_PAGE_TOKEN,
                              "meta_app_id": "app-1"})
            with _no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=server.graph_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=server.post_multipart), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=boom):
                with self.assertRaises(SocialError) as got:
                    FacebookPublisher().publish(ctx)
        self.assertEqual(got.exception.code, "invalid_request")
        self.assertEqual(server.finish_calls, [])


class HandleParserTests(unittest.TestCase):
    def test_official_h_shape(self):
        from gaeclip.social.publishers.meta import extract_upload_handle

        self.assertEqual(
            extract_upload_handle({"h": "2:c2FtcGxl"}), "2:c2FtcGxl")

    def test_aliases_and_nesting(self):
        from gaeclip.social.publishers.meta import extract_upload_handle

        self.assertEqual(
            extract_upload_handle({"handle": "abc"}), "abc")
        self.assertEqual(
            extract_upload_handle({"data": {"h": "2:xxx"}}), "2:xxx")

    def test_session_id_never_a_handle(self):
        from gaeclip.social.publishers.meta import extract_upload_handle

        self.assertIsNone(extract_upload_handle({"id": FAKE_SESSION}))
        self.assertIsNone(extract_upload_handle({"h": FAKE_SESSION}))
        self.assertIsNone(extract_upload_handle({}))
        self.assertIsNone(extract_upload_handle({"file_offset": 2048}))
        self.assertIsNone(extract_upload_handle(None))
        self.assertIsNone(extract_upload_handle("2:notadict"))

    def test_safe_finish_schema(self):
        from gaeclip.social.publishers.meta import safe_finish_request_schema

        schema = safe_finish_request_schema()
        self.assertIn("fbuploader_video_file_chunk", schema)
        self.assertIn("graph-video", schema)
        self.assertIn("title", schema)
        self.assertIn("description", schema)
        self.assertNotIn(FAKE_HANDLE, schema)
        self.assertNotIn(FAKE_PAGE_TOKEN, schema)


class LogHygieneTests(unittest.TestCase):
    def test_no_secrets_in_logs(self):
        from gaeclip.social.publishers.meta import FacebookPublisher

        server = FakeMetaServer(
            self, final_body=('{"h": "%s"}' % FAKE_HANDLE).encode())
        with TemporaryDirectory() as tmp:
            clip = _clip(tmp)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "page_token": FAKE_PAGE_TOKEN,
                              "meta_app_id": "app-1"})
            with _no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=server.graph_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=server.post_multipart), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=server.urlopen), \
                 self.assertLogs("gaeclip.social.publishers.meta",
                                 level="INFO") as logs:
                FacebookPublisher().publish(ctx)
        combined = "\n".join(logs.output)
        for secret in (FAKE_SESSION, FAKE_HANDLE, FAKE_PAGE_TOKEN,
                       FAKE_USER_TOKEN):
            self.assertNotIn(secret, combined)
        self.assertNotIn("access_token", combined.lower())
        # ...but the honest presence signals remain.
        self.assertIn("handle=present", combined)
        self.assertIn("response_fields", combined)


class FinalizingStageTests(unittest.TestCase):
    def test_finalizing_keeps_uploading_phase_finalizing(self):
        import sys

        sys.path.insert(0, "tests")
        from test_video_metadata import FakeRedis

        from gaeclip.social.service import SocialPublishService

        svc = SocialPublishService(FakeRedis(), runtime="desktop")
        from gaeclip.social.accounts import SocialPublishJob

        job = SocialPublishJob(id="spub-fin", clip_id="c1", job_id="job-1",
                               provider="facebook",
                               social_account_id="acc-1", status="uploading",
                               title="t", idempotency_key="k-fin")
        svc.jobs.save_job(job)
        svc._progress(job, 1.0, "finalizing")
        saved = svc.jobs.get_job("spub-fin")
        # Never UPLOADED (no remote id exists yet); indeterminate finalize.
        self.assertEqual(saved.status, "uploading")
        self.assertEqual(saved.phase, "finalizing")
        self.assertIsNone(saved.progress)
        self.assertIn("finaliz", (saved.status_message or "").lower())


if __name__ == "__main__":
    unittest.main()
