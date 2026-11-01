"""Social publishing tests (spec §46, §48, §49).

No network, no real credentials. Fakes cover all four providers; the
OAuth loopback listener is exercised for real against 127.0.0.1.
"""

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, "tests")

from test_video_metadata import FakeRedis


def make_service(runtime="desktop"):
    from gaeclip.social.service import SocialPublishService

    return SocialPublishService(FakeRedis(), runtime=runtime)


class FakeSuccessPublisher:
    provider = "youtube"

    def __init__(self):
        from gaeclip.social.states import ProviderCapabilities

        self.capabilities = ProviderCapabilities(direct_publish=True)
        self.calls = []

    def account_info(self, access_token):
        from gaeclip.social.publishers.base import AccountInfo

        return AccountInfo(external_account_id="ext-1", display_name="Chan")

    def validate_account(self, access_token, account_meta):
        return None

    def publish(self, ctx):
        from gaeclip.social.states import PublishResult

        self.calls.append(ctx)
        return PublishResult(status="published", remote_media_id="vid-1",
                             remote_url="https://youtu.be/vid-1")


class FakeFailPublisher(FakeSuccessPublisher):
    provider = "instagram"

    def __init__(self):
        super().__init__()
        from gaeclip.social.states import ProviderCapabilities

        self.capabilities = ProviderCapabilities(direct_publish=True)

    def publish(self, ctx):
        from gaeclip.social.states import PermissionError

        raise PermissionError("no permission")


class FakeFlakyPublisher(FakeSuccessPublisher):
    provider = "tiktok"

    def __init__(self, failures=2):
        super().__init__()
        self.failures = failures

    def publish(self, ctx):
        from gaeclip.social.states import PublishResult, TransientError

        if self.failures > 0:
            self.failures -= 1
            raise TransientError("boom")
        return PublishResult(status="published", remote_media_id="vid-9")


def _account(service, provider="youtube", account_id="acc-1"):
    from gaeclip.social.accounts import SocialAccount

    account = SocialAccount(id=account_id, provider=provider,
                            external_account_id="ext-1", display_name="Chan",
                            status="connected", token_reference="ref-1")
    service.accounts.save(account)
    service.secrets.put("ref-1", {"access_token": "tok-abc",
                                  "expires_at": time.time() + 3600})
    return account


def _job(service, provider="youtube", account_id="acc-1", status="pending"):
    from gaeclip.social.accounts import SocialPublishJob

    job = SocialPublishJob(id=f"spub-{provider}-1", clip_id="c000001",
                            job_id="job-1", provider=provider,
                            social_account_id=account_id, status=status,
                            title="Hook Title", idempotency_key=f"k-{provider}")
    return service.jobs.save_job(job)


class ProviderAbstractionTests(unittest.TestCase):
    def test_registry_has_four_honest_providers(self):
        from gaeclip.social.service import provider_capabilities

        caps = provider_capabilities()
        self.assertEqual(set(caps), {"youtube", "tiktok", "instagram", "facebook"})
        self.assertTrue(caps["youtube"]["direct_publish"])
        self.assertFalse(caps["tiktok"]["direct_publish"])
        self.assertTrue(caps["tiktok"]["approval_note"])

    def test_unknown_provider_rejected(self):
        from gaeclip.social.service import publisher_for
        from gaeclip.social.states import SocialError

        with self.assertRaises(SocialError):
            publisher_for("myspace")


class AccountTests(unittest.TestCase):
    def test_create_and_disconnect(self):
        service = make_service()
        account = _account(service)
        self.assertEqual(service.accounts.get(account.id).display_name, "Chan")
        result = service.disconnect(account.id)
        self.assertEqual(result["disconnected"], account.id)
        self.assertIsNone(service.accounts.get(account.id))
        self.assertIsNone(service.secrets.get("ref-1"))

    def test_disconnect_missing(self):
        from gaeclip.social.states import SocialError

        with self.assertRaises(SocialError):
            make_service().disconnect("nope")


class StateMachineTests(unittest.TestCase):
    def test_legal_transitions(self):
        from gaeclip.social.states import can_transition

        self.assertTrue(can_transition("pending", "uploading"))
        self.assertTrue(can_transition("failed", "pending"))
        self.assertFalse(can_transition("published", "pending"))
        self.assertFalse(can_transition("pending", "published"))

    def test_retry_policy_bounded(self):
        from gaeclip.social.states import RetryPolicy

        policy = RetryPolicy(max_attempts=5, base_delay_seconds=2.0,
                             max_delay_seconds=120.0)
        self.assertEqual(policy.delay_for(1), 2.0)
        self.assertEqual(policy.delay_for(3), 8.0)
        self.assertEqual(policy.delay_for(99), 120.0)

    def test_http_classification(self):
        from gaeclip.social.states import (AuthError, PermissionError,
                                           TransientError, classify_http_status)

        self.assertIsInstance(classify_http_status(429), TransientError)
        self.assertIsInstance(classify_http_status(503), TransientError)
        self.assertIsInstance(classify_http_status(401), AuthError)
        self.assertIsInstance(classify_http_status(403), PermissionError)
        self.assertFalse(classify_http_status(400).retryable)


class PublishFlowTests(unittest.TestCase):
    def _patched_service(self, fakes):
        service = make_service()
        patcher = patch("gaeclip.social.service.publisher_for",
                        side_effect=lambda provider, **kw: fakes[provider])
        patcher.start()
        self.addCleanup(patcher.stop)
        return service

    def test_success_end_to_end(self):
        service = self._patched_service({"youtube": FakeSuccessPublisher()})
        _account(service)
        job = _job(service)
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "c000001.mp4"
            clip.write_bytes(b"\x00" * 2048)
            with patch("gaeclip.social.service.clip_file_path", return_value=clip), \
                 patch("gaeclip.media.verify_clip",
                       return_value={"duration": "30.0"}), \
                 patch("gaeclip.media.probe",
                       return_value={"width": "1080", "height": "1920",
                                     "duration": "30.0"}):
                out = service.execute(job.id)
        self.assertEqual(out.status, "published")
        self.assertEqual(out.remote_media_id, "vid-1")

    def test_independent_platform_statuses(self):
        # 3/4 overall: youtube ok, tiktok ok, instagram fails, facebook ok.
        fakes = {"youtube": FakeSuccessPublisher(),
                 "tiktok": FakeFlakyPublisher(failures=0),
                 "instagram": FakeFailPublisher(),
                 "facebook": FakeSuccessPublisher()}
        service = self._patched_service(fakes)
        results = {}
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "c000001.mp4"
            clip.write_bytes(b"\x00" * 2048)
            with patch("gaeclip.social.service.clip_file_path", return_value=clip), \
                 patch("gaeclip.media.verify_clip",
                       return_value={"duration": "30.0"}), \
                 patch("gaeclip.media.probe",
                       return_value={"width": "1080", "height": "1920",
                                     "duration": "30.0"}):
                for provider in ("youtube", "tiktok", "instagram", "facebook"):
                    _account(service, provider, f"acc-{provider}")
                    job = _job(service, provider, f"acc-{provider}")
                    results[provider] = service.execute(job.id).status
        self.assertEqual(results, {"youtube": "published", "tiktok": "published",
                                   "instagram": "failed", "facebook": "published"})

    def test_transient_retries_then_succeeds(self):
        from gaeclip.social.states import RetryPolicy

        service = self._patched_service({"tiktok": FakeFlakyPublisher(failures=2)})
        service.retry = RetryPolicy(max_attempts=5, base_delay_seconds=0.01,
                                    max_delay_seconds=0.05)
        _account(service, "tiktok", "acc-tiktok")
        job = _job(service, "tiktok", "acc-tiktok")
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "c000001.mp4"
            clip.write_bytes(b"\x00" * 2048)
            with patch("gaeclip.social.service.clip_file_path", return_value=clip), \
                 patch("gaeclip.media.verify_clip",
                       return_value={"duration": "30.0"}), \
                 patch("gaeclip.media.probe",
                       return_value={"width": "1080", "height": "1920",
                                     "duration": "30.0"}):
                out = service.execute(job.id)
        self.assertEqual(out.status, "published")
        self.assertEqual(len(service.jobs.list_attempts(job.id)), 3)

    def test_non_retryable_fails_fast(self):
        service = self._patched_service({"instagram": FakeFailPublisher()})
        _account(service, "instagram", "acc-instagram")
        job = _job(service, "instagram", "acc-instagram")
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "c000001.mp4"
            clip.write_bytes(b"\x00" * 2048)
            with patch("gaeclip.social.service.clip_file_path", return_value=clip), \
                 patch("gaeclip.media.verify_clip",
                       return_value={"duration": "30.0"}), \
                 patch("gaeclip.media.probe",
                       return_value={"width": "1080", "height": "1920",
                                     "duration": "30.0"}):
                out = service.execute(job.id)
        self.assertEqual(out.status, "failed")
        self.assertEqual(len(service.jobs.list_attempts(job.id)), 1)

    def test_idempotency_no_duplicate_upload(self):
        service = make_service()
        _account(service)
        with patch.object(service, "enqueue", return_value="t"):
            first = service.create_publish(clip_id="c1", job_id="j1",
                                           provider="youtube", account_id="acc-1",
                                           title="Hook")
            second = service.create_publish(clip_id="c1", job_id="j1",
                                            provider="youtube", account_id="acc-1",
                                            title="Hook")
        self.assertEqual(first.id, second.id)

    def test_expired_token_marks_reconnect(self):
        service = self._patched_service({"youtube": FakeSuccessPublisher()})
        _account(service)
        service.secrets.put("ref-1", {"access_token": "old",
                                      "expires_at": time.time() - 10})
        job = _job(service)
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "c000001.mp4"
            clip.write_bytes(b"\x00" * 2048)
            with patch("gaeclip.social.service.clip_file_path", return_value=clip), \
                 patch("gaeclip.media.verify_clip",
                       return_value={"duration": "30.0"}), \
                 patch("gaeclip.media.probe",
                       return_value={"width": "1080", "height": "1920",
                                     "duration": "30.0"}):
                out = service.execute(job.id)
        self.assertEqual(out.status, "failed")
        self.assertEqual(service.accounts.get("acc-1").status, "needs_reconnect")

    def test_cancel(self):
        service = make_service()
        _account(service)
        job = _job(service, status="uploading")
        out = service.cancel_job(job.id)
        self.assertEqual(out.status, "cancelled")

    def test_retry_failed_job(self):
        service = make_service()
        _account(service)
        job = _job(service, status="failed")
        with patch.object(service, "enqueue", return_value="t"):
            out = service.retry_job(job.id)
        self.assertEqual(out.status, "pending")


class RealBaseHelpersTests(unittest.TestCase):
    """Exercise the REAL base helpers (no fakes): relative imports and
    validation must work on the true code path (regression: publishers.base
    used '.states' which only exists as '..states' — every real publish
    died with Kesalahan internal)."""

    def test_validate_clip_file_real_path(self):
        import tempfile
        from pathlib import Path

        from gaeclip.social.publishers.base import validate_clip_file

        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "c000001.mp4"
            clip.write_bytes(b"\x00" * 2048)
            with patch("gaeclip.media.verify_clip",
                       return_value={"duration": "30.0"}), \
                 patch("gaeclip.media.probe",
                       return_value={"width": "1080", "height": "1920",
                                     "duration": "30.0"}):
                info = validate_clip_file(clip, max_bytes=10 * 1024 * 1024)
        self.assertEqual(info["width"], 1080)
        self.assertEqual(info["duration"], 30.0)

    def test_validate_clip_file_missing(self):
        from gaeclip.social.publishers.base import validate_clip_file
        from gaeclip.social.states import MediaError

        with self.assertRaises(MediaError):
            validate_clip_file(Path("/nonexistent/c.mp4"), max_bytes=1024)

    def test_check_cancelled_raises_social_error(self):
        from gaeclip.social.publishers.base import PublishContext
        from gaeclip.social.states import PublishRequest, SocialError

        ctx = PublishContext(request=PublishRequest(clip_id="c", job_id="j",
                                                    provider="youtube",
                                                    account_id="a"),
                             clip_path=Path("/tmp/x.mp4"),
                             should_cancel=lambda: True)
        with self.assertRaises(SocialError):
            ctx.check_cancelled()


class SecretsTests(unittest.TestCase):
    def test_server_store_roundtrip_encrypted(self):
        from gaeclip.social.secrets import ServerSecretStore

        store = ServerSecretStore(FakeRedis())
        store.put("ref-x", {"access_token": "tok-secret-123",
                            "refresh_token": "ref-secret-456"})
        self.assertEqual(store.get("ref-x")["access_token"], "tok-secret-123")

    def test_stored_blob_is_not_plaintext(self):
        from gaeclip.social.secrets import ServerSecretStore

        fake = FakeRedis()
        ServerSecretStore(fake).put("ref-y", {"access_token": "tok-secret-123"})
        stored = fake.get("gaeclip:social:secret:ref-y")
        text = stored.decode() if isinstance(stored, bytes) else str(stored)
        self.assertNotIn("tok-secret-123", text)

    def test_windows_store_fernet_fallback_on_linux(self):
        from gaeclip.social.secrets import ServerSecretStore, WindowsSecretStore

        fake = FakeRedis()
        store = WindowsSecretStore(fake, fallback=ServerSecretStore(fake))
        store.put("ref-z", {"access_token": "tok-abc"})
        self.assertEqual(store.get("ref-z")["access_token"], "tok-abc")

    def test_no_secrets_in_api_shapes(self):
        from gaeclip.social.accounts import SocialAccount, SocialPublishJob
        from gaeclip.social.api import _account_public, _job_public

        account = SocialAccount(id="a", provider="youtube",
                                token_reference="ref-with-secrets-inside")
        job = SocialPublishJob(id="j", clip_id="c", error_message="user text")
        blob = json.dumps({"a": _account_public(account), "j": _job_public(job)})
        for needle in ("tok-secret", "refresh_token", "access_token",
                       "authorization_code", "client_secret"):
            self.assertNotIn(needle, blob)

    def test_no_hardcoded_listener_or_leaks_in_sources(self):
        import inspect
        import pathlib

        from gaeclip.social import oauth as oauth_mod

        # The secure default must stay loopback + ephemeral.
        self.assertEqual(oauth_mod.bind_loopback.__defaults__, ("127.0.0.1", 0))
        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "gaeclip" / "social"
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            # No wildcard listener binds (doc mentions of 0.0.0.0 are fine).
            self.assertNotIn('("0.0.0.0"', text, path.name)
            self.assertNotIn("('0.0.0.0'", text, path.name)
            self.assertNotIn("print(", text.replace("eprintln", ""), path.name)

    def test_oauth_bind_defaults_and_overrides(self):
        from gaeclip.social import oauth_flow as flow_mod

        service_client = FakeRedis()
        host, port = flow_mod._oauth_bind(service_client)
        self.assertEqual((host, port), ("127.0.0.1", 0))
        with patch.dict("os.environ", {"SOCIAL_OAUTH_HOST": "0.0.0.0",
                                       "SOCIAL_OAUTH_PORT": "8765"}):
            host, port = flow_mod._oauth_bind(service_client)
            self.assertEqual((host, port), ("0.0.0.0", 8765))


class ApiRouteTests(unittest.TestCase):
    def _app(self):
        from unittest.mock import MagicMock, patch

        from fastapi.testclient import TestClient

        import gaeclip.api as api_mod

        from test_video_metadata import FakeRedis

        fake = FakeRedis()
        redis_mock = MagicMock()
        redis_mock.from_url.return_value = fake
        with patch("redis.Redis", redis_mock):
            app = api_mod.create_app()
        return TestClient(app), fake

    def test_providers_capabilities_endpoint(self):
        client, _ = self._app()
        body = client.get("/api/v1/social/providers").json()
        self.assertEqual(set(body["providers"]),
                         {"youtube", "tiktok", "instagram", "facebook"})

    def test_accounts_empty_then_validation(self):
        client, _ = self._app()
        self.assertEqual(client.get("/api/v1/social/accounts").json()["accounts"], [])
        res = client.post("/api/v1/social/accounts/connect", json={"provider": "myspace"})
        self.assertEqual(res.status_code, 422)
        res = client.post("/api/v1/social/accounts/connect", json={"provider": "youtube"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["detail"]["code"], "oauth_not_configured")

    def test_publish_flow_via_api(self):
        from gaeclip.social.accounts import SocialAccount

        client, fake = self._app()
        from gaeclip.social.accounts import SocialAccountStore

        account = SocialAccount(id="acc-9", provider="youtube",
                                external_account_id="ext-9", display_name="C9",
                                status="connected", token_reference="ref-9")
        SocialAccountStore(fake).save(account)
        with patch("gaeclip.social.service.SocialPublishService.enqueue",
                   return_value="t"):
            res = client.post("/api/v1/social/publish", json={
                "clip_id": "c000001", "job_id": "job-9", "provider": "youtube",
                "account_id": "acc-9", "title": "Hook Title"})
        self.assertEqual(res.status_code, 202, res.text)
        job_id = res.json()["id"]
        listed = client.get("/api/v1/social/publish",
                            params={"clip_id": "c000001"}).json()["jobs"]
        self.assertEqual(len(listed), 1)
        detail = client.get(f"/api/v1/social/publish/{job_id}").json()
        self.assertEqual(detail["status"], "pending")
        cancelled = client.post(f"/api/v1/social/publish/{job_id}/cancel").json()
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(client.get("/api/v1/social/publish/nope").status_code, 404)
        self.assertEqual(client.post("/api/v1/social/publish/nope/retry").status_code, 404)

    def test_disconnect_missing(self):
        client, _ = self._app()
        res = client.delete("/api/v1/social/accounts/nope")
        self.assertEqual(res.status_code, 404)

    def test_connect_returns_url_synchronously_with_redirect(self):
        import os

        client, _ = self._app()
        with patch.dict(os.environ, {"SOCIAL_YOUTUBE_CLIENT_ID": "cid-123"}):
            res = client.post("/api/v1/social/accounts/connect", json={
                "provider": "youtube",
                "redirect_uri": "http://127.0.0.1:3000/oauth/callback"})
        self.assertEqual(res.status_code, 202, res.text)
        body = res.json()
        self.assertIn("session_id", body)
        # No listener thread needed: URL is immediate.
        self.assertIn("accounts.google.com", body["authorization_url"])
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A3000%2Foauth%2Fcallback",
                      body["authorization_url"])

    def test_connect_rejects_non_loopback_redirect(self):
        import os

        client, _ = self._app()
        with patch.dict(os.environ, {"SOCIAL_YOUTUBE_CLIENT_ID": "cid-123"}):
            res = client.post("/api/v1/social/accounts/connect", json={
                "provider": "youtube",
                "redirect_uri": "https://evil.example/oauth/callback"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["detail"]["code"], "invalid_input")

    def test_connect_accepts_allowlisted_tunnel_redirect(self):
        import os

        ngrok = "https://2b4d-test.ngrok-free.app"
        client, _ = self._app()
        with patch.dict(os.environ, {"SOCIAL_YOUTUBE_CLIENT_ID": "cid-123",
                                     "FRONTEND_ORIGINS":
                                     "http://127.0.0.1:3000,"
                                     f"{ngrok}"}):
            res = client.post("/api/v1/social/accounts/connect", json={
                "provider": "youtube",
                "redirect_uri": f"{ngrok}/oauth/callback"})
        self.assertEqual(res.status_code, 202, res.text)
        self.assertIn("redirect_uri=https%3A%2F%2F2b4d-test.ngrok-free.app"
                      "%2Foauth%2Fcallback",
                      res.json()["authorization_url"])

    def test_connect_rejects_tunnel_wrong_path(self):
        import os

        ngrok = "https://2b4d-test.ngrok-free.app"
        client, _ = self._app()
        with patch.dict(os.environ, {"SOCIAL_YOUTUBE_CLIENT_ID": "cid-123",
                                     "FRONTEND_ORIGINS": ngrok}):
            res = client.post("/api/v1/social/accounts/connect", json={
                "provider": "youtube",
                "redirect_uri": f"{ngrok}/other/path"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["detail"]["code"], "invalid_input")

    def test_oauth_callback_completes_account(self):
        import os

        from gaeclip.social import oauth_flow as flow_mod

        client, fake = self._app()
        with patch.dict(os.environ, {"SOCIAL_YOUTUBE_CLIENT_ID": "cid-123",
                                     "SETTINGS_SECRET": "dGVzdHNlY3JldGtleXRlc3RzMTIzNDU2Nzg="}):
            started = client.post("/api/v1/social/accounts/connect", json={
                "provider": "youtube",
                "redirect_uri": "http://127.0.0.1:3000/oauth/callback"}).json()
            # Simulate Google landing on the frontend callback route.
            with patch("gaeclip.social.oauth.exchange_code",
                       return_value={"access_token": "tok-new",
                                     "refresh_token": "ref-new",
                                     "expires_in": 3600}), \
                 patch("gaeclip.social.service.publisher_for",
                       return_value=FakeSuccessPublisher()):
                res = client.post("/api/v1/social/oauth/callback", json={
                    "state": self._state_of(started["session_id"], flow_mod),
                    "code": "authcode-123"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["status"], "complete")
        listed = client.get("/api/v1/social/accounts",
                            params={"provider": "youtube"}).json()["accounts"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["status"], "connected")

    @staticmethod
    def _state_of(session_id, flow_mod):
        return flow_mod._sessions[session_id]["oauth"].state



    def test_meta_redirect_rewritten_to_localhost(self):
        import os
        import urllib.parse

        client, _ = self._app()
        # Facebook (Facebook Login) and Instagram (Instagram Login) are
        # independent connections with independent credentials; both
        # rewrite loopback redirects to the localhost hostname.
        env = {"SOCIAL_META_APP_ID": "meta-id",
               "SOCIAL_INSTAGRAM_APP_ID": "ig-app-id"}
        with patch.dict(os.environ, env):
            for provider in ("instagram", "facebook"):
                res = client.post("/api/v1/social/accounts/connect", json={
                    "provider": provider,
                    "redirect_uri": "http://127.0.0.1:3000/oauth/callback"})
                self.assertEqual(res.status_code, 202, res.text)
                parsed = urllib.parse.urlparse(res.json()["authorization_url"])
                params = urllib.parse.parse_qs(parsed.query)
                # Authorize URL carries the rewritten redirect (decoded form).
                self.assertEqual(params.get("redirect_uri"),
                                 ["http://localhost:3000/oauth/callback"], provider)
                if provider == "instagram":
                    # Instagram launches Instagram Login, never Facebook Login.
                    self.assertIn("instagram.com", parsed.netloc, provider)
                    self.assertNotIn("facebook.com", parsed.netloc, provider)
                else:
                    self.assertIn("facebook.com", parsed.netloc, provider)

    def test_youtube_tiktok_redirect_host_untouched(self):
        import os
        import urllib.parse

        client, _ = self._app()
        with patch.dict(os.environ, {"SOCIAL_YOUTUBE_CLIENT_ID": "cid-123",
                                     "SOCIAL_TIKTOK_CLIENT_KEY": "tk-123"}):
            for provider in ("youtube", "tiktok"):
                res = client.post("/api/v1/social/accounts/connect", json={
                    "provider": provider,
                    "redirect_uri": "http://127.0.0.1:3000/oauth/callback"})
                self.assertEqual(res.status_code, 202, res.text)
                query = urllib.parse.urlparse(
                    res.json().get("authorization_url", "")).query
                params = urllib.parse.parse_qs(query)
                self.assertEqual(params.get("redirect_uri"),
                                 ["http://127.0.0.1:3000/oauth/callback"], provider)

    def test_meta_exchange_uses_localhost_redirect(self):
        import os

        from gaeclip.social import oauth_flow as flow_mod

        client, _ = self._app()
        seen: dict = {}
        real_exchange = flow_mod._oauth.exchange_code

        def capture(**kwargs):
            seen.update(kwargs)
            return {"access_token": "tok-meta", "expires_in": 3600}

        with patch.dict(os.environ, {"SOCIAL_META_APP_ID": "meta-id",
                                     "SETTINGS_SECRET": "dGVzdHNlY3JldGtleXRlc3RzMTIzNDU2Nzg="}):
            started = client.post("/api/v1/social/accounts/connect", json={
                "provider": "facebook",
                "redirect_uri": "http://127.0.0.1:3000/oauth/callback"}).json()
            with patch("gaeclip.social.oauth.exchange_code",
                       side_effect=capture), \
                 patch("gaeclip.social.oauth_flow._exchange_fb_long_lived",
                       side_effect=lambda url, resp, cid, sec: resp), \
                 patch("gaeclip.social.service.publisher_for",
                       return_value=FakeSuccessPublisher()):
                res = client.post("/api/v1/social/oauth/callback", json={
                    "state": self._state_of(started["session_id"], flow_mod),
                    "code": "authcode-meta"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(seen.get("redirect_uri"),
                         "http://localhost:3000/oauth/callback")
        self.assertTrue(real_exchange)  # silence unused-import lint

    def test_facebook_connect_upgrades_to_long_lived(self):
        import os
        import time

        from gaeclip.social import oauth_flow as flow_mod

        client, _ = self._app()
        seen_tokens = []
        stored = {}

        class FBPages:
            def account_info(self, access_token):
                from gaeclip.social.publishers.base import AccountInfo

                seen_tokens.append(access_token)
                return AccountInfo(external_account_id="page-9",
                                   display_name="P9",
                                   extra={"page_token": "page-tok-9"})

            def validate_account(self, access_token, account_meta):
                return None

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return (b'{"access_token": "fb-long-lived", '
                        b'"token_type": "bearer", "expires_in": 5184000}')

        def fake_urlopen(request, timeout=30):
            self.assertIn("fb_exchange_token", request.full_url)
            self.assertIn("grant_type=fb_exchange_token",
                          request.full_url)
            return Resp()

        real_put = None
        with patch.dict(os.environ, {"SOCIAL_META_APP_ID": "meta-id",
                                     "SOCIAL_META_APP_SECRET": "meta-secret",
                                     "SETTINGS_SECRET": "dGVzdHNlY3JldGtleXRlc3RzMTIzNDU2Nzg="}):
            started = client.post("/api/v1/social/accounts/connect", json={
                "provider": "facebook",
                "redirect_uri": "http://127.0.0.1:3000/oauth/callback"}).json()
            with patch("gaeclip.social.oauth.exchange_code",
                        return_value={"access_token": "fb-short",
                                      "expires_in": 7200}), \
                 patch("urllib.request.urlopen",
                       side_effect=fake_urlopen), \
                 patch("gaeclip.social.service.publisher_for",
                        return_value=FBPages()), \
                 patch("gaeclip.social.secrets.ServerSecretStore.put",
                       autospec=True) as mock_put:
                res = client.post("/api/v1/social/oauth/callback", json={
                    "state": self._state_of(started["session_id"], flow_mod),
                    "code": "authcode-fb"})
        self.assertEqual(res.status_code, 200, res.text)
        # Discovery ran with the LONG-lived token, and it is what got stored.
        self.assertEqual(seen_tokens, ["fb-long-lived"])
        _, _, creds = mock_put.call_args[0]
        self.assertEqual(creds["access_token"], "fb-long-lived")
        self.assertGreater(creds["expires_at"], time.time() + 50 * 86400)

    def test_facebook_long_lived_unreachable_keeps_short_token(self):
        import os
        import urllib.error

        from gaeclip.social import oauth_flow as flow_mod

        client, _ = self._app()

        def boom(request, timeout=30):
            raise OSError("down")

        with patch.dict(os.environ, {"SOCIAL_META_APP_ID": "meta-id",
                                     "SOCIAL_META_APP_SECRET": "meta-secret",
                                     "SETTINGS_SECRET": "dGVzdHNlY3JldGtleXRlc3RzMTIzNDU2Nzg="}):
            started = client.post("/api/v1/social/accounts/connect", json={
                "provider": "facebook",
                "redirect_uri": "http://127.0.0.1:3000/oauth/callback"}).json()
            with patch("gaeclip.social.oauth.exchange_code",
                        return_value={"access_token": "fb-short",
                                      "expires_in": 7200}), \
                 patch("urllib.request.urlopen", side_effect=boom), \
                 patch("gaeclip.social.service.publisher_for",
                        return_value=FakeSuccessPublisher()):
                res = client.post("/api/v1/social/oauth/callback", json={
                    "state": self._state_of(started["session_id"], flow_mod),
                    "code": "authcode-fb"})
        # Connect still succeeds (today's behavior) instead of failing.
        self.assertEqual(res.status_code, 200, res.text)

    def test_apply_redirect_host_unit(self):
        from gaeclip.social.oauth import apply_redirect_host
        from gaeclip.social.publishers.meta import META_OAUTH
        from gaeclip.social.publishers.tiktok import TIKTOK_OAUTH
        from gaeclip.social.publishers.youtube import YOUTUBE_OAUTH

        self.assertEqual(
            apply_redirect_host("http://127.0.0.1:3000/oauth/callback", META_OAUTH),
            "http://localhost:3000/oauth/callback")
        self.assertEqual(
            apply_redirect_host("http://localhost:3000/oauth/callback", META_OAUTH),
            "http://localhost:3000/oauth/callback")
        # Non-loopback input is never rewritten (open-redirector guard).
        self.assertEqual(
            apply_redirect_host("https://evil.example/oauth/callback", META_OAUTH),
            "https://evil.example/oauth/callback")
        # Other providers: byte-identical passthrough.
        for cfg in (YOUTUBE_OAUTH, TIKTOK_OAUTH):
            self.assertEqual(
                apply_redirect_host("http://127.0.0.1:3000/oauth/callback", cfg),
                "http://127.0.0.1:3000/oauth/callback")
        self.assertIsNone(YOUTUBE_OAUTH.redirect_host)
        self.assertIsNone(TIKTOK_OAUTH.redirect_host)
        self.assertEqual(META_OAUTH.redirect_host, "localhost")

    def test_oauth_callback_unknown_state(self):
        client, _ = self._app()
        res = client.post("/api/v1/social/oauth/callback", json={
            "state": "nope", "code": "x"})
        self.assertEqual(res.status_code, 400)

    def test_oauth_callback_decline(self):
        import os

        from gaeclip.social import oauth_flow as flow_mod

        client, _ = self._app()
        with patch.dict(os.environ, {"SOCIAL_YOUTUBE_CLIENT_ID": "cid-123"}):
            started = client.post("/api/v1/social/accounts/connect", json={
                "provider": "youtube",
                "redirect_uri": "http://127.0.0.1:3000/oauth/callback"}).json()
            state = self._state_of(started["session_id"], flow_mod)
            res = client.post("/api/v1/social/oauth/callback", json={
                "state": state, "error": "access_denied"})
        self.assertEqual(res.status_code, 400)

    def test_oauth_callback_replay_is_idempotent(self):
        import os

        from gaeclip.social import oauth_flow as flow_mod

        client, _ = self._app()
        with patch.dict(os.environ, {"SOCIAL_YOUTUBE_CLIENT_ID": "cid-123",
                                     "SETTINGS_SECRET": "dGVzdHNlY3JldGtleXRlc3RzMTIzNDU2Nzg="}):
            started = client.post("/api/v1/social/accounts/connect", json={
                "provider": "youtube",
                "redirect_uri": "http://127.0.0.1:3000/oauth/callback"}).json()
            state = self._state_of(started["session_id"], flow_mod)
            body = {"state": state, "code": "authcode-abc"}
            with patch("gaeclip.social.oauth.exchange_code") as ex, \
                 patch("gaeclip.social.service.publisher_for",
                       return_value=FakeSuccessPublisher()):
                ex.return_value = {"access_token": "t", "expires_in": 3600}
                first = client.post("/api/v1/social/oauth/callback", json=body)
                second = client.post("/api/v1/social/oauth/callback", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        # Code redeemed exactly once despite two POSTs.
        self.assertEqual(ex.call_count, 1)
        self.assertEqual(second.json()["status"], "complete")

    def test_token_error_messages_mapped(self):
        from gaeclip.social.oauth import _token_error_message

        self.assertIn("Desktop", _token_error_message(
            '{"error": "redirect_uri_mismatch", "error_description": "Bad request"}'))
        self.assertIn("Connect lagi", _token_error_message(
            '{"error": "invalid_grant", "error_description": "expired"}'))
        self.assertIn("dibatalkan", _token_error_message(
            '{"error": "access_denied"}'))
        self.assertIn("Client ID", _token_error_message(
            '{"error": "invalid_client"}'))
        plain = _token_error_message("not json at all {{{")
        self.assertNotIn("secret", plain.lower())


class OAuthInfraTests(unittest.TestCase):
    def test_pkce_pair_shape_and_uniqueness(self):
        from gaeclip.social.oauth import new_pkce_pair

        v1, c1 = new_pkce_pair()
        v2, c2 = new_pkce_pair()
        self.assertNotEqual(v1, v2)
        self.assertNotEqual(c1, c2)
        self.assertTrue(v1 and c1)

    def test_authorize_url_has_state_pkce_scopes(self):
        from gaeclip.social.oauth import (OAuthProviderConfig,
                                          build_authorization_url)

        cfg = OAuthProviderConfig(provider="x", authorize_url="https://a.example/auth",
                                  token_url="https://a.example/token",
                                  scopes=("s1", "s2"))
        session = build_authorization_url("cid", cfg, "http://127.0.0.1:9/cb")
        self.assertIn("state=" + session.state, session.authorization_url)
        self.assertIn("code_challenge=", session.authorization_url)
        self.assertIn("s1", session.authorization_url)

    def test_loopback_roundtrip_binds_localhost_only(self):
        import urllib.parse
        from gaeclip.social import oauth as oauth_mod

        session_holder: dict = {}

        def _server():
            from gaeclip.social.oauth import (OAuthProviderConfig,
                                              await_callback, bind_loopback,
                                              build_authorization_url)

            cfg = OAuthProviderConfig(provider="x",
                                      authorize_url="https://a.example/auth",
                                      token_url="https://a.example/token")
            server = bind_loopback()
            self.assertEqual(server.server_address[0], "127.0.0.1")
            port = server.server_address[1]
            session = build_authorization_url(
                "cid", cfg, f"http://127.0.0.1:{port}/oauth/callback")
            session_holder["session"] = session
            session_holder["server"] = server
            code = await_callback(server, session, timeout_seconds=10)
            session_holder["code"] = code

        thread = threading.Thread(target=_server, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while "session" not in session_holder and time.time() < deadline:
            time.sleep(0.05)
        session = session_holder["session"]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{session_holder['server'].server_address[1]}"
                f"/oauth/callback?state={session.state}&code=abc123",
                timeout=10) as response:
            self.assertEqual(response.status, 200)
        thread.join(timeout=10)
        self.assertEqual(session_holder.get("code"), "abc123")

    def test_unknown_state_rejected(self):
        from gaeclip.social.oauth import bind_loopback
        import urllib.error

        server = bind_loopback()
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/oauth/callback?state=nope&code=x",
                    timeout=10)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()

    def test_youtube_oauth_config_documented(self):
        from gaeclip.social.publishers.youtube import YOUTUBE_OAUTH

        self.assertIn("youtube.upload", " ".join(YOUTUBE_OAUTH.scopes))
        self.assertTrue(YOUTUBE_OAUTH.use_pkce)
        # Public client: no secret required for installed-app flow.
        self.assertFalse(YOUTUBE_OAUTH.client_secret_required)

    def test_provider_identity_param_and_pkce_matrix(self):
        from gaeclip.social.oauth import build_authorization_url
        from gaeclip.social.publishers.meta import META_OAUTH
        from gaeclip.social.publishers.tiktok import TIKTOK_OAUTH
        from gaeclip.social.publishers.youtube import YOUTUBE_OAUTH

        cases = [
            (YOUTUBE_OAUTH, "client_id=", True),
            (TIKTOK_OAUTH, "client_key=", False),
            (META_OAUTH, "client_id=", False),
        ]
        for cfg, id_param, wants_pkce in cases:
            session = build_authorization_url(
                "CID", cfg, "http://127.0.0.1:9/oauth/callback")
            self.assertIn(id_param, session.authorization_url, cfg.provider)
            self.assertEqual("code_challenge=" in session.authorization_url,
                             wants_pkce, cfg.provider)
        # TikTok must NOT receive the wrong identity field.
        tiktok_url = build_authorization_url(
            "TK", TIKTOK_OAUTH, "http://127.0.0.1:9/oauth/callback").authorization_url
        self.assertNotIn("client_id=", tiktok_url)

    def test_token_payload_identity_field_per_provider(self):
        import io
        import json as _json

        from gaeclip.social import oauth as oauth_mod
        from gaeclip.social.publishers.tiktok import TIKTOK_OAUTH
        from gaeclip.social.publishers.youtube import YOUTUBE_OAUTH

        seen: dict = {}

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return _json.dumps(self._payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=30):
            seen["url"] = request.full_url
            seen["body"] = request.data.decode()
            return FakeResponse({"access_token": "tok",
                                 "refresh_token": "ref", "expires_in": 3600})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            oauth_mod.exchange_code(
                token_url="https://t.example/token", code="c",
                redirect_uri="http://127.0.0.1:9/oauth/callback",
                client_id="TK", client_secret="TS",
                client_id_param=TIKTOK_OAUTH.client_id_param)
            self.assertIn("client_key=TK", seen["body"])
            self.assertNotIn("client_id=", seen["body"])
            oauth_mod.refresh_access_token(
                token_url="https://g.example/token", refresh_token="r",
                client_id="GC",
                client_id_param=YOUTUBE_OAUTH.client_id_param)
            self.assertIn("client_id=GC", seen["body"])
            self.assertNotIn("client_key=", seen["body"])


if __name__ == "__main__":
    unittest.main()


class MediaEndpointTests(unittest.TestCase):
    def _app(self):
        from unittest.mock import MagicMock, patch

        from fastapi.testclient import TestClient

        import gaeclip.api as api_mod

        from test_video_metadata import FakeRedis

        fake = FakeRedis()
        redis_mock = MagicMock()
        redis_mock.from_url.return_value = fake
        with patch("redis.Redis", redis_mock):
            app = api_mod.create_app()
        return TestClient(app), fake

    def _job(self, fake, job_id="spub-m1"):
        from gaeclip.social.accounts import SocialPublishJob

        job = SocialPublishJob(id=job_id, clip_id="c000001",
                               job_id="job-1", provider="instagram",
                               social_account_id="acc-1", status="failed",
                               title="T", idempotency_key=f"k-{job_id}")
        from gaeclip.social.accounts import SocialPublishStore

        return SocialPublishStore(fake).save_job(job)

    def test_token_roundtrip_and_expiry(self):
        import time

        from gaeclip.social.service import SocialPublishService

        svc = make_service()
        token = svc.issue_media_token("spub-m1")
        self.assertTrue(len(token) >= 32)
        self.assertEqual(svc.resolve_media_token(token), "spub-m1")
        self.assertIsNone(svc.resolve_media_token("bogus-token-xyz"))
        self.assertIsNone(svc.resolve_media_token(""))
        # Expired payload rejected.
        import json

        fake = svc.jobs.client if hasattr(svc.jobs, "client") else None
        if fake is not None:
            fake.set("gaeclip:social:media:old-tok-1234567890",
                     json.dumps({"job_id": "spub-m1",
                                 "expires_at": time.time() - 10}))
            self.assertIsNone(svc.resolve_media_token("old-tok-1234567890"))

    def test_media_base_url_validation(self):
        import os

        from gaeclip.social.service import _public_media_base_url

        from test_video_metadata import FakeRedis

        with patch.dict(os.environ,
                        {"SOCIAL_PUBLIC_MEDIA_BASE_URL":
                         "https://api.example.com"}):
            self.assertEqual(_public_media_base_url(FakeRedis()),
                             "https://api.example.com")
        for bad in ("", "http://127.0.0.1:4114", "http://localhost:4113",
                    "not-a-url", "ftp://x.example/y"):
            with patch.dict(os.environ,
                            {"SOCIAL_PUBLIC_MEDIA_BASE_URL": bad}):
                self.assertEqual(_public_media_base_url(FakeRedis()), "")

    def test_media_context_only_when_configured(self):
        import os

        from gaeclip.social.service import SocialPublishService

        from test_video_metadata import FakeRedis

        svc = SocialPublishService(FakeRedis(), runtime="desktop")

        class Job:
            id = "spub-m1"

        with patch.dict(os.environ, {"SOCIAL_PUBLIC_MEDIA_BASE_URL": ""}):
            self.assertEqual(svc._media_context(Job()), {})
        with patch.dict(os.environ, {"SOCIAL_PUBLIC_MEDIA_BASE_URL":
                                     "https://api.example.com/"}):
            out = svc._media_context(Job())
            self.assertTrue(out["media_url"].startswith(
                "https://api.example.com/api/v1/social/publish/spub-m1/media?token="))
            self.assertEqual(len(out["media_token"]) >= 32, True)

    def test_serve_media_endpoint(self):
        import os
        import tempfile
        from pathlib import Path as _P

        from gaeclip.social.service import SocialPublishService

        client, fake = self._app()
        job = self._job(fake)
        svc = SocialPublishService(fake, runtime="desktop")
        token = svc.issue_media_token(job.id)
        with tempfile.TemporaryDirectory() as tmp:
            clip_dir = _P(tmp) / "jobs" / job.job_id / "clips"
            clip_dir.mkdir(parents=True)
            (clip_dir / f"{job.clip_id}.mp4").write_bytes(b"\x00\x00mp4bytes")
            with patch.dict(os.environ, {"STORAGE_ROOT": tmp}):
                res = client.get(
                    f"/api/v1/social/publish/{job.id}/media",
                    params={"token": token})
        self.assertEqual(res.status_code, 200, res.text[:200])
        self.assertIn("video/mp4", res.headers.get("content-type", ""))
        self.assertEqual(res.content, b"\x00\x00mp4bytes")

    def test_serve_media_rejects(self):
        import os
        import tempfile

        from gaeclip.social.service import SocialPublishService

        client, fake = self._app()
        job = self._job(fake)
        svc = SocialPublishService(fake, runtime="desktop")
        token = svc.issue_media_token(job.id)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"STORAGE_ROOT": tmp}):
                bad = client.get(
                    f"/api/v1/social/publish/{job.id}/media",
                    params={"token": "wrong-token-value-123"})
                self.assertEqual(bad.status_code, 404)
                other = client.get(
                    "/api/v1/social/publish/spub-nope/media",
                    params={"token": token})
                self.assertEqual(other.status_code, 404)

    def test_media_token_never_logged(self):
        import os
        import tempfile
        from pathlib import Path as _P

        from gaeclip.social.service import SocialPublishService

        client, fake = self._app()
        job = self._job(fake)
        svc = SocialPublishService(fake, runtime="desktop")
        token = svc.issue_media_token(job.id)
        with tempfile.TemporaryDirectory() as tmp:
            clip_dir = _P(tmp) / "jobs" / job.job_id / "clips"
            clip_dir.mkdir(parents=True)
            (clip_dir / f"{job.clip_id}.mp4").write_bytes(b"vid")
            with patch.dict(os.environ, {"STORAGE_ROOT": tmp}), \
                 self.assertLogs("gaeclip.social.api",
                                 level="INFO") as logs:
                client.get(f"/api/v1/social/publish/{job.id}/media",
                           params={"token": token})
        self.assertNotIn(token, "\n".join(logs.output))
