from __future__ import annotations

import json
import unittest

from app.ai.gemini_reliability import (
    GeminiRequestConfigurationError,
    StructuredResponseError,
    build_model_generation_config,
    classify_gemini_exception,
    parse_structured_response,
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


if __name__ == "__main__":
    unittest.main()
