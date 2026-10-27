"""Meta OAuth scope + discovery tests (no network, no credentials)."""

import sys
import unittest

sys.path.insert(0, "tests")


class MetaScopeTests(unittest.TestCase):
    def test_facebook_only_scopes(self):
        from gaeclip.social.publishers.meta import META_FACEBOOK_PAGE_SCOPES

        scopes = tuple(META_FACEBOOK_PAGE_SCOPES)
        self.assertEqual(scopes, (
            "pages_show_list",
            "pages_read_engagement",
            "pages_manage_posts",
        ))
        self.assertNotIn("publish_video", scopes)
        self.assertNotIn("business_management", scopes)
        self.assertNotIn("instagram_basic", scopes)

    def test_instagram_login_scopes(self):
        # Current Instagram Login scopes (post-2025 names). No Facebook
        # Page scopes unless Meta officially requires them (it does not
        # for this setup).
        from gaeclip.social.publishers.instagram_login import (
            INSTAGRAM_LOGIN_SCOPES)

        scopes = tuple(INSTAGRAM_LOGIN_SCOPES)
        self.assertEqual(scopes, (
            "instagram_business_basic",
            "instagram_business_content_publish",
        ))
        for page_scope in ("pages_show_list", "pages_read_engagement",
                           "pages_manage_posts", "business_management"):
            self.assertNotIn(page_scope, scopes)

    def test_instagram_legacy_scopes_kept_for_detection_only(self):
        from gaeclip.social.publishers.meta import META_INSTAGRAM_SCOPES

        scopes = tuple(META_INSTAGRAM_SCOPES)
        self.assertIn("instagram_basic", scopes)
        self.assertIn("pages_show_list", scopes)

    def test_combined_no_duplicates(self):
        from gaeclip.social.publishers.instagram_login import (
            INSTAGRAM_LOGIN_SCOPES)
        from gaeclip.social.publishers.meta import meta_scopes_for

        for provider in ("facebook", "meta"):
            scopes = meta_scopes_for(provider)
            self.assertEqual(len(scopes), len(set(scopes)))
        self.assertEqual(len(INSTAGRAM_LOGIN_SCOPES),
                         len(set(INSTAGRAM_LOGIN_SCOPES)))

    def test_no_deprecated_scopes(self):
        from gaeclip.social.publishers.meta import META_FACEBOOK_PAGE_SCOPES

        deprecated = {"business_management", "business_basic",
                      "business_content_publish", "ads_management", "ads_read",
                      "publish_video"}
        for scope in META_FACEBOOK_PAGE_SCOPES:
            self.assertNotIn(scope, deprecated)

    def test_no_business_management_unless_required(self):
        from gaeclip.social.publishers.instagram_login import (
            INSTAGRAM_LOGIN_SCOPES)
        from gaeclip.social.publishers.meta import META_FACEBOOK_PAGE_SCOPES

        self.assertNotIn("business_management", META_FACEBOOK_PAGE_SCOPES)
        self.assertNotIn("business_management", INSTAGRAM_LOGIN_SCOPES)

    def test_no_secret_in_authorize_url(self):
        from gaeclip.social import oauth as _oauth
        from gaeclip.social.service import _provider_oauth_config

        for provider in ("facebook", "instagram"):
            config = _provider_oauth_config(provider)
            session = _oauth.build_authorization_url(
                "test-client-id", config, "http://localhost:3000/oauth/callback")
            url = session.authorization_url.lower()
            self.assertNotIn("secret", url)
            self.assertNotIn("access_token", url)
            self.assertIn("scope=", url)

    def test_redirect_uri_consistency(self):
        from gaeclip.social import oauth as _oauth
        from gaeclip.social.service import _provider_oauth_config

        for provider in ("facebook", "instagram"):
            config = _provider_oauth_config(provider)
            rewritten = _oauth.apply_redirect_host(
                "http://127.0.0.1:41817/oauth/callback", config)
            self.assertIn("localhost", rewritten)
            self.assertIn("/oauth/callback", rewritten)

    def test_token_exchange_consistency(self):
        from gaeclip.social.service import _provider_oauth_config

        fb = _provider_oauth_config("facebook")
        self.assertIn("v21.0", fb.authorize_url)
        self.assertIn("v21.0", fb.token_url)
        self.assertEqual(fb.client_id_param, "client_id")
        self.assertTrue(fb.client_secret_required)
        # Instagram Login: independent endpoints (never Facebook URLs).
        ig = _provider_oauth_config("instagram")
        self.assertIn("instagram.com/oauth/authorize", ig.authorize_url)
        self.assertNotIn("facebook.com", ig.authorize_url)
        self.assertIn("api.instagram.com/oauth/access_token", ig.token_url)
        self.assertNotIn("facebook.com", ig.token_url)
        self.assertEqual(ig.client_id_param, "client_id")
        self.assertTrue(ig.client_secret_required)

    def test_provider_configs_isolated(self):
        from gaeclip.social.service import _provider_oauth_config

        fb = _provider_oauth_config("facebook")
        ig = _provider_oauth_config("instagram")
        self.assertNotEqual(ig.authorize_url, fb.authorize_url)
        self.assertNotEqual(ig.token_url, fb.token_url)
        self.assertTrue(set(fb.scopes).isdisjoint(set(ig.scopes)))
        self.assertNotIn("publish_video", fb.scopes)
        self.assertIn("pages_manage_posts", fb.scopes)
        self.assertIn("instagram_business_content_publish", ig.scopes)
        for scope in ig.scopes:
            self.assertNotIn("pages_", scope)

    def test_youtube_tiktok_untouched(self):
        from gaeclip.social.service import _provider_oauth_config

        yt = _provider_oauth_config("youtube")
        tt = _provider_oauth_config("tiktok")
        self.assertIn("youtube.upload", " ".join(yt.scopes))
        self.assertIn("video.upload", " ".join(tt.scopes))


class MetaDiscoveryTests(unittest.TestCase):
    def test_facebook_page_discovery(self):
        from unittest.mock import patch

        from gaeclip.social.publishers.meta import FacebookPublisher

        pages = {"data": [{"id": "page-1", "name": "My Page",
                           "access_token": "page-tok-1"}]}
        with patch("gaeclip.social.publishers.meta._GraphClient.call",
                   return_value=pages):
            info = FacebookPublisher().account_info("user-tok")
        self.assertEqual(info.external_account_id, "page-1")
        self.assertEqual(info.extra["page_token"], "page-tok-1")

    def test_facebook_missing_page(self):
        from unittest.mock import patch

        from gaeclip.social.publishers.meta import FacebookPublisher
        from gaeclip.social.states import AccountRequirementError

        with patch("gaeclip.social.publishers.meta._GraphClient.call",
                   return_value={"data": []}):
            with self.assertRaises(AccountRequirementError):
                FacebookPublisher().account_info("user-tok")

    def test_instagram_legacy_discovery(self):
        # Retired Facebook-Login flow, kept for old-row detection only.
        from unittest.mock import patch

        from gaeclip.social.publishers.meta import InstagramPublisher

        def fake_call(method, path, token, params=None, **kw):
            if path == "me/accounts":
                return {"data": [{"id": "page-1", "name": "P",
                                  "access_token": "page-tok",
                                  "instagram_business_account": {"id": "ig-1"}}]}
            return {"id": "ig-1", "username": "myig", "account_type": "BUSINESS"}

        with patch("gaeclip.social.publishers.meta._GraphClient.call",
                   side_effect=fake_call):
            info = InstagramPublisher().account_info("user-tok")
        self.assertEqual(info.external_account_id, "ig-1")
        self.assertEqual(info.extra["facebook_page_id"], "page-1")
        self.assertEqual(info.extra["page_token"], "page-tok")

    def test_instagram_legacy_missing_account(self):
        from unittest.mock import patch

        from gaeclip.social.publishers.meta import InstagramPublisher
        from gaeclip.social.states import AccountRequirementError

        with patch("gaeclip.social.publishers.meta._GraphClient.call",
                   return_value={"data": [{"id": "p", "name": "P"}]}):
            with self.assertRaises(AccountRequirementError):
                InstagramPublisher().account_info("user-tok")

    def test_new_oauth_config_never_facebook(self):
        from gaeclip.social.publishers.instagram_login import (
            INSTAGRAM_LOGIN_OAUTH)

        self.assertNotIn("facebook.com", INSTAGRAM_LOGIN_OAUTH.authorize_url)
        self.assertNotIn("facebook.com", INSTAGRAM_LOGIN_OAUTH.token_url)
        # Comma-separated scopes per Instagram Login docs.
        self.assertEqual(INSTAGRAM_LOGIN_OAUTH.scope_separator, ",")

    def test_effective_page_token(self):
        from gaeclip.social.publishers.meta import _effective_page_token

        self.assertEqual(_effective_page_token({"page_token": "pt"}, "ut"), "pt")
        self.assertEqual(_effective_page_token({}, "ut"), "ut")

    def test_safe_log_has_no_secrets(self):
        from gaeclip.social.publishers.meta import safe_authorization_log

        out = safe_authorization_log("facebook", "http://localhost:3000/oauth/callback",
                                     ("pages_show_list",))
        self.assertIn("facebook", out)
        self.assertNotIn("secret", out.lower())
        self.assertNotIn("token", out.lower().replace("oauth", ""))


if __name__ == "__main__":
    unittest.main()
