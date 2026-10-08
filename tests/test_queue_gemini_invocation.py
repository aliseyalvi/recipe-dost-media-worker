from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from google.genai import types
from google.genai.errors import ServerError

from app import main
from app.config import Settings
from app.qa.replay import (
    OfflineRepository,
    canonical_recipe,
    fixture_response,
    make_job,
    settings_for_case,
)
from app.queue_worker import (
    CANONICAL_RECIPE_PROMPT,
    CANONICAL_RECIPE_SCHEMA,
    QueueJobFailure,
    _GEMINI_CALL_CONTEXT,
    create_gemini_client,
    run_single_pass_structured_extraction,
)


class FailingEventRepository(OfflineRepository):
    def __init__(self, failing_events: set[str]) -> None:
        super().__init__()
        self.failing_events = failing_events

    def insert_event(
        self,
        media_job_id,
        *,
        event_type: str,
        message: str,
        metadata=None,
    ) -> None:
        if event_type in self.failing_events:
            raise RuntimeError("telemetry fixture failure")
        super().insert_event(
            media_job_id,
            event_type=event_type,
            message=message,
            metadata=metadata,
        )


def request_fingerprint(call: dict[str, object], *, api_version: str) -> dict[str, object]:
    contents = call["contents"]
    config = call["config"]
    assert isinstance(contents, list)
    assert isinstance(config, dict)
    input_part = contents[0]
    file_data = getattr(input_part, "file_data", None)
    prompt = contents[1]
    assert isinstance(prompt, str)
    schema_field = (
        "response_json_schema"
        if "response_json_schema" in config
        else "response_schema"
    )
    return {
        "model": call["model"],
        "content_count": len(contents),
        "content_types": tuple(type(item).__name__ for item in contents),
        "youtube_part_type": type(input_part).__name__,
        "youtube_mime_type": getattr(file_data, "mime_type", None),
        "schema_field": schema_field,
        "schema_is_canonical": config.get(schema_field) is CANONICAL_RECIPE_SCHEMA,
        "response_mime_type": config.get("response_mime_type"),
        "system_instruction_present": "system_instruction" in config,
        "prompt_template_identity": hashlib.sha256(
            CANONICAL_RECIPE_PROMPT.encode("utf-8")
        ).hexdigest(),
        "actual_prompt_identity": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "thinking_config": config.get("thinking_config"),
        "max_output_tokens": config.get("max_output_tokens"),
        "temperature": config.get("temperature"),
        "api_version": api_version,
    }


class QueueGeminiInvocationTests(unittest.TestCase):
    def run_queue_extraction(
        self,
        *,
        repository: OfflineRepository,
        client: MagicMock,
        settings=None,
        index: int = 901,
        input_part: object | None = None,
    ):
        active_settings = settings or settings_for_case()
        job = make_job(index, "youtube").model_copy(
            update={"status": "analyzing", "progress": 60}
        )
        repository.register(job)
        token = _GEMINI_CALL_CONTEXT.set(
            (repository, job, active_settings)  # type: ignore[arg-type]
        )
        try:
            return run_single_pass_structured_extraction(
                repository=repository,  # type: ignore[arg-type]
                job=job,
                settings=active_settings,
                client=client,
                input_part=input_part or object(),
                input_mode="youtube_url",
                source_metadata={"platform": "youtube"},
                platform="youtube",
            )
        finally:
            _GEMINI_CALL_CONTEXT.reset(token)

    def successful_client(self) -> MagicMock:
        client = MagicMock()
        client.models.generate_content.return_value = fixture_response(
            json.dumps(canonical_recipe(), ensure_ascii=False)
        )
        return client

    def test_queue_success_invokes_provider_once_without_fallback(self) -> None:
        repository = OfflineRepository()
        client = self.successful_client()

        result, model, repair_used = self.run_queue_extraction(
            repository=repository,
            client=client,
        )

        self.assertEqual(result.recipe_title_en, "Fixture Curry")
        self.assertEqual(model, "gemini-3.1-flash-lite")
        self.assertFalse(repair_used)
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertFalse(
            any(
                event["event_type"] == "gemini_structured_extraction_fallback_started"
                for event in repository.events
            )
        )

    def test_all_call_telemetry_can_fail_without_fallback(self) -> None:
        repository = FailingEventRepository(
            {
                "gemini_structured_extraction_started",
                "gemini_api_call_started",
                "gemini_api_call_completed",
                "gemini_structured_extraction_completed",
            }
        )
        client = self.successful_client()

        result, model, _repair_used = self.run_queue_extraction(
            repository=repository,
            client=client,
        )

        self.assertEqual(result.recipe_title_en, "Fixture Curry")
        self.assertEqual(model, "gemini-3.1-flash-lite")
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_primary_failure_and_fallback_success_make_two_provider_calls(self) -> None:
        repository = FailingEventRepository({"gemini_api_call_failed"})
        repository.count_job_gemini_calls = MagicMock(
            side_effect=RuntimeError("database count fixture failure")
        )
        repository.count_daily_gemini_calls = MagicMock(
            side_effect=RuntimeError("database count fixture failure")
        )
        client = MagicMock()
        client.models.generate_content.side_effect = [
            RuntimeError("provider fixture failure"),
            fixture_response(json.dumps(canonical_recipe(), ensure_ascii=False)),
        ]

        result, model, _repair_used = self.run_queue_extraction(
            repository=repository,
            client=client,
        )

        self.assertEqual(result.recipe_title_en, "Fixture Curry")
        self.assertEqual(model, "gemini-2.5-flash")
        self.assertEqual(client.models.generate_content.call_count, 2)
        repository.count_job_gemini_calls.assert_not_called()
        repository.count_daily_gemini_calls.assert_not_called()

    def test_retryable_primary_503_uses_model_compatible_fallback_config(self) -> None:
        repository = OfflineRepository()
        client = MagicMock()
        client.models.generate_content.side_effect = [
            ServerError(503, {"error": {"status": "UNAVAILABLE"}}),
            fixture_response(json.dumps(canonical_recipe(), ensure_ascii=False)),
        ]

        result, model, _repair_used = self.run_queue_extraction(
            repository=repository,
            client=client,
        )

        self.assertEqual(result.recipe_title_en, "Fixture Curry")
        self.assertEqual(model, "gemini-2.5-flash")
        self.assertEqual(client.models.generate_content.call_count, 2)
        primary_config = client.models.generate_content.call_args_list[0].kwargs[
            "config"
        ]
        fallback_config = client.models.generate_content.call_args_list[1].kwargs[
            "config"
        ]
        self.assertEqual(
            primary_config["thinking_config"],
            {"thinking_level": "high"},
        )
        self.assertEqual(
            fallback_config["response_schema"]["properties"]["time"]["properties"][
                "prep_minutes"
            ]["type"],
            "integer",
        )
        self.assertTrue(
            fallback_config["response_schema"]["properties"]["time"]["properties"][
                "prep_minutes"
            ]["nullable"]
        )
        self.assertNotIn("thinking_config", fallback_config)

    def test_failed_primary_and_fallback_report_two_actual_attempts(self) -> None:
        repository = OfflineRepository()
        client = MagicMock()
        client.models.generate_content.side_effect = [
            RuntimeError("primary provider fixture failure"),
            RuntimeError("fallback provider fixture failure"),
        ]
        settings = settings_for_case(
            gemini_max_calls_per_job=2,
            recipe_repair_enabled=False,
        )

        with self.assertRaises(QueueJobFailure) as captured:
            self.run_queue_extraction(
                repository=repository,
                client=client,
                settings=settings,
            )

        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(captured.exception.metadata["provider_attempt"], 2)
        self.assertNotEqual(captured.exception.metadata["provider_attempt"], 0)

    def test_temporary_diagnostic_surface_is_removed(self) -> None:
        self.assertNotIn("allow_gemini_diagnostic", Settings.model_fields)
        self.assertNotIn("app.ai.gemini_diagnostic", sys.modules)
        self.assertIsNone(importlib.util.find_spec("app.ai.gemini_diagnostic"))
        self.assertFalse(
            any(
                getattr(route, "path", None) == "/debug/gemini-request-ladder"
                for route in main.app.routes
            )
        )

        settings = settings_for_case()
        main.app.dependency_overrides[main.get_settings] = lambda: settings
        try:
            client = TestClient(main.app)
            self.assertEqual(
                client.post("/debug/gemini-request-ladder", json={}).status_code,
                404,
            )
            self.assertEqual(client.get("/health").status_code, 200)
        finally:
            main.app.dependency_overrides.clear()

    def test_queue_request_fingerprint_matches_gd8q2_snapshot(self) -> None:
        settings = settings_for_case(recipe_repair_enabled=False)
        sdk_client = create_gemini_client(
            "offline-fixture-key",
            settings.gemini_file_upload_timeout_seconds,
        )
        api_version = sdk_client._api_client._http_options.api_version
        sdk_client.close()
        self.assertEqual(api_version, "v1beta")
        input_part = types.Part.from_uri(
            file_uri="https://www.youtube.com/watch?v=abcdefghijk",
            mime_type="video/mp4",
        )
        queue_client = self.successful_client()
        self.run_queue_extraction(
            repository=OfflineRepository(),
            client=queue_client,
            settings=settings,
            input_part=input_part,
        )
        queue_call = queue_client.models.generate_content.call_args.kwargs
        self.assertEqual(
            request_fingerprint(queue_call, api_version=api_version),
            {
                "model": "gemini-3.1-flash-lite",
                "content_count": 2,
                "content_types": ("Part", "str"),
                "youtube_part_type": "Part",
                "youtube_mime_type": "video/mp4",
                "schema_field": "response_json_schema",
                "schema_is_canonical": True,
                "response_mime_type": "application/json",
                "system_instruction_present": False,
                "prompt_template_identity": (
                    "5d8d998a6bfbd9a8d76819e316d735bdf3c7509c216bac3e761f798e534f55d8"
                ),
                "actual_prompt_identity": (
                    "5d8d998a6bfbd9a8d76819e316d735bdf3c7509c216bac3e761f798e534f55d8"
                ),
                "thinking_config": {"thinking_level": "high"},
                "max_output_tokens": 12000,
                "temperature": 0.1,
                "api_version": "v1beta",
            },
        )


if __name__ == "__main__":
    unittest.main()
