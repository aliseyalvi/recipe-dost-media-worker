from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import requests

from app.media_resolvers.contracts import ResolverError
from app.media_resolvers.http_client import SafeProviderHttpClient


def response(status: int, content_type: str, chunks: list[bytes]) -> MagicMock:
    value = MagicMock()
    value.status_code = status
    value.headers = {"Content-Type": content_type}
    value.iter_content.return_value = chunks
    value.__enter__.return_value = value
    value.__exit__.return_value = False
    return value


class ProviderHttpClientTests(unittest.TestCase):
    def client(self) -> SafeProviderHttpClient:
        return SafeProviderHttpClient(timeout_seconds=15, response_max_bytes=64 * 1024)

    def call(self, active: SafeProviderHttpClient):
        return active.get_json(
            base_url="https://provider.invalid",
            path="/resolve",
            params={"input": "https://source.invalid/video", "access_token": "secret"},
            headers={"Authorization": "secret"},
            expected_host="provider.invalid",
        )

    def test_valid_json_and_proxy_inheritance_disabled(self) -> None:
        active = self.client()
        self.assertFalse(active._session.trust_env)
        active._session = MagicMock()
        active._session.get.return_value = response(200, "application/json", [b'{"success":true}'])
        payload, status_class, retry_after = self.call(active)
        self.assertEqual(payload["success"], True)
        self.assertEqual(status_class, "2xx")
        self.assertIsNone(retry_after)

    def test_rate_limit_and_auth_are_normalized_without_secret_text(self) -> None:
        for status, code in ((429, "resolver_rate_limited"), (401, "resolver_configuration_error")):
            active = self.client()
            active._session = MagicMock()
            active._session.get.return_value = response(status, "application/json", [b"{}"])
            with self.assertRaises(ResolverError) as captured:
                self.call(active)
            self.assertEqual(captured.exception.code, code)
            self.assertNotIn("secret", str(captured.exception))

    def test_body_limit_content_type_and_malformed_json_fail_safely(self) -> None:
        cases = [
            response(200, "text/html", [b"<html>"]),
            response(200, "application/json", [b"x" * (65 * 1024)]),
            response(200, "application/json", [b"not-json"]),
        ]
        for fake in cases:
            active = self.client()
            active._session = MagicMock()
            active._session.get.return_value = fake
            with self.assertRaises(ResolverError) as captured:
                self.call(active)
            self.assertEqual(captured.exception.code, "resolver_contract_error")
            self.assertTrue(captured.exception.technical)
            self.assertEqual(captured.exception.http_status_class, "2xx")

    def test_timeout_does_not_expose_request_url(self) -> None:
        active = self.client()
        active._session = MagicMock()
        active._session.get.side_effect = requests.Timeout("unsafe https://secret.invalid/?token=x")
        with self.assertRaises(ResolverError) as captured:
            self.call(active)
        self.assertEqual(captured.exception.code, "resolver_temporarily_unavailable")
        self.assertNotIn("secret.invalid", str(captured.exception))
        self.assertIsNone(captured.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
