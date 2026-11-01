"""Tests for signed URL utilities."""

import sys
import time
import unittest

sys.path.insert(0, "tests")


class SignedUrlTests(unittest.TestCase):
    def test_sign_and_verify(self):
        from gaeclip.signed_urls import sign, verify

        secret = "super-secret"
        url = sign(secret=secret, path="/foo", expires_in=3600)
        # Parse query params manually.
        parts = url.split("?")[1].split("&")
        params = {}
        for part in parts:
            k, v = part.split("=")
            params[k] = v
        self.assertIn("sig", params)
        self.assertIn("expires", params)
        self.assertTrue(verify(secret=secret, path="/foo", sig=params["sig"], expires=params["expires"]))

    def test_expired_signature_rejected(self):
        from gaeclip.signed_urls import sign, verify

        secret = "s"
        url = sign(secret=secret, path="/bar", expires_in=-1)
        parts = url.split("?")[1].split("&")
        params = {p.split("=")[0]: p.split("=")[1] for p in parts}
        self.assertFalse(verify(secret=secret, path="/bar", sig=params["sig"], expires=params["expires"]))

    def test_tampered_sig_rejected(self):
        from gaeclip.signed_urls import sign, verify

        secret = "s"
        url = sign(secret=secret, path="/baz", expires_in=3600)
        # Tamper with the signature by appending a character.
        original_sig = url.split("sig=")[1].split("&")[0]
        tampered_sig = original_sig + "x"
        tampered_url = url.replace(f"sig={original_sig}", f"sig={tampered_sig}")
        parts = tampered_url.split("?")[1].split("&")
        params = {p.split("=")[0]: p.split("=")[1] for p in parts}
        self.assertFalse(verify(secret=secret, path="/baz", sig=params["sig"], expires=params["expires"]))


if __name__ == "__main__":
    unittest.main()
