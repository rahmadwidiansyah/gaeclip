"""Facebook Resumable Upload + Page picker tests (no network, no credentials)."""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

sys.path.insert(0, "tests")


def _ctx(clip_path, account_meta, token="page-tok"):
    from gaeclip.social.publishers.base import PublishContext
    from gaeclip.social.states import PublishRequest

    return PublishContext(
        request=PublishRequest(
            clip_id="c1", job_id="j1", provider="facebook",
            account_id="acc-1", title="t", caption="cap",
            hashtags=(), privacy="public", scheduled_at=None,
            idempotency_key="k"),
        clip_path=Path(clip_path),
        access_token=lambda: token,
        on_progress=lambda *a, **k: None,
        on_uploaded=lambda *a, **k: None,
        should_cancel=lambda: False,
        account_meta=dict(account_meta),
    )


class _FakeFP:
    def __init__(self, body: str):
        self._body = body.encode()

    def read(self, *args, **kwargs):
        return self._body

    def close(self):
        pass

    def info(self):
        return {}


def _http_400(body: str):
    import urllib.error

    return urllib.error.HTTPError(
        "https://graph.facebook.com/v21.0/x", 400,
        "Bad Request", {}, _FakeFP(body))


class FacebookUploadTests(unittest.TestCase):
    def _clip(self, tmp, size=3 * 1024 * 1024 + 123):
        p = Path(tmp) / "clip.mp4"
        p.write_bytes(b"v" * size)
        return p

    def _no_verify(self):
        return patch("gaeclip.social.publishers.meta.validate_clip_file",
                     return_value=None)

    def test_publish_uses_resumable_flow(self):
        from gaeclip.social.publishers.meta import FacebookPublisher

        opened_urls = []
        opened_headers = []

        real_urlopen = __import__("urllib.request", fromlist=["urlopen"]).urlopen

        def fake_urlopen(request, timeout=300):
            opened_urls.append(request.full_url)
            opened_headers.append(dict(request.header_items()))

            class Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    # Chunk responses carry the resumable-upload handle.
                    return b'{"h": "2:dGVzdGhhbmRsZQ=="}'
            return Resp()

        calls = []
        finishes = []
        finish_hosts = []

        def fake_call(method, path, token, params=None, **kw):
            calls.append((method, path, dict(params or {})))
            if path.endswith("/uploads"):
                self.assertEqual(method, "POST")
                self.assertEqual(params.get("file_type"), "video/mp4")
                self.assertIn("file_length", params)
                self.assertIn("file_name", params)
                return {"id": "upload:SESS123"}
            raise AssertionError(f"unexpected {method} {path}")

        def fake_finish(path, token, fields, file_field="",
                        filename="", filebytes=b"", host="", **kw):
            finishes.append(dict(fields))
            finish_hosts.append(host)
            # Official Video API shape: multipart title + description +
            # fbuploader_video_file_chunk (never the old file_upload_handle),
            # carrying the HANDLE — never the session id — with the Page
            # token on the graph-video host.
            self.assertIn("fbuploader_video_file_chunk", fields)
            self.assertNotIn("file_upload_handle", fields)
            self.assertIn("title", fields)
            self.assertIn("description", fields)
            self.assertEqual(fields["fbuploader_video_file_chunk"],
                             "2:dGVzdGhhbmRsZQ==")
            self.assertNotEqual(fields["fbuploader_video_file_chunk"],
                                "upload:SESS123")
            self.assertEqual(token, "page-tok")
            return {"id": "vid-9"}

        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "page_token": "page-tok",
                              "meta_app_id": "app-1"})
            with self._no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=fake_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=fake_finish), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=fake_urlopen):
                res = FacebookPublisher().publish(ctx)
        self.assertEqual(res.remote_media_id, "vid-9")
        # Start went through _GraphClient; finish via multipart; chunks via
        # upload: URL.
        self.assertTrue(any(p.endswith("/uploads") for _, p, _ in calls))
        self.assertEqual(len(finishes), 1)
        # Finish goes to the graph-video host (graph.facebook.com 500s).
        import gaeclip.social.publishers.meta as meta_mod

        self.assertTrue(all(h == meta_mod.GRAPH_VIDEO for h in finish_hosts))
        self.assertTrue(all("upload:SESS123" in u for u in opened_urls))
        self.assertTrue(all("upload:SESS123" in u for u in opened_urls))
        # Auth via header, never access_token in chunk URL.
        for url, headers in zip(opened_urls, opened_headers):
            self.assertNotIn("access_token", url)
            auth = headers.get("Authorization", headers.get("authorization", ""))
            self.assertIn("page-tok", auth)
        # No trace of the dead edge.
        import subprocess

        all_calls = str(calls) + str(opened_urls)
        self.assertNotIn("video_uploads", all_calls)
        self.assertNotIn("upload_session_id", all_calls)

    def test_publish_requires_app_id(self):
        from gaeclip.social.publishers.meta import FacebookPublisher
        from gaeclip.social.states import SocialError

        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp, size=1024)
            ctx = _ctx(clip, {"facebook_page_id": "page-1"})
            with self._no_verify(), self.assertRaises(SocialError) as got:
                FacebookPublisher().publish(ctx)
        self.assertEqual(got.exception.code, "oauth_not_configured")

    def test_chunk_400_uses_graph_mapping(self):
        import urllib.error

        from gaeclip.social.publishers.meta import FacebookPublisher
        from gaeclip.social.states import SocialError

        class FP:
            def read(self):
                return (b'{"error":{"message":"Unknown path components",'
                        b'"code":2500,"type":"OAuthException"}}')

            def close(self):
                pass

            def info(self):
                return {}

        def boom(request, timeout=300):
            raise urllib.error.HTTPError(request.full_url, 400,
                                         "Bad Request", {}, FP())

        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp, size=1024)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "meta_app_id": "app-1"})
            with self._no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       return_value={"id": "upload:S1"}), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=boom):
                with self.assertRaises(SocialError) as got:
                    FacebookPublisher().publish(ctx)
        self.assertEqual(got.exception.code, "invalid_request")
        self.assertIn("2500", str(got.exception))

    def test_broken_pipe_once_resumes_same_session(self):
        from gaeclip.social.publishers.meta import CHUNK_BYTES, FacebookPublisher

        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp, size=CHUNK_BYTES * 2 + 100)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "meta_app_id": "app-1"})
            starts = []

            def fake_call(method, path, token, params=None, **kw):
                if path.endswith("/uploads"):
                    starts.append(path)
                    return {"id": "upload:S1"}
                if path.endswith("/videos"):
                    return {"id": "vid-1"}
                raise AssertionError(path)

            state = {"n": 0, "committed": 0}

            def fake_urlopen(request, timeout=300):
                # GET offset query -> server-authoritative committed bytes.
                if request.get_method() == "GET":
                    committed = state["committed"]

                    class G:
                        def __enter__(self):
                            return self

                        def __exit__(self, *a):
                            return False

                        def read(self):
                            return ('{"file_offset": %d}' % committed).encode()
                    return G()
                state["n"] += 1
                if state["n"] == 2:
                    raise OSError(32, "Broken pipe")
                # Record Content-Length present.
                assert request.get_header("Content-length") or \
                    request.get_header("Content-Length"), "missing Content-Length"
                state["committed"] += len(request.data or b"")

                class Resp:
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                    def read(self):
                        return b'{"h": "handle-1"}'
                return Resp()

            with self._no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=fake_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       return_value={"id": "vid-1"}), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=fake_urlopen):
                res = FacebookPublisher().publish(ctx)
        # Only ONE session despite the mid-way broken pipe.
        self.assertEqual(len(starts), 1)
        self.assertEqual(res.remote_media_id, "vid-1")

    def test_same_offset_3x_fails_fast(self):
        from gaeclip.social.publishers.meta import FacebookPublisher
        from gaeclip.social.states import SocialError

        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp, size=2048)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "meta_app_id": "app-1"})
            pub = FacebookPublisher()
            with self._no_verify(), \
                 patch.object(FacebookPublisher, "_query_session",
                              return_value=(0, '{"file_offset": 0}')), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=OSError(32, "Broken pipe")):
                with self.assertRaises(SocialError) as got:
                    pub._upload_parts(ctx, "page-tok", "upload:S1", 2048)
        # Offset-wall gets its own code so publish() can fall back to the
        # Page-scoped phased upload instead of looping fresh sessions.
        self.assertEqual(got.exception.code, "upload_offset_wall")
        self.assertFalse(getattr(got.exception, "retryable", True))

    def test_query_unreachable_stays_transient(self):
        from gaeclip.social.publishers.meta import FacebookPublisher
        from gaeclip.social.states import TransientError

        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp, size=2048)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "meta_app_id": "app-1"})
            with self._no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       return_value={"id": "upload:S1"}), \
                 patch.object(FacebookPublisher, "_query_session",
                              return_value=(None, "")), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=OSError(32, "Broken pipe")):
                with self.assertRaises(TransientError):
                    FacebookPublisher().publish(ctx)

    def test_offset_wall_falls_back_to_phased(self):
        from gaeclip.social.publishers.meta import FacebookPublisher

        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp, size=2048)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "meta_app_id": "app-1"})
            phased_bodies = []

            def fake_multipart(path, token, fields, file_field="",
                               filename="", filebytes=b"", host="", **kw):
                import gaeclip.social.publishers.meta as meta_mod

                # Phased Page upload also targets the graph-video host.
                self.assertEqual(host, meta_mod.GRAPH_VIDEO)
                phase = fields.get("upload_phase")
                if phase == "start":
                    self.assertIn("file_size", fields)
                    return {"video_id": "vid-p", "upload_session_id": "sess-1",
                            "start_offset": 0, "end_offset": 2048}
                if phase == "transfer":
                    self.assertEqual(fields.get("upload_session_id"), "sess-1")
                    self.assertTrue(filebytes)
                    phased_bodies.append(bytes(filebytes))
                    return {"video_id": "vid-p",
                            "start_offset": int(fields["start_offset"]) + len(filebytes)}
                if phase == "finish":
                    return {"video_id": "vid-p", "success": True}
                raise AssertionError(phase)


            with self._no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       return_value={"id": "upload:S1"}), \
                 patch.object(FacebookPublisher, "_query_session",
                              return_value=(0, '{"file_offset": 0}')), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=OSError(32, "Broken pipe")), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=fake_multipart):
                with self.assertLogs("gaeclip.social.publishers.meta",
                                     level="INFO") as logs:
                    res = FacebookPublisher().publish(ctx)
                expected = Path(clip).read_bytes()
        self.assertEqual(res.remote_media_id, "vid-p")
        self.assertIn("fallback=phased_upload", "\n".join(logs.output))
        self.assertEqual(b"".join(phased_bodies), expected)
        # No secrets in fallback logs.
        combined = "\n".join(logs.output)
        self.assertNotIn("page-tok", combined)
        self.assertNotIn("access_token", combined.lower())

    def test_phased_finish_success_flag_uses_start_video_id(self):
        # Live behavior: finish answers {"success": true} with no id.
        # The publisher must use the start-phase video_id, not fail.
        from gaeclip.social.publishers.meta import FacebookPublisher

        seen = {}

        def fake_multipart(path, token, fields, file_field="",
                           filename="", filebytes=b"", host="", **kw):
            phase = fields.get("upload_phase")
            if phase == "start":
                return {"video_id": "vid-start", "upload_session_id": "s",
                        "start_offset": 0, "end_offset": 64}
            if phase == "transfer":
                seen["transfer"] = True
                return {"video_id": "vid-start",
                        "start_offset": int(fields["start_offset"]) + len(filebytes)}
            if phase == "finish":
                return {"success": True}
            raise AssertionError(phase)

        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp, size=64)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "meta_app_id": "app-1"})
            pub = FacebookPublisher()
            with self._no_verify(), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=fake_multipart):
                res = pub._publish_phased(ctx, "page-tok", "page-1", 64,
                                          "t", "d")
        self.assertEqual(res.remote_media_id, "vid-start")
        self.assertTrue(seen.get("transfer"))

    def test_chunk_retries_keep_uniform_size(self):
        import gaeclip.social.publishers.meta as meta_mod
        from gaeclip.social.publishers.meta import FacebookPublisher

        # Uniform chunk geometry: retries resend the SAME size (mid-stream
        # shrinking once produced irregular layouts ending 206/no-handle).
        chunk = 512 * 1024
        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp, size=chunk + 100)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "meta_app_id": "app-1"})
            sizes = []
            state = {"n": 0, "committed": 0}

            def fake_urlopen(request, timeout=300):
                if request.get_method() == "GET":
                    committed = state["committed"]

                    class G:
                        def __enter__(self):
                            return self

                        def __exit__(self, *a):
                            return False

                        def read(self):
                            return ('{"file_offset": %d}' % committed).encode()
                    return G()
                state["n"] += 1
                sizes.append(len(request.data or b""))
                if state["n"] < 3:
                    raise OSError(32, "Broken pipe")
                state["committed"] += len(request.data or b"")

                class Resp:
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                    def read(self):
                        return b'{"h": "2:c2hyaW5rLWhhbmRsZQ=="}'
                return Resp()

            with self._no_verify(), \
                 patch.object(meta_mod, "CHUNK_BYTES", chunk), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=lambda m, p, t, pr=None, **kw:
                       {"id": "upload:S1"}), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       return_value={"id": "vid-1"}), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=fake_urlopen):
                FacebookPublisher().publish(ctx)
            # Retried with identical size; uniform layout preserved.
            self.assertGreaterEqual(len(sizes), 3)
            self.assertEqual(sizes[0], sizes[1])

    def test_server_ahead_of_local_fails_fast(self):
        # A rejected attempt that the server actually committed (e.g.
        # post-commit 429): resuming would duplicate bytes and poison the
        # assembly, so fail fast instead of marching on.
        from gaeclip.social.publishers.meta import FacebookPublisher
        from gaeclip.social.states import SocialError

        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp, size=2048)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "meta_app_id": "app-1"})
            with self._no_verify(), \
                 patch.object(FacebookPublisher, "_query_session",
                              return_value=(1024, '{"file_offset": 1024}')), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=OSError(32, "Broken pipe")):
                with self.assertRaises(SocialError) as got:
                    FacebookPublisher()._upload_parts(
                        ctx, "page-tok", "upload:S1", 2048)
        self.assertEqual(got.exception.code, "upload_offset_mismatch")
        self.assertFalse(getattr(got.exception, "retryable", True))

    def test_backoff_parse(self):
        from gaeclip.social.publishers.meta import _parse_rate_backoff_ms

        self.assertEqual(_parse_rate_backoff_ms(
            '{"backoff":60000,"debug_info":{"retriable":false}}'), 60_000)
        self.assertEqual(_parse_rate_backoff_ms(
            '{"error":{"backoff":5000}}'), 5_000)
        self.assertEqual(_parse_rate_backoff_ms('not json'), 60_000)
        self.assertEqual(_parse_rate_backoff_ms('{}'), 60_000)
        # Clamped to [1s, 120s].
        self.assertEqual(_parse_rate_backoff_ms('{"backoff":5}'), 1_000)
        self.assertEqual(_parse_rate_backoff_ms('{"backoff":9999999}'), 120_000)

    def test_429_backoff_then_resume_same_session(self):
        import urllib.error

        import gaeclip.social.publishers.meta as meta_mod
        from gaeclip.social.publishers.meta import FacebookPublisher

        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp, size=2048)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "meta_app_id": "app-1"})
            starts = []
            sleeps = []
            posts = []

            class FP429:
                def read(self):
                    return (b'{"backoff":100,"debug_info":{"retriable":false,'
                            b'"type":"UploadRateLimitedError"}}')

                def close(self):
                    pass

                def info(self):
                    return {}

            class Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b'{"h": "2:NDI5LWhhbmRsZQ=="}'

            committed = {"n": 0}

            def fake_urlopen(request, timeout=300):
                if request.get_method() == "GET":
                    done = committed["n"]

                    class G:
                        def __enter__(self):
                            return self

                        def __exit__(self, *a):
                            return False

                        def read(self):
                            return ('{"file_offset": %d}' % done).encode()
                    return G()
                posts.append(1)
                if len(posts) == 1:
                    raise urllib.error.HTTPError(
                        request.full_url, 429, "Too Many Requests", {}, FP429())
                committed["n"] += len(request.data or b"")
                return Resp()

            def fake_call(method, path, token, params=None, **kw):
                if path.endswith("/uploads"):
                    starts.append(path)
                    return {"id": "upload:S1"}
                raise AssertionError(path)

            with self._no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=fake_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       return_value={"id": "vid-1"}), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=fake_urlopen), \
                 patch("gaeclip.social.publishers.meta.time.sleep",
                       side_effect=lambda s: sleeps.append(s)):
                with self.assertLogs("gaeclip.social.publishers.meta",
                                     level="INFO") as logs:
                    res = FacebookPublisher().publish(ctx)
        # One session only: honored backoff, resumed, published.
        self.assertEqual(len(starts), 1)
        self.assertEqual(res.remote_media_id, "vid-1")
        self.assertTrue(sleeps)
        self.assertIn("rate_limited", "\n".join(logs.output))

    def test_429_four_times_raises_transient(self):
        import urllib.error

        from gaeclip.social.publishers.meta import FacebookPublisher
        from gaeclip.social.states import TransientError

        class FP429:
            def read(self):
                return b'{"backoff":10}'

            def close(self):
                pass

            def info(self):
                return {}

        def always_429(request, timeout=300):
            if request.get_method() == "GET":
                class G:
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                    def read(self):
                        return b'{"file_offset": 0}'
                return G()
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests", {}, FP429())

        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp, size=2048)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "meta_app_id": "app-1"})
            with self._no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       return_value={"id": "upload:S1"}), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=always_429), \
                 patch("gaeclip.social.publishers.meta.time.sleep",
                       return_value=None):
                with self.assertRaises(TransientError):
                    FacebookPublisher().publish(ctx)


    def test_is_transient_flag_maps_to_transient(self):
        import json

        from gaeclip.social.publishers.meta import (
            _graph_is_transient, _GraphClient)
        from gaeclip.social.states import TransientError

        self.assertTrue(_graph_is_transient(json.dumps(
            {"error": {"message": "timed out", "code": 390,
                       "error_subcode": 1363030, "is_transient": True}})))
        self.assertFalse(_graph_is_transient(json.dumps(
            {"error": {"message": "x", "code": 200}})))
        self.assertFalse(_graph_is_transient("not json"))

        body = json.dumps(
            {"error": {"message": "There was a problem uploading your video file.",
                       "code": 390, "error_subcode": 1363030,
                       "type": "OAuthException", "is_transient": True}})
        with patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                   side_effect=_http_400(body)):
            with self.assertRaises(TransientError) as got:
                _GraphClient.call("POST", "page-1/videos", "tok", {})
        self.assertIn("390", str(got.exception))

    def test_finish_timeout_then_success(self):
        from gaeclip.social.publishers.meta import FacebookPublisher
        from gaeclip.social.states import TransientError

        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp, size=1024)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "meta_app_id": "app-1"})
            calls = []
            sleeps = []

            class Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b'{"h": "2:ZmluaXNoLWhhbmRsZQ=="}'

            def fake_call(method, path, token, params=None, **kw):
                calls.append(path)
                if path.endswith("/uploads"):
                    return {"id": "upload:S1"}
                raise AssertionError(path)

            finishes = [
                TransientError("meta transient: timed out (code=390)"),
                {"id": "vid-7"},
            ]

            def fake_finish(path, token, fields, file_field="",
                            filename="", filebytes=b"", host="", **kw):
                # Official shape: fbuploader_video_file_chunk + title.
                self.assertIn("fbuploader_video_file_chunk", fields)
                self.assertIn("title", fields)
                self.assertEqual(fields["fbuploader_video_file_chunk"],
                                 "2:ZmluaXNoLWhhbmRsZQ==")
                out = finishes.pop(0)
                if isinstance(out, Exception):
                    raise out
                return out

            with self._no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       side_effect=fake_call), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=fake_finish), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=lambda r, timeout=300: Resp()), \
                 patch("gaeclip.social.publishers.meta.time.sleep",
                       side_effect=lambda s: sleeps.append(s)):
                with self.assertLogs("gaeclip.social.publishers.meta",
                                     level="INFO") as logs:
                    res = FacebookPublisher().publish(ctx)
        self.assertEqual(res.remote_media_id, "vid-7")
        self.assertEqual(sleeps, [15])
        self.assertIn("attempt=2 ok", "\n".join(logs.output))

    def test_finish_3x_fail_raises_transient(self):
        from gaeclip.social.publishers.meta import FacebookPublisher
        from gaeclip.social.states import TransientError

        with TemporaryDirectory() as tmp:
            clip = self._clip(tmp, size=1024)
            ctx = _ctx(clip, {"facebook_page_id": "page-1",
                              "meta_app_id": "app-1"})
            sleeps = []

            class Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b'{"h": "2:M3gtZmFpbC1oYW5kbGU="}'

            with self._no_verify(), \
                 patch("gaeclip.social.publishers.meta._GraphClient.call",
                       return_value={"id": "upload:S1"}), \
                 patch("gaeclip.social.publishers.meta._post_multipart",
                       side_effect=TransientError("meta transient: t")), \
                 patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=lambda r, timeout=300: Resp()), \
                 patch("gaeclip.social.publishers.meta.time.sleep",
                       side_effect=lambda s: sleeps.append(s)):
                with self.assertRaises(TransientError):
                    FacebookPublisher().publish(ctx)
        self.assertEqual(sleeps, [15, 30])


class PagePickerTests(unittest.TestCase):
    def test_select_page_swaps_token(self):
        from gaeclip.social.service import SocialPublishService

        svc = SocialPublishService.__new__(SocialPublishService)
        import types

        acct = types.SimpleNamespace(
            id="acc-fb-1", provider="facebook",
            external_account_id="page-old", display_name="Old",
            status="connected",
            meta={"facebook_page_id": "page-old", "page_token": "tok-old",
                  "page_choices": [{"id": "page-old", "name": "Old"}]},
            token_reference="ref-1")
        svc.accounts = types.SimpleNamespace(
            get=lambda _id: acct,
            save=lambda a: setattr(svc, "_saved", a))
        svc.secrets = types.SimpleNamespace(
            get=lambda _ref: {"access_token": "user-tok"})
        svc.client = types.SimpleNamespace()

        pages = {"data": [
            {"id": "page-old", "name": "Old", "access_token": "tok-old"},
            {"id": "page-1", "name": "Gaeclip1", "access_token": "tok-new"},
        ]}
        with patch("gaeclip.social.service.publisher_for") as _pf, \
             patch("gaeclip.social.publishers.meta._GraphClient.call",
                   return_value=pages):
            svc._access_token = lambda account, publisher: "user-tok"  # noqa
            out = svc.select_facebook_page("acc-fb-1", "page-1")
        self.assertEqual(out.external_account_id, "page-1")
        self.assertEqual(out.meta["facebook_page_id"], "page-1")
        self.assertEqual(out.meta["page_token"], "tok-new")
        self.assertEqual(len(out.meta["page_choices"]), 2)

    def test_select_unknown_page_404(self):
        from gaeclip.social.service import SocialPublishService
        from gaeclip.social.states import SocialError

        svc = SocialPublishService.__new__(SocialPublishService)
        import types

        acct = types.SimpleNamespace(
            id="acc-fb-1", provider="facebook",
            external_account_id="page-old", display_name="Old",
            status="connected", meta={}, token_reference="ref-1")
        svc.accounts = types.SimpleNamespace(get=lambda _id: acct)
        svc.secrets = types.SimpleNamespace(
            get=lambda _ref: {"access_token": "user-tok"})
        svc.client = types.SimpleNamespace()
        with patch("gaeclip.social.service.publisher_for"), \
             patch("gaeclip.social.publishers.meta._GraphClient.call",
                   return_value={"data": [
                       {"id": "page-old", "name": "Old",
                        "access_token": "tok-old"}]}):
            svc._access_token = lambda account, publisher: "user-tok"  # noqa
            with self.assertRaises(SocialError) as got:
                svc.select_facebook_page("acc-fb-1", "nope")
        self.assertEqual(got.exception.code, "not_found")


if __name__ == "__main__":
    unittest.main()
