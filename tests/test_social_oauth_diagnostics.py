"""OAuth diagnostics tests: stage-tagged safe logs for Meta 400s.

No network, no real credentials. Verifies that the real provider reason
reaches the server log (with stage + code) while the UI keeps only the
generic user-facing message — and that no secrets leak into logs.
"""

import sys
import unittest

sys.path.insert(0, "tests")


class GraphMessageTests(unittest.TestCase):
    def test_graph_message_preserves_code_subcode_type(self):
        import json

        from gaeclip.social.publishers.meta import _graph_message

        detail = json.dumps({"error": {
            "message": "Invalid permission",
            "code": 10,
            "error_subcode": 123,
            "type": "OAuthException",
            "fbtrace_id": "abc123",
        }})
        out = _graph_message(detail)
        self.assertIn("Invalid permission", out)
        self.assertIn("code=10", out)
        self.assertIn("123", out)
        self.assertIn("OAuthException", out)
        self.assertLessEqual(len(out), 300)

    def test_graph_json_error_mapping(self):
        import json
        from unittest.mock import patch

        from gaeclip.social.publishers.meta import _GraphClient
        from gaeclip.social.states import (
            AuthError, PermissionError, SocialError, TransientError)

        def http_error_with_body(body: str):
            import urllib.error

            fp = _FakeFP(body)
            return urllib.error.HTTPError(
                "https://graph.facebook.com/v21.0/me", 400,
                "Bad Request", {}, fp)

        cases = [
            ({"error": {"message": "Invalid token", "code": 190}}, AuthError),
            ({"error": {"message": "No permission", "code": 200}}, PermissionError),
            ({"error": {"message": "Rate limited", "code": 4}}, TransientError),
            ({"error": {"message": "Bad param", "code": 100}}, SocialError),
            ({"error": {"message": "Unknown path components: /video_uploads",
                         "code": 2500, "type": "OAuthException"}},
             SocialError),
        ]
        for payload, exc_type in cases:
            body = json.dumps(payload)
            with patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                       side_effect=http_error_with_body(body)):
                with self.assertRaises(exc_type, msg=str(payload)) as ctx:
                    _GraphClient.call("GET", "me/accounts", "tok", {})
            # Technical detail preserved for server logs.
            self.assertIn(str(payload["error"]["code"]), str(ctx.exception))
        # 2500 must NOT masquerade as missing permission.
        with patch("gaeclip.social.publishers.meta.urllib.request.urlopen",
                   side_effect=http_error_with_body(json.dumps(
                       {"error": {"message": "Unknown path components",
                                  "code": 2500}}))):
            with self.assertRaises(SocialError) as ctx:
                _GraphClient.call("GET", "me/accounts", "tok", {})
        self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertNotIsInstance(ctx.exception, PermissionError)


class _FakeFP:
    def __init__(self, body: str):
        self._body = body.encode()

    def read(self, *args, **kwargs):
        return self._body

    def close(self):
        pass

    def info(self):
        return {}


class OAuthStageLogTests(unittest.TestCase):
    def _start_session(self, flow_mod, provider="facebook"):
        import uuid

        from gaeclip.social import oauth as _oauth
        from gaeclip.social.service import _provider_oauth_config

        config = _provider_oauth_config(provider)
        session = _oauth.build_authorization_url(
            "test-client", config, "http://localhost:3000/oauth/callback")
        session_id = f"oauth_{uuid.uuid4().hex[:12]}"
        flow_mod._sessions[session_id] = {
            "status": "pending", "provider": provider,
            "created_at": 0.0, "account_id": "", "error": "",
            "oauth": session, "client_id": "test-client",
            "client_secret": "test-secret",
        }
        return session_id, session.state

    def test_exchange_failure_logs_stage_exchange_without_secrets(self):
        from unittest.mock import patch

        from gaeclip.social import oauth_flow as flow_mod
        from gaeclip.social.states import AuthError

        session_id, state = self._start_session(flow_mod)
        secret_code = "authcode-super-secret-123"
        with patch("gaeclip.social.oauth.exchange_code",
                   side_effect=AuthError(
                       "token endpoint http 400: invalid code",
                       user_message="Gagal menukar kode login. Coba lagi.")), \
             self.assertLogs("gaeclip.social.oauth_flow", level="INFO") as logs:
            with self.assertRaises(AuthError):
                flow_mod.complete_by_state(
                    object(), state, code=secret_code)
        combined = "\n".join(logs.output)
        self.assertIn("stage=exchange", combined)
        self.assertIn("facebook", combined)
        # Secrets must never appear in logs.
        self.assertNotIn(secret_code, combined)
        self.assertNotIn("test-secret", combined)
        self.assertNotIn("access_token", combined.lower())
        # Session marked failed with user-facing message only.
        item = flow_mod._sessions.get(session_id) or {}
        self.assertEqual(item.get("status"), "failed")
        flow_mod._sessions.pop(session_id, None)

    def test_account_info_400_logs_stage_account_info(self):
        from unittest.mock import patch

        from gaeclip.social import oauth_flow as flow_mod
        from gaeclip.social.states import SocialError

        session_id, state = self._start_session(flow_mod)
        with patch("gaeclip.social.oauth.exchange_code",
                   return_value={"access_token": "tok-1",
                                 "expires_in": 3600}), \
             patch("gaeclip.social.oauth_flow._exchange_fb_long_lived",
                   side_effect=lambda url, resp, cid, sec: resp), \
             patch("gaeclip.social.service.publisher_for") as mock_pub:
            import types

            fake = types.SimpleNamespace()

            def _boom(token):
                raise SocialError(
                    "http 400: (#10) This action is not allowed (code=10)",
                    code="invalid_request",
                    user_message="Permintaan tidak valid untuk platform ini.")
            fake.account_info = _boom
            fake.validate_account = lambda *a, **k: None
            mock_pub.return_value = fake
            with self.assertLogs("gaeclip.social.oauth_flow",
                                level="INFO") as logs:
                with self.assertRaises(SocialError) as ctx:
                    flow_mod.complete_by_state(
                        object(), state, code="code-123")
        combined = "\n".join(logs.output)
        self.assertIn("stage=account_info", combined)
        self.assertIn("invalid_request", combined)
        self.assertIn("code=10", combined)
        # UI-facing message stays generic.
        self.assertEqual(ctx.exception.user_message,
                         "Permintaan tidak valid untuk platform ini.")
        flow_mod._sessions.pop(session_id, None)

    def test_decline_and_missing_code_logged_without_state_secret(self):
        from gaeclip.social import oauth_flow as flow_mod
        from gaeclip.social.states import AuthError

        session_id, state = self._start_session(flow_mod)
        with self.assertLogs("gaeclip.social.oauth_flow",
                            level="INFO") as logs:
            with self.assertRaises(AuthError):
                flow_mod.complete_by_state(
                    object(), state, code="", error="access_denied")
        self.assertIn("declined", "\n".join(logs.output).lower())
        # Full state value must not be logged, only a suffix.
        self.assertNotIn(state, "\n".join(logs.output))
        flow_mod._sessions.pop(session_id, None)


if __name__ == "__main__":
    unittest.main()
