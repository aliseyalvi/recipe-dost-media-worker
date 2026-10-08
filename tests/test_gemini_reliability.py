from __future__ import annotations

import json
import unittest

from app.ai.gemini_reliability import (
    GeminiRequestConfigurationError,
    StructuredResponseError,
    build_model_generation_config,
    classify_gemini_exception,
    parse_structured_response,
    safe_gemini_provider_error,
)
from app.qa.replay import canonical_recipe, settings_for_case
from app.queue_worker import (
    validate_and_normalize_canonical_recipe,
)


class GeminiStructuredParserTests(unittest.TestCase):
    def test_accepts_object_fence_prose_bom_and_unicode(self) -> None:
        payload = {"title_en": "Curry", "title_ur": "سالن", "value": None}
        raw = json.dumps(payload, ensure_ascii=False)
        variants = (
            raw,
            f"```json\n{raw}\n```",
            f"Result follows.\n{raw}\nEnd.",
            "\ufeff" + raw,
        )
        for value in variants:
            with self.subTest(value=value[:12]):
                self.assertEqual(
                    parse_structured_response(value)["title_ur"],
                    "سالن",
                )

    def test_closed_parse_categories(self) -> None:
        cases = (
            ("", "gemini_empty_response"),
            ('{"title":', "gemini_truncated_response"),
            ('{"a":1} {"b":2}', "gemini_json_parse_failed"),
            ('["wrong"]', "gemini_schema_validation_failed"),
            ("I cannot comply with that request.", "gemini_safety_blocked"),
        )
        for value, category in cases:
            with self.subTest(category=category):
                with self.assertRaises(StructuredResponseError) as captured:
                    parse_structured_response(value)
                self.assertEqual(captured.exception.category, category)

    def test_finish_reason_and_safety_override_parse(self) -> None:
        with self.assertRaises(StructuredResponseError) as truncated:
            parse_structured_response('{"a":', finish_reason="MAX_TOKENS")
        self.assertEqual(truncated.exception.category, "gemini_truncated_response")
        with self.assertRaises(StructuredResponseError) as blocked:
            parse_structured_response('{"a":1}', safety_blocked=True)
        self.assertEqual(blocked.exception.category, "gemini_safety_blocked")

    def test_schema_validation_handles_null_extra_and_non_recipe(self) -> None:
        payload = canonical_recipe()
        payload["unknown_extra"] = "ignored"
        result, errors = validate_and_normalize_canonical_recipe(
            payload,
            {"platform": "tiktok", "title": "Fixture"},
            settings_for_case(),
        )
        self.assertEqual(errors, [])
        self.assertIsNone(result.servings)

        payload["is_recipe"] = False
        _result, errors = validate_and_normalize_canonical_recipe(
            payload,
            {"platform": "tiktok", "title": "Fixture"},
            settings_for_case(),
        )
        self.assertIn("non_recipe_content", errors)


class GeminiCapabilityTests(unittest.TestCase):
    def test_model_specific_schema_and_thinking_configuration(self) -> None:
        schema = {"type": "object", "properties": {}}
        primary = build_model_generation_config(
            model="gemini-3.1-flash-lite",
            response_schema=schema,
            structured_output_enabled=True,
            thinking_level="high",
            max_output_tokens=12_000,
            temperature=0.1,
        )
        fallback = build_model_generation_config(
            model="gemini-2.5-flash",
            response_schema=schema,
            structured_output_enabled=True,
            thinking_level="high",
            max_output_tokens=12_000,
            temperature=0.1,
        )
        self.assertIn("response_json_schema", primary)
        self.assertEqual(primary["thinking_config"], {"thinking_level": "high"})
        self.assertIn("response_schema", fallback)
        self.assertNotIn("thinking_config", fallback)

    def test_fallback_schema_survives_pinned_sdk_request_transformation(self) -> None:
        from google import genai
        from google.genai import models, types
        from app.queue_worker import CANONICAL_RECIPE_SCHEMA

        primary = build_model_generation_config(
            model="gemini-3.1-flash-lite",
            response_schema=CANONICAL_RECIPE_SCHEMA,
            structured_output_enabled=True,
            thinking_level="high",
            max_output_tokens=12_000,
            temperature=0.1,
        )
        fallback = build_model_generation_config(
            model="gemini-2.5-flash",
            response_schema=CANONICAL_RECIPE_SCHEMA,
            structured_output_enabled=True,
            thinking_level="high",
            max_output_tokens=12_000,
            temperature=0.1,
        )
        client = genai.Client(api_key="offline-request-shape-validation")
        try:
            for model, config in (
                ("gemini-3.1-flash-lite", primary),
                ("gemini-2.5-flash", fallback),
            ):
                parameters = types._GenerateContentParameters(
                    model=model,
                    contents=["offline fixture"],
                    config=config,
                )
                models._GenerateContentParameters_to_mldev(
                    client._api_client,
                    parameters,
                    None,
                    parameters,
                )
        finally:
            client.close()

    def test_unknown_model_rejected_before_request(self) -> None:
        with self.assertRaises(GeminiRequestConfigurationError):
            build_model_generation_config(
                model="unknown-model",
                response_schema={},
                structured_output_enabled=True,
                thinking_level="high",
                max_output_tokens=1000,
                temperature=0.1,
            )

    def test_exception_taxonomy(self) -> None:
        class Error(Exception):
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code

        expected = {
            400: "gemini_request_configuration_error",
            401: "gemini_authentication_error",
            403: "gemini_authentication_error",
            404: "gemini_file_unavailable",
            429: "gemini_rate_limited",
            504: "gemini_timeout",
            500: "gemini_server_error",
        }
        for status, category in expected.items():
            with self.subTest(status=status):
                self.assertEqual(classify_gemini_exception(Error(status)), category)

    def test_safe_provider_error_taxonomy_uses_structured_sdk_fields(self) -> None:
        from google.genai.errors import ClientError, ServerError

        cases = (
            (ClientError(400, {"error": {"status": "INVALID_ARGUMENT"}}), "invalid_argument"),
            (
                ClientError(400, {"error": {"status": "FAILED_PRECONDITION"}}),
                "failed_precondition",
            ),
            (ClientError(401, {"error": {"status": "UNAUTHENTICATED"}}), "authentication_failed"),
            (ClientError(403, {"error": {"status": "PERMISSION_DENIED"}}), "permission_denied"),
            (ClientError(404, {"error": {"status": "NOT_FOUND"}}), "model_or_endpoint_not_found"),
            (ClientError(409, {"error": {"status": "ABORTED"}}), "conflict"),
            (ClientError(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}), "rate_limited"),
            (ServerError(500, {"error": {"status": "INTERNAL"}}), "provider_internal"),
            (ServerError(503, {"error": {"status": "UNAVAILABLE"}}), "provider_unavailable"),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                safe = safe_gemini_provider_error(
                    error,
                    provider_request_attempted=True,
                    failure_stage="provider_request",
                    model="gemini-3.1-flash-lite",
                )
                self.assertEqual(safe["provider_error_category"], expected)
                self.assertEqual(safe["provider_http_status"], error.code)
                self.assertNotIn("details", safe)
                self.assertNotIn("message", safe)

    def test_safe_provider_error_local_transport_and_parse_categories(self) -> None:
        from pydantic import BaseModel, ValidationError

        class TinyModel(BaseModel):
            ok: bool

        try:
            TinyModel.model_validate({})
        except ValidationError as validation_error:
            local_validation = validation_error

        cases = (
            (
                GeminiRequestConfigurationError("sensitive configuration"),
                False,
                "local_request_validation",
            ),
            (TimeoutError("sensitive timeout"), True, "timeout"),
            (ConnectionError("sensitive connection"), True, "connection_error"),
            (
                StructuredResponseError("gemini_json_parse_failed", raw_text="sensitive"),
                True,
                "response_parse_error",
            ),
            (local_validation, True, "local_request_validation"),
        )
        for error, attempted, expected in cases:
            with self.subTest(expected=expected):
                safe = safe_gemini_provider_error(
                    error,
                    provider_request_attempted=attempted,
                    failure_stage="diagnostic",
                )
                self.assertEqual(safe["provider_error_category"], expected)
                if error is local_validation:
                    self.assertFalse(safe["provider_request_attempted"])
                serialized = json.dumps(safe)
                self.assertNotIn("sensitive", serialized)

    def test_safe_detail_does_not_change_production_failure_category(self) -> None:
        class Error(Exception):
            status_code = 500
            status = "INTERNAL"

        error = Error("sensitive")
        self.assertEqual(classify_gemini_exception(error), "gemini_server_error")
        self.assertEqual(
            safe_gemini_provider_error(
                error,
                provider_request_attempted=True,
                failure_stage="gemini_structured_extraction",
            )["provider_error_category"],
            "provider_internal",
        )


if __name__ == "__main__":
    unittest.main()
