"""Instagram Login independence tests (no network, no credentials).

Covers: separate OAuth config/URLs/scopes, dedicated callback
(valid/invalid/expired/exchange-failure/discovery-failure/storage/expiry),
no-Facebook architecture (behavioral host assertions), legacy detection,
publisher container/publish/poll/errors/reconcile, refresh branch, and
log hygiene. Facebook behavior is asserted unchanged by the existing
meta/facebook suites.
"""

import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, "tests")

from test_video_metadata import FakeRedis

TEST_SECRET = "dGVzdHNlY3JldGtleXRlc3RzMTIzNDU2Nzg="
SHORT_TOKEN = "IG-SHORT-SECRET-1"
LONG_TOKEN = "IG-LONG-SECRET-2"
IG_ID = "17841400008460056"


def _env():
    return patch.dict(os.environ, {"SETTINGS_SECRET": TEST_SECRET,
                                   "SOCIAL_INSTAGRAM_APP_ID": "ig-app-id",
                                   "SOCIAL_INSTAGRAM_APP_SECRET": "ig-app-secret"})


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body

    def close(self):
        pass

    def info(self):
        return {}


def _ctx(clip_path, account_meta, stages=None):
    from gaeclip.social.publishers.base import PublishContext
    from gaeclip.social.states import PublishRequest

    stages = stages if stages is not None else []

    return PublishContext(
        request=PublishRequest(
            clip_id="c-ig", job_id="j-ig", provider="instagram",
            account_id="acc-ig", title="t", caption="hello",
            hashtags=("reels",), privacy="public", scheduled_at=None,
            idempotency_key="k-ig"),
        clip_path=Path(clip_path),
        access_token=lambda: LONG_TOKEN,
        on_progress=lambda f, s: stages.append((f, s)),
        on_uploaded=lambda *a, **k: None,
        should_cancel=lambda: False,
        account_meta=dict(account_meta),
    )


class ConfigTests(unittest.TestCase):
    def test_authorize_url_shape(self):
        import urllib.parse

        from gaeclip.social import oauth as _oauth
        from gaeclip.social.service import _provider_oauth_config

        config = _provider_oauth_config("instagram")
        session = _oauth.build_authorization_url(
            "ig-app-id", config, "http://localhost:47291/oauth/callback")
        parsed = urllib.parse.urlparse(session.authorization_url)
        self.assertIn("instagram.com", parsed.netloc)
        self.assertNotIn("facebook", session.authorization_url)
        params = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(params.get("client_id"), ["ig-app-id"])
        self.assertEqual(params.get("redirect_uri"),
                         ["http://localhost:47291/oauth/callback"])
        self.assertEqual(params.get("response_type"), ["code"])
        self.assertTrue(params.get("state"))
        # Comma-separated scopes per Instagram Login docs; no secrets.
        (scope,) = params.get("scope", [""])
        self.assertIn(",", scope)
        self.assertIn("instagram_business_content_publish", scope)
        self.assertNotIn("secret", session.authorization_url.lower())

    def test_client_config_uses_instagram_keys(self):
        from gaeclip.social.service import _oauth_client_config

        cfg = _oauth_client_config(FakeRedis())["instagram"]
        with patch.dict(os.environ, {"SOCIAL_INSTAGRAM_APP_ID": "A",
                                     "SOCIAL_INSTAGRAM_APP_SECRET": "S",
                                     "SOCIAL_META_APP_ID": "M"}):
            cfg = _oauth_client_config(FakeRedis())["instagram"]
            self.assertEqual(cfg["client_id"], "A")
            self.assertEqual(cfg["client_secret"], "S")

    def test_scopes_independent(self):
        from gaeclip.social.service import _provider_oauth_config

        fb = _provider_oauth_config("facebook")
        ig = _provider_oauth_config("instagram")
        self.assertTrue(set(fb.scopes).isdisjoint(set(ig.scopes)))


class CallbackTests(unittest.TestCase):
    def _session(self, flow_mod, provider="instagram"):
        import uuid

        from gaeclip.social import oauth as _oauth
        from gaeclip.social.service import _provider_oauth_config

        config = _provider_oauth_config(provider)
        session = _oauth.build_authorization_url(
            "ig-app-id", config, "http://localhost:47291/oauth/callback")
        session_id = f"oauth_{uuid.uuid4().hex[:12]}"
        flow_mod._sessions[session_id] = {
            "status": "pending", "provider": provider,
            "created_at": time.time(), "account_id": "", "error": "",
            "oauth": session, "client_id": "ig-app-id",
            "client_secret": "ig-app-secret",
        }
        return session_id, session

    def _token_urlopen(self, me_body=b'{"id": "%s", "username": "myig", "account_type": "BUSINESS"}' % IG_ID.encode()):
        def fake(request, timeout=30):
            url = request.full_url
            if "api.instagram.com/oauth/access_token" in url:
                return _Resp(('{"data": [{"access_token": "%s", '
                              '"user_id": "%s", "permissions": '
                              '"instagram_business_basic,'
                              'instagram_business_content_publish"}]}'
                             % (SHORT_TOKEN, IG_ID)).encode())
            if "graph.instagram.com/access_token" in url:
                return _Resp(('{"access_token": "%s", "expires_in": 5184000}'
                              % LONG_TOKEN).encode())
            if "/me" in url:
                return _Resp(me_body)
            raise AssertionError(f"unexpected token/discovery URL {url}")
        return fake

    def test_valid_callback_stores_long_lived_and_account(self):
        from gaeclip.social import oauth_flow as flow_mod
        from gaeclip.social.accounts import SocialAccountStore
        from gaeclip.social.secrets import make_secret_store

        client = FakeRedis()
        session_id, session = self._session(flow_mod)
        with _env(), \
             patch("urllib.request.urlopen", side_effect=self._token_urlopen()), \
             self.assertLogs("gaeclip.social.oauth_flow",
                             level="INFO") as logs:
            flow_mod.complete_by_state(
                client, session.state, code="authcode-SECRET-xyz")
        item = flow_mod._sessions.get(session_id) or {}
        self.assertEqual(item.get("status"), "complete")
        account = SocialAccountStore(client).get(item["account_id"])
        self.assertEqual(account.provider, "instagram")
        self.assertEqual(account.external_account_id, IG_ID)
        self.assertEqual(account.display_name, "@myig")
        self.assertEqual(account.status, "connected")
        self.assertIn("instagram_business_content_publish", account.scopes)
        meta = account.meta or {}
        self.assertEqual(meta.get("login_flow"), "instagram_login")
        self.assertEqual(meta.get("ig_user_id"), IG_ID)
        self.assertNotIn("page_token", meta)
        self.assertNotIn("facebook_page_id", meta)
        # Stored credential is the LONG-lived token, never the short one.
        creds = make_secret_store(client, "server").get(account.token_reference)
        self.assertEqual(creds.get("access_token"), LONG_TOKEN)
        self.assertNotEqual(creds.get("access_token"), SHORT_TOKEN)
        # ~60 days expiry.
        self.assertGreater(creds.get("expires_at", 0), time.time() + 50 * 86400)
        # Hygiene: code / tokens / secret / state never logged.
        combined = "\n".join(logs.output)
        for secret in ("authcode-SECRET-xyz", SHORT_TOKEN, LONG_TOKEN,
                       "ig-app-secret", session.state):
            self.assertNotIn(secret, combined)
        flow_mod._sessions.pop(session_id, None)

    def test_invalid_state(self):
        from gaeclip.social import oauth_flow as flow_mod
        from gaeclip.social.states import AuthError

        with _env():
            with self.assertRaises(AuthError) as got:
                flow_mod.complete_by_state(FakeRedis(), "nope", code="x")
        self.assertEqual(got.exception.code, "auth_failed")

    def test_code_exchange_failure_stage(self):
        import urllib.error

        from gaeclip.social import oauth_flow as flow_mod
        from gaeclip.social.states import AuthError

        client = FakeRedis()
        session_id, session = self._session(flow_mod)

        def boom(request, timeout=30):
            raise urllib.error.HTTPError(
                request.full_url, 400, "Bad Request", {}, _Resp(b"{}"))

        with _env(), \
             patch("urllib.request.urlopen", side_effect=boom), \
             self.assertLogs("gaeclip.social.oauth_flow",
                             level="INFO") as logs:
            with self.assertRaises(AuthError):
                flow_mod.complete_by_state(client, session.state, code="c")
        combined = "\n".join(logs.output)
        self.assertIn("stage=exchange", combined)
        self.assertIn("instagram", combined)
        flow_mod._sessions.pop(session_id, None)

    def test_personal_account_rejected(self):
        from gaeclip.social import oauth_flow as flow_mod
        from gaeclip.social.states import AccountRequirementError

        client = FakeRedis()
        session_id, session = self._session(flow_mod)
        me = b'{"id": "9", "username": "personal", "account_type": "PERSONAL"}'
        with _env(), \
             patch("urllib.request.urlopen",
                   side_effect=self._token_urlopen(me_body=me)):
            with self.assertRaises(AccountRequirementError):
                flow_mod.complete_by_state(client, session.state, code="c")
        flow_mod._sessions.pop(session_id, None)

    def test_media_creator_accepted_personal_rejected(self):
        from gaeclip.social.publishers.instagram_login import (
            InstagramLoginPublisher)
        from gaeclip.social.states import AccountRequirementError

        # MEDIA_CREATOR is the live creator subtype: eligible.
        InstagramLoginPublisher._require_professional("MEDIA_CREATOR")
        InstagramLoginPublisher._require_professional("BUSINESS")
        # PERSONAL / empty / unknown: rejected (fail-closed).
        for bad in ("PERSONAL", "", "UNKNOWN_TYPE"):
            with self.assertRaises(AccountRequirementError):
                InstagramLoginPublisher._require_professional(bad)

    def test_discovery_failure_stage(self):
        from gaeclip.social import oauth_flow as flow_mod
        from gaeclip.social.states import SocialError

        client = FakeRedis()
        session_id, session = self._session(flow_mod)
        with _env(), \
             patch("urllib.request.urlopen",
                   side_effect=self._token_urlopen(me_body=b'{}')):
            with self.assertRaises(SocialError):
                flow_mod.complete_by_state(client, session.state, code="c")
        flow_mod._sessions.pop(session_id, None)


class ExchangeDiagnosticTests(unittest.TestCase):
    def test_missing_token_error_surfaced_safely(self):
        from gaeclip.social.publishers.instagram_login import (
            _safe_error_summary, exchange_code_for_short_lived)
        from gaeclip.social.states import AuthError

        body = (b'{"error_type": "OAuthException", "code": 400, '
                b'"error_message": "Matching code was not found"}')

        def fake(request, timeout=30):
            return _Resp(body)

        with patch("urllib.request.urlopen", side_effect=fake):
            with self.assertRaises(AuthError) as got:
                exchange_code_for_short_lived(
                    client_id="id", client_secret="s",
                    redirect_uri="https://x/cb", code="c")
        detail = str(got.exception)
        # Meta's reason is visible; no secret values leak.
        self.assertIn("Matching code was not found", detail)
        self.assertIn("OAuthException", detail)
        summary = _safe_error_summary({"access_token": "SECRET-TOKEN-XYZ",
                                       "data": [{"a": 1}]})
        self.assertNotIn("SECRET-TOKEN-XYZ", summary)
        self.assertIn("keys=", summary)


class ExchangeShapeTests(unittest.TestCase):
    def test_flat_response_shape_accepted(self):
        # Live Meta shape 2026-10-08: flat object, no data wrapper.
        from gaeclip.social.publishers.instagram_login import (
            exchange_code_for_short_lived)

        body = ('{"access_token": "SHORT-1", "user_id": "%s", '
                '"permissions": "instagram_business_basic"}' % IG_ID).encode()

        def fake(request, timeout=30):
            return _Resp(body)

        with patch("urllib.request.urlopen", side_effect=fake):
            out = exchange_code_for_short_lived(
                client_id="id", client_secret="s",
                redirect_uri="https://x/cb", code="c")
        self.assertEqual(out["access_token"], "SHORT-1")
        self.assertEqual(out["ig_user_id"], IG_ID)

    def test_wrapped_data_shape_accepted(self):
        from gaeclip.social.publishers.instagram_login import (
            exchange_code_for_short_lived)

        body = ('{"data": [{"access_token": "SHORT-2", "user_id": "%s", '
                '"permissions": "x"}]}' % IG_ID).encode()

        def fake(request, timeout=30):
            return _Resp(body)

        with patch("urllib.request.urlopen", side_effect=fake):
            out = exchange_code_for_short_lived(
                client_id="id", client_secret="s",
                redirect_uri="https://x/cb", code="c")
        self.assertEqual(out["access_token"], "SHORT-2")
        self.assertEqual(out["ig_user_id"], IG_ID)


class ArchitectureTests(unittest.TestCase):
    def test_publish_hits_only_instagram_hosts(self):
        from gaeclip.social.publishers.instagram_login import (
            InstagramLoginPublisher)

        hosts = []
        transfer_headers = {}

        def fake_urlopen(request, timeout=300):
            url = request.full_url
            hosts.append(url)
            if "media_publish" in url:
                # Universal Graph shape: params as query string.
                import urllib.parse as _up

                q = _up.parse_qs(_up.urlparse(url).query)
                self.assertIn("creation_id", q)
                self.assertIn("access_token", q)
                return _Resp(b'{"id": "media-7"}')
            if "/media?" in url and "media_type" in url:
                import urllib.parse as _up

                q = _up.parse_qs(_up.urlparse(url).query)
                self.assertEqual((q.get("media_type") or [""])[0], "REELS")
                self.assertEqual((q.get("upload_type") or [""])[0],
                                 "resumable")
                self.assertIn("caption", q)
                self.assertIn("access_token", q)
                return _Resp(b'{"id": "cont-1", "uri": '
                             b'"https://rupload.facebook.com/ig-api-upload/'
                             b'v21.0/cont-1"}')
            if "rupload.facebook.com" in url:
                # Documented transfer shape: session URL returned by Meta,
                # header auth + header offsets, whole file one POST.
                self.assertEqual(
                    url, "https://rupload.facebook.com/ig-api-upload/"
                         "v21.0/cont-1")
                self.assertNotIn("access_token", url)
                for name, _ in request.header_items():
                    transfer_headers[name.lower()] = request.get_header(name)
                self.assertTrue(
                    transfer_headers.get("authorization", "").startswith("OAuth "))
                self.assertEqual(transfer_headers.get("offset"), "0")
                self.assertEqual(transfer_headers.get("file_size"), "2048")
                self.assertTrue(request.data)
                return _Resp(b'{"success": true}')
            if "status_code" in url:
                return _Resp(b'{"id": "cont-1", "status_code": "FINISHED"}')
            raise AssertionError(f"unexpected publish URL {url}")

        stages = []
        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"v" * 2048)
            ctx = _ctx(clip, {"ig_user_id": IG_ID,
                              "login_flow": "instagram_login"}, stages=stages)
            with patch("gaeclip.social.publishers.instagram_login.validate_clip_file",
                       return_value=None), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                res = InstagramLoginPublisher().publish(ctx)
        self.assertEqual(res.remote_media_id, "media-7")
        self.assertTrue(hosts)
        for url in hosts:
            self.assertNotIn("graph.facebook.com", url)
            self.assertNotIn("www.facebook.com", url)
            self.assertNotIn("dialog/oauth", url)
            self.assertNotIn("me/accounts", url)
        self.assertTrue(any("graph.instagram.com" in u for u in hosts))
        emitted = [s for _, s in stages]
        self.assertIn("uploading", emitted)
        self.assertIn("finalizing", emitted)
        self.assertNotIn("uploaded", emitted)

    def test_legacy_account_refuses_without_network(self):
        from gaeclip.social.publishers.instagram_login import (
            InstagramLoginPublisher, is_legacy_instagram_account)
        from gaeclip.social.states import SocialError

        meta = {"facebook_page_id": "p", "page_token": "pt"}
        self.assertTrue(is_legacy_instagram_account(meta))
        self.assertFalse(is_legacy_instagram_account(
            {"login_flow": "instagram_login", "ig_user_id": "1"}))
        self.assertFalse(is_legacy_instagram_account({}))

        def boom(request, timeout=300):
            raise AssertionError("legacy must not touch network")

        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"v" * 64)
            ctx = _ctx(clip, meta)
            with patch("gaeclip.social.publishers.instagram_login.validate_clip_file",
                       return_value=None), \
                 patch("urllib.request.urlopen", side_effect=boom):
                with self.assertRaises(SocialError) as got:
                    InstagramLoginPublisher().publish(ctx)
        self.assertEqual(got.exception.code, "legacy_instagram_connection")
        self.assertFalse(getattr(got.exception, "retryable", True))

    def test_transfer_rejection_fails_fast(self):
        import urllib.error

        from gaeclip.social.publishers.instagram_login import (
            InstagramLoginPublisher)
        from gaeclip.social.states import SocialError, TransientError

        calls = []

        class FP:
            def read(self):
                return (b'{"error": {"message": "Permission denied", '
                        b'"code": 200, "type": "OAuthException"}}')

            def close(self):
                pass

            def info(self):
                return {}

        def fake_urlopen(request, timeout=300):
            calls.append(request.full_url)
            if "graph.instagram.com" in request.full_url:
                return _Resp(b'{"id": "cont-1", "uri": '
                             b'"https://rupload.facebook.com/ig-api-upload/'
                             b'v21.0/cont-1"}')
            raise urllib.error.HTTPError(request.full_url, 400,
                                         "Bad Request", {}, FP())

        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"v" * 64)
            ctx = _ctx(clip, {"ig_user_id": IG_ID,
                              "login_flow": "instagram_login"})
            with patch("gaeclip.social.publishers.instagram_login.validate_clip_file",
                       return_value=None), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaises(SocialError) as got:
                    InstagramLoginPublisher().publish(ctx)
        # Permission rejection is NOT transient: fail fast, no retry loop.
        self.assertNotIsInstance(got.exception, TransientError)
        self.assertFalse(getattr(got.exception, "retryable", True))

    def test_container_error_status(self):
        from gaeclip.social.publishers.instagram_login import (
            InstagramLoginPublisher)
        from gaeclip.social.states import SocialError

        def fake_urlopen(request, timeout=300):
            url = request.full_url
            if "/media?" in url and "media_type" in url:
                return _Resp(b'{"id": "cont-1", "uri": '
                             b'"https://rupload.facebook.com/ig-api-upload/'
                             b'v21.0/cont-1"}')
            if "rupload.facebook.com" in url:
                return _Resp(b'{"success": true}')
            if "status_code" in url:
                return _Resp(b'{"id": "cont-1", "status_code": "ERROR"}')
            raise AssertionError(url)

        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"v" * 64)
            ctx = _ctx(clip, {"ig_user_id": IG_ID,
                              "login_flow": "instagram_login"})
            with patch("gaeclip.social.publishers.instagram_login.validate_clip_file",
                       return_value=None), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaises(SocialError) as got:
                    InstagramLoginPublisher().publish(ctx)
        self.assertEqual(got.exception.code, "processing_failed")

    def test_container_falls_back_to_fb_host_once(self):
        from gaeclip.social.publishers.instagram_login import (
            FB_GRAPH, IG_GRAPH, InstagramLoginPublisher)
        from gaeclip.social.states import SocialError

        hosts = []

        def fake_call(method, path, token, params=None, host=IG_GRAPH):
            hosts.append(host)
            if path.endswith("/media") and params.get("upload_type") == "resumable":
                if host == IG_GRAPH:
                    raise SocialError(
                        'instagram graph 100: The parameter video_url is '
                        'required (code=100, type=IGApiException)',
                        code="provider_error",
                        user_message="Instagram mengembalikan error.")
                return {"id": "cont-fb",
                        "uri": "https://rupload.facebook.com/ig-api-upload/"
                               "v21.0/cont-fb"}
            if path.endswith("/media_publish"):
                return {"id": "media-fb"}
            return {"id": "cont-fb", "status_code": "FINISHED"}

        transfers = []

        def fake_urlopen(request, timeout=300):
            transfers.append(request.full_url)
            return _Resp(b'{"success": true}')

        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"v" * 64)
            ctx = _ctx(clip, {"ig_user_id": IG_ID,
                              "login_flow": "instagram_login"})
            with patch("gaeclip.social.publishers.instagram_login.validate_clip_file",
                       return_value=None), \
                 patch("gaeclip.social.publishers.instagram_login._IGraphClient.call",
                       side_effect=fake_call), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                res = InstagramLoginPublisher().publish(ctx)
        self.assertEqual(res.remote_media_id, "media-fb")
        # IG host tried first, FB host exactly once for the container.
        self.assertIn(IG_GRAPH, hosts)
        self.assertEqual(hosts.count(FB_GRAPH), 1)
        self.assertTrue(any("rupload.facebook.com" in u for u in transfers))

    def test_fb_host_auth_rejection_is_also_unsupported(self):
        # Live 2026-10-08: graph.facebook.com answers the IG-login token
        # with auth 190 (unparseable). That also terminates as
        # instagram_resumable_unsupported — reconnect would not help.
        from gaeclip.social.publishers.instagram_login import (
            FB_GRAPH, IG_GRAPH, InstagramLoginPublisher)
        from gaeclip.social.states import AuthError, SocialError

        def fake_call(method, path, token, params=None, host=IG_GRAPH):
            if host == IG_GRAPH:
                raise SocialError(
                    "instagram graph 100: video_url required (code=100)",
                    code="provider_error", user_message="x")
            raise AuthError("instagram graph 190: Cannot parse access token")

        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"v" * 64)
            ctx = _ctx(clip, {"ig_user_id": IG_ID,
                              "login_flow": "instagram_login"})
            with patch("gaeclip.social.publishers.instagram_login.validate_clip_file",
                       return_value=None), \
                 patch("gaeclip.social.publishers.instagram_login._IGraphClient.call",
                       side_effect=fake_call):
                with self.assertRaises(SocialError) as got:
                    InstagramLoginPublisher().publish(ctx)
        self.assertEqual(got.exception.code,
                         "instagram_resumable_unsupported")

    def test_both_hosts_reject_is_unsupported_not_transient(self):
        from gaeclip.social.publishers.instagram_login import (
            InstagramLoginPublisher)
        from gaeclip.social.states import SocialError, TransientError

        def boom(method, path, token, params=None, host=""):
            raise SocialError(
                'instagram graph 100: The parameter video_url is required '
                '(code=100, type=IGApiException)',
                code="provider_error",
                user_message="Instagram mengembalikan error.")

        def no_net(request, timeout=300):
            raise AssertionError("no transfer must be attempted")

        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"v" * 64)
            ctx = _ctx(clip, {"ig_user_id": IG_ID,
                              "login_flow": "instagram_login"})
            with patch("gaeclip.social.publishers.instagram_login.validate_clip_file",
                       return_value=None), \
                 patch("gaeclip.social.publishers.instagram_login._IGraphClient.call",
                       side_effect=boom), \
                 patch("urllib.request.urlopen", side_effect=no_net):
                with self.assertRaises(SocialError) as got:
                    InstagramLoginPublisher().publish(ctx)
        self.assertEqual(got.exception.code,
                         "instagram_resumable_unsupported")
        self.assertNotIsInstance(got.exception, TransientError)
        self.assertFalse(getattr(got.exception, "retryable", True))

    def test_video_url_path_needs_no_bytes_upload(self):
        from gaeclip.social.publishers.instagram_login import (
            InstagramLoginPublisher)

        seen_urls = []
        seen_bodies = []

        def fake_urlopen(request, timeout=300):
            url = request.full_url
            seen_urls.append(url)
            if "media_publish" in url:
                return _Resp(b'{"id": "media-url-1"}')
            if "/media?" in url and "video_url" in url:
                return _Resp(b'{"id": "cont-url"}')
            if "status_code" in url:
                return _Resp(b'{"id": "cont-url", "status_code": "FINISHED"}')
            raise AssertionError(f"unexpected publish URL {url}")

        stages = []
        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"v" * 64)
            ctx = _ctx(clip, {"ig_user_id": IG_ID,
                              "login_flow": "instagram_login",
                              "media_url": "https://cdn.example/v/clip.mp4"
                                           "?token=SECRET-TOKEN"},
                         stages=stages)
            with patch("gaeclip.social.publishers.instagram_login.validate_clip_file",
                       return_value=None), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertLogs("gaeclip.social.publishers.instagram_login",
                                     level="INFO") as logs:
                    res = InstagramLoginPublisher().publish(ctx)
        self.assertEqual(res.remote_media_id, "media-url-1")
        # No byte transfer attempted at all.
        self.assertFalse(any("rupload" in u for u in seen_urls))
        emitted = [s for _, s in stages]
        self.assertIn("finalizing", emitted)
        self.assertNotIn("uploaded", emitted)
        # The media URL value itself is never logged.
        self.assertNotIn("SECRET-TOKEN", "\n".join(logs.output))

    def test_early_poll_errors_tolerated_then_publishes(self):
        # A fresh container may briefly 400 ("nonexisting field") before
        # Meta materializes it — polling must ride through, then publish.
        from gaeclip.social.publishers.instagram_login import (
            InstagramLoginPublisher)
        from gaeclip.social.states import SocialError

        polls = {"n": 0}

        def fake_call(method, path, token, params=None, host=""):
            if path.endswith("/media") and (params or {}).get("video_url"):
                return {"id": "cont-race"}
            if path.endswith("/media_publish"):
                return {"id": "media-race"}
            polls["n"] += 1
            if polls["n"] <= 2:
                raise SocialError(
                    "instagram graph 100: Tried accessing nonexisting "
                    "field (status_code) (code=100)",
                    code="provider_error",
                    user_message="Instagram mengembalikan error.")
            return {"id": "cont-race", "status_code": "FINISHED"}

        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"v" * 64)
            ctx = _ctx(clip, {"ig_user_id": IG_ID,
                              "login_flow": "instagram_login",
                              "media_url": "https://cdn.example/v.mp4"})
            with patch("gaeclip.social.publishers.instagram_login.validate_clip_file",
                       return_value=None), \
                 patch("gaeclip.social.publishers.instagram_login._IGraphClient.call",
                       side_effect=fake_call), \
                 patch("gaeclip.social.publishers.instagram_login.time.sleep",
                       return_value=None):
                res = InstagramLoginPublisher().publish(ctx)
        self.assertEqual(res.remote_media_id, "media-race")
        self.assertGreaterEqual(polls["n"], 3)

    def test_persistent_poll_errors_fail_fast(self):
        from gaeclip.social.publishers.instagram_login import (
            InstagramLoginPublisher)
        from gaeclip.social.states import SocialError

        def always_bad(method, path, token, params=None, host=""):
            if (params or {}).get("video_url"):
                return {"id": "cont-dead"}
            if path.endswith("/media_publish"):
                return {"id": "media-dead"}
            raise SocialError("instagram graph 100: nope (code=100)",
                              code="provider_error",
                              user_message="Instagram mengembalikan error.")

        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"v" * 64)
            ctx = _ctx(clip, {"ig_user_id": IG_ID,
                              "login_flow": "instagram_login",
                              "media_url": "https://cdn.example/v.mp4"})
            with patch("gaeclip.social.publishers.instagram_login.validate_clip_file",
                       return_value=None), \
                 patch("gaeclip.social.publishers.instagram_login._IGraphClient.call",
                       side_effect=always_bad), \
                 patch("gaeclip.social.publishers.instagram_login.time.sleep",
                       return_value=None):
                with self.assertRaises(SocialError) as got:
                    InstagramLoginPublisher().publish(ctx)
        self.assertEqual(got.exception.code, "provider_error")

    def test_status_falls_back_to_media_read(self):
        # A published MEDIA id has no status_code (container field).
        # The status check must fall back to a media-shaped read instead
        # of failing the whole publish after a successful media_publish.
        from gaeclip.social.publishers.instagram_login import (
            InstagramLoginPublisher)
        from gaeclip.social.states import PUBLISHED, SocialError

        calls = []

        def fake_call(method, path, token, params=None, host=""):
            calls.append((path, (params or {}).get("fields", "")))
            if (params or {}).get("fields", "") == "id,status_code":
                raise SocialError(
                    "instagram graph 100: Tried accessing nonexisting "
                    "field (status_code) (code=100)",
                    code="provider_error",
                    user_message="Instagram mengembalikan error.")
            return {"id": "media-9", "media_type": "VIDEO"}

        pub = InstagramLoginPublisher()
        with patch("gaeclip.social.publishers.instagram_login._IGraphClient.call",
                   side_effect=fake_call):
            self.assertEqual(pub.get_publish_status("tok", "media-9"),
                             PUBLISHED)
        self.assertIn(("media-9", "id,media_type,timestamp"),
                      [(p, f) for p, f in calls])

    def test_status_mapping(self):
        from gaeclip.social.publishers.instagram_login import (
            InstagramLoginPublisher)
        from gaeclip.social.states import FAILED, PROCESSING, PUBLISHED

        pub = InstagramLoginPublisher()
        bodies = {"IN_PROGRESS": PROCESSING, "ERROR": FAILED,
                  "EXPIRED": FAILED, "FINISHED": PUBLISHED}
        for status_code, expected in bodies.items():
            with patch("gaeclip.social.publishers.instagram_login._IGraphClient.call",
                       return_value={"status_code": status_code}):
                self.assertEqual(pub.get_publish_status("tok", "m"), expected)


class RefreshTests(unittest.TestCase):
    def test_service_refresh_uses_instagram_endpoint(self):
        import types

        from gaeclip.social.service import SocialPublishService

        svc = SocialPublishService.__new__(SocialPublishService)
        import time as _t

        acct = types.SimpleNamespace(
            id="acc-ig", provider="instagram", token_reference="ref-ig",
            meta={}, expires_at=0.0)
        svc.accounts = types.SimpleNamespace(
            save=lambda a: setattr(svc, "_saved", a))
        svc.secrets = types.SimpleNamespace(
            get=lambda _ref: {"access_token": "old-long",
                              "expires_at": _t.time() - 10,
                              "scopes": []},
            put=lambda _ref, creds: setattr(svc, "_put", creds))

        with patch("gaeclip.social.publishers.instagram_login.refresh_long_lived",
                   return_value={"access_token": "new-long",
                                 "expires_in": 5184000}) as mock:
            self.assertTrue(svc._try_refresh_instagram(acct))
        mock.assert_called_once_with("old-long")
        self.assertEqual(svc._put["access_token"], "new-long")
        self.assertGreater(svc._put["expires_at"], _t.time() + 50 * 86400)

    def test_service_refresh_failure_false(self):
        import types

        from gaeclip.social.service import SocialPublishService

        svc = SocialPublishService.__new__(SocialPublishService)
        acct = types.SimpleNamespace(
            id="acc-ig", provider="instagram", token_reference="ref-ig",
            meta={}, expires_at=0.0)
        svc.accounts = types.SimpleNamespace(save=lambda a: None)
        svc.secrets = types.SimpleNamespace(
            get=lambda _ref: {"access_token": "old-long"},
            put=lambda _ref, creds: None)
        with patch("gaeclip.social.publishers.instagram_login.refresh_long_lived",
                   side_effect=Exception("expired")):
            self.assertFalse(svc._try_refresh_instagram(acct))


if __name__ == "__main__":
    unittest.main()
