from __future__ import annotations

import io
import logging
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from google.genai.errors import ClientError

from app import main
from app.ai import gemini_diagnostic
from app.config import Settings
from app.queue_worker import (
    CANONICAL_RECIPE_SCHEMA,
    build_gemini_generation_config,
    build_youtube_gemini_input_part,
)

WORKER_SECRET = "test-worker-secret"
API_KEY = "test-gemini-key"
YOUTUBE_URL = "https://www.youtube.com/watch?v=abcdefghijk"


class FakeGeminiResponse:
    def __init__(self, *, text: str = "{}", parsed: object = None) -> None:
        self.text = text
        self.parsed = parsed
        self.candidates: list[object] = []
        self.prompt_feedback = None


def diagnostic_settings(
    *,
    app_env: str = "development",
    enabled: bool = True,
) -> Settings:
    return Settings(
        _env_file=None,
        worker_api_secret=WORKER_SECRET,
        supabase_url="https://example.invalid",
        supabase_service_role_key="test-only",
        gemini_api_key=API_KEY,
        app_env=app_env,
        allow_gemini_diagnostic=enabled,
        media_resolver_mode="legacy",
        external_media_resolver_audience="disabled",
        instagram_resolver_chain=[],
        tiktok_resolver_chain=[],
    )


class GeminiDiagnosticEndpointTests(unittest.TestCase):
    def tearDown(self) -> None:
        main.app.dependency_overrides.clear()

    def client_for(self, settings: Settings) -> TestClient:
        main.app.dependency_overrides[main.get_settings] = lambda: settings
        return TestClient(main.app)

    def post(
        self,
        client: TestClient,
        payload: dict[str, object],
        *,
        secret: str | None = WORKER_SECRET,
    ):
        headers = {"X-Worker-Secret": secret} if secret is not None else {}
        return client.post(
            "/debug/gemini-request-ladder",
            headers=headers,
            json=payload,
        )

    def test_endpoint_is_unavailable_outside_development(self) -> None:
        client = self.client_for(diagnostic_settings(app_env="production"))
        response = self.post(
            client,
            {"model": "gemini-3.1-flash-lite", "level": "plain_text"},
        )
        self.assertEqual(response.status_code, 404)

    def test_endpoint_requires_explicit_enable_flag(self) -> None:
        client = self.client_for(diagnostic_settings(enabled=False))
        response = self.post(
            client,
            {"model": "gemini-3.1-flash-lite", "level": "plain_text"},
        )
        self.assertEqual(response.status_code, 404)

    def test_endpoint_requires_existing_worker_secret(self) -> None:
        client = self.client_for(diagnostic_settings())
        payload = {"model": "gemini-3.1-flash-lite", "level": "plain_text"}
        self.assertEqual(self.post(client, payload, secret=None).status_code, 401)
        self.assertEqual(self.post(client, payload, secret="wrong").status_code, 401)

    def test_request_allowlists_models_levels_fields_and_youtube_urls(self) -> None:
        client = self.client_for(diagnostic_settings())
        invalid_payloads = (
            {"model": "gemini-2.5-flash-lite", "level": "plain_text"},
            {"model": "gemini-3.1-flash-lite", "level": "unknown"},
            {
                "model": "gemini-3.1-flash-lite",
                "level": "plain_text",
                "unexpected": True,
            },
            {
                "model": "gemini-3.1-flash-lite",
                "level": "youtube_production_shape",
                "youtube_url": "http://www.youtube.com/watch?v=abcdefghijk",
            },
            {
                "model": "gemini-3.1-flash-lite",
                "level": "youtube_production_shape",
                "youtube_url": "https://example.com/watch?v=abcdefghijk",
            },
            {
                "model": "gemini-3.1-flash-lite",
                "level": "plain_text",
                "youtube_url": YOUTUBE_URL,
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload_keys=sorted(payload)):
                self.assertEqual(self.post(client, payload).status_code, 422)

    def test_each_level_performs_exactly_one_provider_invocation(self) -> None:
        client = self.client_for(diagnostic_settings())
        levels = (
            ("plain_text", None),
            ("tiny_schema", None),
            ("recipe_schema_text", None),
            ("youtube_production_shape", YOUTUBE_URL),
        )
        for level, youtube_url in levels:
            with self.subTest(level=level):
                fake_client = MagicMock()
                fake_client.models.generate_content.return_value = FakeGeminiResponse(
                    parsed={"ok": True}
                )
                payload: dict[str, object] = {
                    "model": "gemini-3.1-flash-lite",
                    "level": level,
                }
                if youtube_url is not None:
                    payload["youtube_url"] = youtube_url
                with patch.object(
                    gemini_diagnostic,
                    "create_gemini_client",
                    return_value=fake_client,
                ):
                    response = self.post(client, payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["success"])
                self.assertEqual(fake_client.models.generate_content.call_count, 1)

    def test_schema_levels_use_tiny_and_production_configs(self) -> None:
        client = self.client_for(diagnostic_settings())

        tiny_client = MagicMock()
        tiny_client.models.generate_content.return_value = FakeGeminiResponse(
            parsed={"ok": True}
        )
        with patch.object(
            gemini_diagnostic,
            "create_gemini_client",
            return_value=tiny_client,
        ):
            tiny = self.post(
                client,
                {"model": "gemini-3.1-flash-lite", "level": "tiny_schema"},
            )
        self.assertTrue(tiny.json()["structured_parse_succeeded"])
        tiny_config = tiny_client.models.generate_content.call_args.kwargs["config"]
        self.assertIs(
            tiny_config["response_schema"],
            gemini_diagnostic.TinyDiagnosticSchema,
        )
        self.assertNotIn("response_json_schema", tiny_config)

        recipe_client = MagicMock()
        recipe_client.models.generate_content.return_value = FakeGeminiResponse()
        with patch.object(
            gemini_diagnostic,
            "create_gemini_client",
            return_value=recipe_client,
        ):
            recipe = self.post(
                client,
                {"model": "gemini-3.1-flash-lite", "level": "recipe_schema_text"},
            )
        self.assertTrue(recipe.json()["structured_parse_succeeded"])
        recipe_config = recipe_client.models.generate_content.call_args.kwargs["config"]
        expected_config = build_gemini_generation_config(
            diagnostic_settings(),
            model="gemini-3.1-flash-lite",
            structured_schema=True,
        )
        self.assertEqual(recipe_config, expected_config)
        self.assertIs(recipe_config["response_json_schema"], CANONICAL_RECIPE_SCHEMA)

    def test_youtube_level_reuses_production_input_builder_and_request_config(self) -> None:
        self.assertIs(
            gemini_diagnostic.build_youtube_gemini_input_part,
            build_youtube_gemini_input_part,
        )
        client = self.client_for(diagnostic_settings())
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = FakeGeminiResponse()
        input_part = object()
        with patch.object(
            gemini_diagnostic,
            "create_gemini_client",
            return_value=fake_client,
        ), patch.object(
            gemini_diagnostic,
            "build_youtube_gemini_input_part",
            return_value=input_part,
        ) as build_part:
            response = self.post(
                client,
                {
                    "model": "gemini-2.5-flash",
                    "level": "youtube_production_shape",
                    "youtube_url": YOUTUBE_URL,
                },
            )
        self.assertTrue(response.json()["success"])
        build_part.assert_called_once_with(YOUTUBE_URL)
        call = fake_client.models.generate_content.call_args.kwargs
        self.assertIs(call["contents"][0], input_part)
        self.assertEqual(
            call["config"],
            build_gemini_generation_config(
                diagnostic_settings(),
                model="gemini-2.5-flash",
                structured_schema=True,
            ),
        )

    def test_diagnostic_calls_no_repository_quota_resolver_or_sentry_boundary(self) -> None:
        client = self.client_for(diagnostic_settings())
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = FakeGeminiResponse()
        with patch.object(
            gemini_diagnostic,
            "create_gemini_client",
            return_value=fake_client,
        ), patch.object(main, "create_repository") as repository, patch.object(
            main, "report_worker_error"
        ) as sentry, patch.object(
            main, "resolve_social_video"
        ) as resolver, patch(
            "app.queue_worker.ensure_quota_reservation_event_safely"
        ) as quota:
            response = self.post(
                client,
                {"model": "gemini-3.1-flash-lite", "level": "plain_text"},
            )
        self.assertTrue(response.json()["success"])
        repository.assert_not_called()
        sentry.assert_not_called()
        resolver.assert_not_called()
        quota.assert_not_called()

    def test_error_response_and_logs_never_contain_sensitive_input(self) -> None:
        client = self.client_for(diagnostic_settings())
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = ClientError(
            400,
            {
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "message": "sensitive provider body",
                }
            },
        )
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        main.logger.addHandler(handler)
        try:
            with patch.object(
                gemini_diagnostic,
                "create_gemini_client",
                return_value=fake_client,
            ):
                response = self.post(
                    client,
                    {
                        "model": "gemini-3.1-flash-lite",
                        "level": "youtube_production_shape",
                        "youtube_url": YOUTUBE_URL,
                    },
                )
        finally:
            main.logger.removeHandler(handler)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["provider_http_status"], 400)
        self.assertEqual(body["provider_status_name"], "INVALID_ARGUMENT")
        self.assertEqual(body["provider_error_category"], "invalid_argument")
        combined = response.text + stream.getvalue()
        for forbidden in (
            YOUTUBE_URL,
            API_KEY,
            "sensitive provider body",
            gemini_diagnostic._PLAIN_TEXT_PROMPT,
            "response_json_schema",
        ):
            self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
