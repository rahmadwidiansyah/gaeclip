import sys
import types
import unittest
from unittest.mock import patch

from gaeclip import gemini_service
from gaeclip.gemini_service import classify_error, close_client


def install_genai_stub(behavior):
    """behavior: 'ok' | Exception instance | list per call."""
    calls = {"n": 0, "closed": 0}

    class FakeModels:
        def __init__(self, client):
            self._client = client

        def generate_content(self, **kwargs):
            calls["n"] += 1
            if getattr(self._client, "_shut", False):
                calls["closed"] += 1
                raise RuntimeError("Cannot send a request, as the client has been closed.")
            action = behavior[calls["n"] - 1] if isinstance(behavior, list) else behavior
            if isinstance(action, Exception):
                raise action
            return types.SimpleNamespace(text="ok")

    class FakeClient:
        def __init__(self, api_key=None, **kwargs):
            self.api_key = api_key
            self._shut = False
            self.models = FakeModels(self)

        def close(self):
            self._shut = True

    stub = types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    genai.Client = FakeClient
    stub.genai = genai
    patcher = patch.dict(sys.modules, {"google": stub, "google.genai": genai})
    patcher.start()
    return patcher, calls


class LifecycleTests(unittest.TestCase):
    def test_repeated_calls_never_hit_closed_client(self):
        patcher, calls = install_genai_stub("ok")
        try:
            for _ in range(3):
                result = gemini_service.test_connection("sk-test-key", "gemini-3.8-flash", 30)
                self.assertTrue(result.ok, result.message)
            self.assertEqual(calls["n"], 3)
            self.assertEqual(calls["closed"], 0)
        finally:
            patcher.stop()

    def test_mixed_success_then_failure_then_success(self):
        patcher, calls = install_genai_stub(
            ["ok", RuntimeError("429 quota exceeded"), "ok"]
        )
        try:
            first = gemini_service.test_connection("sk-k", "m", 30)
            second = gemini_service.test_connection("sk-k", "m", 30)
            third = gemini_service.test_connection("sk-k", "m", 30)
            self.assertTrue(first.ok)
            self.assertFalse(second.ok)
            self.assertEqual(second.error_code, "rate_limit")
            self.assertTrue(third.ok)
            self.assertEqual(calls["closed"], 0)
        finally:
            patcher.stop()

    def test_close_client_is_safe_on_anything(self):
        close_client(None)
        close_client(object())
        self.assertTrue(True)

    def test_latency_and_model_echoed(self):
        patcher, _ = install_genai_stub("ok")
        try:
            result = gemini_service.test_connection("sk-k", "gemini-x", 30)
            self.assertEqual(result.model, "gemini-x")
            self.assertIsNotNone(result.latency_ms)
            self.assertGreaterEqual(result.latency_ms, 0)
        finally:
            patcher.stop()


class MissingConfigTests(unittest.TestCase):
    def test_missing_key(self):
        result = gemini_service.test_connection("", "gemini-x", 30)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "missing_api_key")

    def test_missing_model(self):
        result = gemini_service.test_connection("sk-k", "  ", 30)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "model_not_found")


class ClassificationTests(unittest.TestCase):
    def test_mapping(self):
        cases = [
            (TimeoutError("timed out"), "timeout"),
            (RuntimeError("429 quota exceeded"), "rate_limit"),
            (RuntimeError("401 unauthorized"), "authentication_error"),
            (RuntimeError("API_KEY_INVALID"), "invalid_api_key"),
            (RuntimeError("404 model not found"), "model_not_found"),
            (ConnectionError("dns failed"), "network_error"),
            (RuntimeError("Cannot send a request, as the client has been closed."),
             "client_lifecycle_error"),
            (RuntimeError("weird boom"), "unknown_error"),
        ]
        for exc, expected in cases:
            with self.subTest(exc=str(exc)[:30]):
                code, message = classify_error(exc)
                self.assertEqual(code, expected)
                self.assertTrue(message)

    def test_key_never_leaks_into_result(self):
        patcher, _ = install_genai_stub(RuntimeError("401 bad key sk-live-abcdef123456"))
        try:
            result = gemini_service.test_connection("sk-live-abcdef123456", "m", 30)
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "authentication_error")
            self.assertNotIn("sk-live-abcdef123456", result.message)
        finally:
            patcher.stop()


if __name__ == "__main__":
    unittest.main()
