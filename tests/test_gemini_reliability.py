from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from importlib.metadata import version
from pathlib import Path

from app.ai.gemini_reliability import (
    GeminiRequestConfigurationError,
    StructuredResponseError,
    build_model_generation_config,
    classify_gemini_exception,
    google_response_schema,
    structural_schema_fingerprint,
    parse_structured_response,
)
from app.qa.replay import canonical_recipe, settings_for_case
from app.queue_worker import (
    CANONICAL_RECIPE_SCHEMA,
    EXPECTED_GOOGLE_GENAI_VERSION,
    EXPECTED_PYDANTIC_VERSION,
    build_gemini_generation_config,
    validate_production_gemini_configs,
    validate_and_normalize_canonical_recipe,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
    def test_exact_runtime_versions_are_declared_and_installed(self) -> None:
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertIn(
            f"google-genai=={EXPECTED_GOOGLE_GENAI_VERSION}",
            requirements,
        )
        self.assertIn(f"pydantic=={EXPECTED_PYDANTIC_VERSION}", requirements)
        self.assertEqual(
            version("google-genai"),
            EXPECTED_GOOGLE_GENAI_VERSION,
        )
        self.assertEqual(version("pydantic"), EXPECTED_PYDANTIC_VERSION)

    def test_exact_full_production_configurations_validate(self) -> None:
        result = validate_production_gemini_configs(settings_for_case())
        self.assertTrue(result["ready"])
        self.assertTrue(result["primary_ready"])
        self.assertTrue(result["fallback_ready"])
        self.assertFalse(result["schema_mutation_detected"])
        self.assertEqual(result["normalized_error_locations"], [])
        self.assertEqual(result["normalized_error_types"], [])

    def test_fallback_conversion_isolated_and_primary_revalidates(self) -> None:
        from google.genai import types

        settings = settings_for_case()
        original = copy.deepcopy(CANONICAL_RECIPE_SCHEMA)
        initial_fingerprint = structural_schema_fingerprint(
            CANONICAL_RECIPE_SCHEMA
        )
        primary = build_gemini_generation_config(
            settings,
            model=settings.gemini_primary_model,
            structured_schema=True,
        )
        fallback = build_gemini_generation_config(
            settings,
            model=settings.gemini_fallback_model,
            structured_schema=True,
        )
        second_primary = build_gemini_generation_config(
            settings,
            model=settings.gemini_primary_model,
            structured_schema=True,
        )
        self.assertIs(
            primary["response_json_schema"],
            CANONICAL_RECIPE_SCHEMA,
        )
        self.assertIsNot(
            primary["response_json_schema"],
            fallback["response_schema"],
        )
        self.assertEqual(CANONICAL_RECIPE_SCHEMA, original)
        self.assertEqual(
            structural_schema_fingerprint(CANONICAL_RECIPE_SCHEMA),
            initial_fingerprint,
        )
        self.assertEqual(
            structural_schema_fingerprint(primary["response_json_schema"]),
            structural_schema_fingerprint(
                second_primary["response_json_schema"]
            ),
        )
        types.GenerateContentConfig(**second_primary)

    def test_startup_import_order_matches_isolated_diagnostic(self) -> None:
        isolated = self._subprocess_validation(startup_order=False)
        startup = self._subprocess_validation(startup_order=True)
        self.assertEqual(isolated, startup)
        self.assertTrue(startup["ready"])

    def test_worker_qa_gemini_config_valid_and_invalid(self) -> None:
        valid = self._run_gemini_config_command({})
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertIn("primary_ready=true", valid.stdout)
        self.assertIn("fallback_ready=true", valid.stdout)
        self.assertIn("schema_mutation_detected=false", valid.stdout)
        self.assertIn("provider_call_attempted=false", valid.stdout)

        invalid = self._run_gemini_config_command(
            {"GEMINI_THINKING_LEVEL": "unsupported-fixture-level"}
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("primary_ready=false", invalid.stdout)
        self.assertIn(
            "normalized_error_locations=thinking_config.thinking_level",
            invalid.stdout,
        )
        self.assertIn("normalized_error_types=literal_error", invalid.stdout)
        self.assertIn("provider_call_attempted=false", invalid.stdout)
        self.assertNotIn("Traceback", invalid.stderr)

    def test_fallback_25_full_configuration_validates(self) -> None:
        from google.genai import types

        settings = settings_for_case()
        config = build_gemini_generation_config(
            settings,
            model="gemini-2.5-flash",
            structured_schema=True,
        )
        validated = types.GenerateContentConfig(**config)
        self.assertIsNotNone(validated.response_schema)
        self.assertIsNone(validated.thinking_config)
        servings = config["response_schema"]["properties"]["servings"]  # type: ignore[index]
        self.assertEqual(servings["nullable"], True)
        self.assertIn("anyOf", servings)

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
        nullable = {
            "type": "object",
            "properties": {"servings": {"type": ["string", "null"]}},
        }
        converted = google_response_schema(nullable)
        self.assertEqual(
            converted["properties"]["servings"],  # type: ignore[index]
            {"type": "string", "nullable": True},
        )

        from google.genai import types

        types.Schema.model_validate(converted)

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
            504: "gemini_server_error",
            500: "gemini_server_error",
        }
        for status, category in expected.items():
            with self.subTest(status=status):
                self.assertEqual(classify_gemini_exception(Error(status)), category)

        class ValidationError(Exception):
            pass

        self.assertEqual(
            classify_gemini_exception(
                ValidationError(),
                provider_request_attempted=False,
            ),
            "gemini_request_configuration_error",
        )
        self.assertNotEqual(
            classify_gemini_exception(
                RuntimeError(),
                provider_request_attempted=False,
            ),
            "gemini_server_error",
        )

    def _subprocess_validation(self, *, startup_order: bool) -> dict[str, object]:
        imports = (
            "import app.workers.combined_worker; import app.main;"
            if startup_order
            else ""
        )
        code = (
            "import json;"
            f"{imports}"
            "from app.config import Settings;"
            "from app.queue_worker import validate_production_gemini_configs;"
            "s=Settings(_env_file=None,worker_api_secret='offline',"
            "supabase_url='https://offline.invalid',"
            "supabase_service_role_key='offline',app_env='production');"
            "r=validate_production_gemini_configs(s);"
            "print(json.dumps({k:r[k] for k in "
            "('ready','primary_ready','fallback_ready',"
            "'primary_schema_fingerprint','fallback_schema_fingerprint',"
            "'schema_mutation_detected')},sort_keys=True))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            env=self._diagnostic_env({}),
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(completed.stdout)

    def _run_gemini_config_command(
        self,
        overrides: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/worker_qa.py", "gemini-config"],
            cwd=PROJECT_ROOT,
            env=self._diagnostic_env(overrides),
            capture_output=True,
            text=True,
            check=False,
        )

    def _diagnostic_env(self, overrides: dict[str, str]) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GEMINI_")
            and key
            not in {
                "APP_ENV",
                "RENDER",
                "RENDER_GIT_BRANCH",
                "WORKER_CONFIG_DIR",
            }
        }
        environment["APP_ENV"] = "production"
        environment.update(overrides)
        return environment


if __name__ == "__main__":
    unittest.main()
