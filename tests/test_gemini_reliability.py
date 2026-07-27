from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

from app.ai.gemini_reliability import (
    GeminiRequestConfigurationError,
    StructuredResponseError,
    build_model_generation_config,
    classify_gemini_exception,
    google_response_schema,
    sdk_failure_evidence,
    structural_schema_fingerprint,
    parse_structured_response,
)
from app.qa.replay import canonical_recipe, settings_for_case
from app.queue_worker import (
    CANONICAL_RECIPE_SCHEMA,
    CanonicalRecipeResponse,
    EXPECTED_GOOGLE_GENAI_VERSION,
    EXPECTED_PYDANTIC_VERSION,
    build_gemini_generation_config,
    build_prepared_gemini_request,
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
        self.assertTrue(result["gemini_config_ready"])
        self.assertTrue(result["gemini_request_shape_ready"])
        self.assertEqual(result["gemini_live_canary_verified"], "unknown")
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
        self.assertIn("response_schema", primary)
        self.assertNotIn("response_json_schema", primary)
        self.assertIsNot(
            primary["response_schema"],
            fallback["response_schema"],
        )
        self.assertEqual(CANONICAL_RECIPE_SCHEMA, original)
        self.assertEqual(
            structural_schema_fingerprint(CANONICAL_RECIPE_SCHEMA),
            initial_fingerprint,
        )
        self.assertEqual(
            primary["response_schema"],
            CanonicalRecipeResponse,
        )
        self.assertIs(
            primary["response_schema"],
            second_primary["response_schema"],
        )
        types.GenerateContentConfig(**second_primary)

    def test_startup_import_order_matches_isolated_diagnostic(self) -> None:
        isolated = self._subprocess_validation(startup_order=False)
        startup = self._subprocess_validation(startup_order=True)
        self.assertEqual(isolated, startup)
        self.assertTrue(startup["ready"])

    def test_primary_defaults_to_last_known_good_response_schema_shape(self) -> None:
        settings = settings_for_case()
        self.assertEqual(settings.gemini_primary_schema_mode, "response_schema")
        with patch(
            "app.ai.gemini_reliability.google_response_schema",
            wraps=google_response_schema,
        ) as fallback_converter:
            config = build_gemini_generation_config(
                settings,
                model=settings.gemini_primary_model,
                structured_schema=True,
            )
        fallback_converter.assert_not_called()
        self.assertEqual(config["response_mime_type"], "application/json")
        self.assertEqual(
            config["thinking_config"],
            {"thinking_level": "high"},
        )
        self.assertIn("response_schema", config)
        self.assertNotIn("response_json_schema", config)
        self.assertIs(config["response_schema"], CanonicalRecipeResponse)
        model_schema = CanonicalRecipeResponse.model_json_schema()
        canonical_properties = CANONICAL_RECIPE_SCHEMA["properties"]
        self.assertEqual(
            set(model_schema["properties"]),
            set(canonical_properties),  # type: ignore[arg-type]
        )
        self.assertEqual(
            set(model_schema["required"]),
            set(CANONICAL_RECIPE_SCHEMA["required"]),  # type: ignore[arg-type]
        )

    def test_prepared_file_request_uses_explicit_sdk_parts(self) -> None:
        from google.genai import types

        uploaded = type(
            "UploadedFile",
            (),
            {
                "uri": "https://offline.invalid/files/fixture",
                "mime_type": "video/mp4",
            },
        )()
        prepared = build_prepared_gemini_request(
            settings_for_case(),
            model_id="gemini-3.1-flash-lite",
            input_part=uploaded,
            prompt="Offline request-shape fixture.",
            input_mode="file_api",
        )
        self.assertIsInstance(prepared.config, types.GenerateContentConfig)
        self.assertEqual(prepared.input_representation_category, "file_uri_part")
        self.assertEqual(len(prepared.contents), 1)
        content = prepared.contents[0]
        self.assertIsInstance(content, types.Content)
        self.assertEqual(len(content.parts or []), 2)
        assert content.parts is not None
        self.assertIsInstance(content.parts[0], types.Part)
        self.assertIsNotNone(content.parts[0].file_data)
        self.assertIsInstance(content.parts[1], types.Part)
        self.assertEqual(
            prepared.configuration_category,
            "response_schema_thinking_level",
        )

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

    def test_owner_live_diagnostic_refuses_without_development_gate(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "scripts/worker_qa.py",
                "gemini-request",
                "--mode",
                "text",
                "--variant",
                "response-schema",
            ],
            cwd=PROJECT_ROOT,
            env=self._diagnostic_env({}),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("APP_ENV=development", completed.stderr)
        self.assertNotIn("sdk_invocation_started=true", completed.stdout)
        self.assertNotIn("Traceback", completed.stderr)

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
        self.assertIn("response_schema", primary)
        self.assertNotIn("response_json_schema", primary)
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

    def test_sdk_failure_evidence_separates_local_and_provider_status(self) -> None:
        from google.genai import types
        from pydantic import ValidationError

        try:
            types.Part.model_validate({"text": 123})
        except ValidationError as exc:
            local = sdk_failure_evidence(
                exc,
                sdk_invocation_attempted=True,
            )
        else:
            self.fail("Fixture must produce a real Pydantic ValidationError.")
        self.assertEqual(local["failure_stage"], "sdk_request_transformation")
        self.assertEqual(local["provider_status_class"], "none")
        self.assertFalse(local["http_status_observed"])

        class ProviderError(Exception):
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code

        rejected = sdk_failure_evidence(
            ProviderError(400),
            sdk_invocation_attempted=True,
        )
        server = sdk_failure_evidence(
            ProviderError(503),
            sdk_invocation_attempted=True,
        )
        self.assertEqual(rejected["failure_stage"], "provider_rejection")
        self.assertEqual(rejected["provider_status_class"], "4xx")
        self.assertEqual(server["failure_stage"], "provider_server")
        self.assertEqual(server["provider_status_class"], "5xx")

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
            "('ready','primary_ready','fallback_ready','gemini_config_ready',"
            "'gemini_request_shape_ready',"
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
