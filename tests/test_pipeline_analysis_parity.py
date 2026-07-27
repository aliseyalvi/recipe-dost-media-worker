from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import main as app_main
from app.pipeline.contracts import (
    DispatchEnvelope,
    IngestedMedia,
    SourceContext,
    StageTimestamps,
)
from app.pipeline.services import (
    analyze_media,
    cleanup_local_media,
    finalize_result,
)
from app.qa.replay import (
    FixtureClientError,
    FixtureGeminiClient,
    OfflineRepository,
    canonical_recipe,
    fixture_response,
    make_job,
    settings_for_case,
)
from app.queue_worker import (
    CANONICAL_RECIPE_SCHEMA,
    QueueJobFailure,
    _GEMINI_CALL_CONTEXT,
    build_prepared_gemini_request,
    finalize_quota_transition_safely,
    guarded_generate_content,
    make_gemini_runtime_context,
    record_queue_failure,
    run_gemini_video_poc_analysis,
)
from app.quota_finalization import ensure_quota_reservation_event


class RecordingGeminiClient(FixtureGeminiClient):
    def __init__(
        self,
        responses: list[object],
        media_path: Path,
        repository: OfflineRepository,
        job_id: object,
    ) -> None:
        super().__init__(responses)
        self.media_path = media_path
        self.repository = repository
        self.job_id = job_id
        self.request_shapes: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> object:
        from google.genai import types

        if not self.media_path.is_file():
            raise FileNotFoundError("fixture media was removed before analysis")
        config = kwargs.get("config")
        config_dict = (
            config.model_dump(exclude_none=True)
            if isinstance(config, types.GenerateContentConfig)
            else config
            if isinstance(config, dict)
            else {}
        )
        contents = kwargs.get("contents")
        content_values = contents if isinstance(contents, list) else []
        sdk_events = [
            event
            for event in self.repository.events
            if event["media_job_id"] == self.job_id
            and event["event_type"] == "gemini_sdk_invocation_started"
        ]
        sdk_entry = (
            dict(sdk_events[-1]["metadata"])
            if sdk_events and isinstance(sdk_events[-1]["metadata"], dict)
            else {}
        )
        part_categories: list[str] = []
        for content in content_values:
            if not isinstance(content, types.Content):
                part_categories.append(type(content).__name__)
                continue
            for part in content.parts or []:
                part_categories.append(
                    "file_uri_part"
                    if part.file_data is not None
                    else "text_part"
                    if part.text is not None
                    else "other_part"
                )
        self.request_shapes.append(
            {
                "model": str(kwargs.get("model") or ""),
                "config_keys": sorted(config_dict),
                "schema_field": (
                    "response_json_schema"
                    if "response_json_schema" in config_dict
                    else "response_schema"
                    if "response_schema" in config_dict
                    else "none"
                ),
                "thinking": "level" if "thinking_config" in config_dict else "default",
                "content_shape": [
                    type(value).__name__ for value in content_values
                ],
                "part_categories": part_categories,
                "sdk_started_before_method": bool(sdk_events),
                "entry_call_number": int(sdk_entry.get("call_number") or 0),
                "entry_calls_remaining": int(
                    sdk_entry.get("calls_remaining") or 0
                ),
            }
        )
        return super().generate_content(**kwargs)


class PipelineAnalysisParityTests(unittest.TestCase):
    def tearDown(self) -> None:
        app_main.reset_gemini_request_config_readiness_cache()
        app_main.queue_task = None

    def test_primary_preflight_records_exact_safe_exception_category(self) -> None:
        result = self._run(
            "staged",
            [
                fixture_response(
                    json.dumps(canonical_recipe(), ensure_ascii=False)
                )
            ],
            {
                "gemini_thinking_level": "unsupported-fixture-level",
                "recipe_repair_enabled": False,
            },
        )
        preflight = result["primary_preflight"]
        self.assertEqual(
            preflight["preflight_category"],  # type: ignore[index]
            "invalid_model_configuration",
        )
        self.assertEqual(
            preflight["exception_type"],  # type: ignore[index]
            "GeminiRequestConfigurationError",
        )
        self.assertFalse(
            preflight["provider_request_attempted"]  # type: ignore[index]
        )
        self.assertNotIn("exception_message", preflight)

    def test_staged_known_success_starts_primary_31_request(self) -> None:
        result = self._run(
            "staged",
            [
                fixture_response(
                    json.dumps(canonical_recipe(), ensure_ascii=False)
                )
            ],
            {},
        )
        self.assertEqual(result["terminal_status"], "completed")
        self.assertEqual(result["call_purposes"], ["analysis"])
        self.assertEqual(result["call_numbers"], [1])
        self.assertIn("gemini_api_call_started", result["analysis_events"])
        self.assertIn("gemini_request_shape_prepared", result["analysis_events"])
        self.assertIn("gemini_sdk_invocation_started", result["analysis_events"])
        self.assertEqual(
            result["request_shapes"][0]["model"],  # type: ignore[index]
            "gemini-3.1-flash-lite",
        )
        self.assertTrue(
            result["request_shapes"][0]["sdk_started_before_method"]  # type: ignore[index]
        )
        self.assertEqual(
            result["request_shapes"][0]["entry_call_number"],  # type: ignore[index]
            1,
        )
        self.assertEqual(
            result["request_shapes"][0]["entry_calls_remaining"],  # type: ignore[index]
            2,
        )
        self.assertEqual(
            result["request_shapes"][0]["part_categories"],  # type: ignore[index]
            ["file_uri_part", "text_part"],
        )
        self.assertNotIn("offline-file-reference", result["event_payload"])
        self.assertTrue(result["cleanup"])

    def test_sequential_and_staged_analysis_are_identical(self) -> None:
        valid = fixture_response(json.dumps(canonical_recipe(), ensure_ascii=False))
        cases = {
            "primary_success": ([valid], {}),
            "malformed_repaired": (
                [
                    fixture_response('{"is_recipe":true,"title_en":'),
                    valid,
                ],
                {},
            ),
            "provider_503_retry_success": (
                [FixtureClientError(503), valid],
                {"gemini_retry_base_seconds": 0.0},
            ),
            "fallback_success": (
                [fixture_response(None), valid],
                {"recipe_repair_enabled": False},
            ),
            "primary_capability_preflight": (
                [valid],
                {
                    "gemini_primary_model": "unsupported-primary",
                    "recipe_repair_enabled": False,
                },
            ),
        }
        for case_name, (responses, overrides) in cases.items():
            with self.subTest(case=case_name):
                sequential = self._run(
                    "sequential",
                    list(responses),
                    dict(overrides),
                )
                staged = self._run("staged", list(responses), dict(overrides))
                self.assertEqual(
                    sequential["terminal_status"],
                    staged["terminal_status"],
                )
                self.assertEqual(sequential["call_purposes"], staged["call_purposes"])
                self.assertEqual(sequential["request_hash"], staged["request_hash"])
                self.assertEqual(
                    sequential["analysis_events"],
                    staged["analysis_events"],
                )
                self.assertEqual(sequential["quota_success"], staged["quota_success"])
                self.assertTrue(sequential["cleanup"])
                self.assertTrue(staged["cleanup"])
                expected_purposes = {
                    "primary_success": ["analysis"],
                    "malformed_repaired": ["analysis", "repair"],
                    "provider_503_retry_success": ["analysis", "transient_retry"],
                    "fallback_success": ["analysis", "model_fallback"],
                    "primary_capability_preflight": [],
                }[case_name]
                self.assertEqual(staged["call_purposes"], expected_purposes)
                self.assertEqual(
                    staged["call_numbers"],
                    list(range(1, len(expected_purposes) + 1)),
                )

        success = self._run("staged", [valid], {})
        self.assertEqual(success["call_purposes"], ["analysis"])
        self.assertIn("gemini_api_call_started", success["analysis_events"])
        self.assertEqual(
            success["request_shapes"][0]["schema_field"],  # type: ignore[index]
            "response_schema",
        )
        self.assertEqual(
            success["request_shapes"][0]["thinking"],  # type: ignore[index]
            "level",
        )
        fallback = self._run(
            "staged",
            [fixture_response(None), valid],
            {"recipe_repair_enabled": False},
        )
        self.assertEqual(
            fallback["request_shapes"][1]["schema_field"],  # type: ignore[index]
            "response_schema",
        )
        self.assertEqual(
            fallback["request_shapes"][1]["thinking"],  # type: ignore[index]
            "default",
        )

    def test_missing_file_reference_fails_preflight_and_cleans(self) -> None:
        for mode in ("sequential", "staged"):
            with self.subTest(mode=mode):
                result = self._run(
                    mode,
                    [fixture_response(json.dumps(canonical_recipe()))],
                    {},
                    missing_file_reference=True,
                )
                self.assertEqual(result["terminal_status"], "failed")
                self.assertEqual(result["call_purposes"], [])
                self.assertIn(
                    "gemini_primary_preflight_failed",
                    result["analysis_events"],
                )
                self.assertTrue(result["cleanup"])

    def test_sdk_and_provider_failures_are_classified_at_the_boundary(self) -> None:
        from google.genai import types
        from pydantic import ValidationError

        try:
            types.Part.model_validate({"text": 123})
        except ValidationError as validation_error:
            local_error = validation_error
        else:
            self.fail("Fixture must produce a Pydantic ValidationError.")
        cases = (
            (
                "local_validation",
                local_error,
                "sdk_request_transformation",
                "none",
                False,
            ),
            (
                "provider_400",
                FixtureClientError(400),
                "provider_rejection",
                "4xx",
                True,
            ),
            (
                "provider_503",
                FixtureClientError(503),
                "provider_server",
                "5xx",
                True,
            ),
        )
        for name, failure, stage, status_class, http_observed in cases:
            with self.subTest(case=name):
                result = self._run(
                    "sequential",
                    [failure],
                    {
                        "gemini_max_retries": 0,
                        "gemini_fallback_model": "",
                        "recipe_repair_enabled": False,
                    },
                )
                self.assertEqual(result["terminal_status"], "failed")
                self.assertEqual(result["call_numbers"], [1])
                evidence = result["sdk_failures"][0]  # type: ignore[index]
                self.assertEqual(evidence["failure_stage"], stage)
                self.assertEqual(
                    evidence["provider_status_class"],
                    status_class,
                )
                self.assertEqual(
                    evidence["http_status_observed"],
                    http_observed,
                )
                self.assertTrue(evidence["sdk_invocation_attempted"])

    def test_per_job_call_budgets_are_independent(self) -> None:
        settings = settings_for_case(gemini_max_calls_per_job=3)
        repository = OfflineRepository()
        jobs = [make_job(93, "tiktok"), make_job(94, "instagram")]
        for job in jobs:
            repository.register(job)
        contexts = [
            make_gemini_runtime_context(
                repository,  # type: ignore[arg-type]
                job,
                settings,
            )
            for job in jobs
        ]
        clients = [
            FixtureGeminiClient([fixture_response("{}"), fixture_response("{}")]),
            FixtureGeminiClient([fixture_response("{}")]),
        ]
        token = _GEMINI_CALL_CONTEXT.set(contexts[0])
        try:
            guarded_generate_content(
                clients[0],
                model=settings.gemini_primary_model,
                contents=["fixture"],
                stage="gemini_structured_extraction",
            )
            guarded_generate_content(
                clients[0],
                model=settings.gemini_primary_model,
                contents=["fixture"],
                stage="gemini_structured_extraction",
            )
        finally:
            _GEMINI_CALL_CONTEXT.reset(token)
        token = _GEMINI_CALL_CONTEXT.set(contexts[1])
        try:
            guarded_generate_content(
                clients[1],
                model=settings.gemini_primary_model,
                contents=["fixture"],
                stage="gemini_structured_extraction",
            )
        finally:
            _GEMINI_CALL_CONTEXT.reset(token)
        numbers_by_job = {
            job.id: [
                event["metadata"]["call_number"]
                for event in repository.events
                if event["media_job_id"] == job.id
                and event["event_type"] == "gemini_api_call_started"
            ]
            for job in jobs
        }
        self.assertEqual(numbers_by_job[jobs[0].id], [1, 2])
        self.assertEqual(numbers_by_job[jobs[1].id], [1])
        self.assertIsNot(contexts[0].budget, contexts[1].budget)

    def test_invalid_startup_config_blocks_sequential_and_staged_launch(self) -> None:
        invalid = self._invalid_readiness()
        for mode in ("sequential", "staged"):
            with self.subTest(mode=mode):
                app_main.reset_gemini_request_config_readiness_cache()
                settings = settings_for_case(
                    worker_execution_mode="queue",
                    worker_pipeline_mode=mode,
                )
                with patch.object(
                    app_main,
                    "get_settings",
                    return_value=settings,
                ), patch.object(
                    app_main,
                    "validate_external_resolver_runtime_config",
                ), patch.object(
                    app_main,
                    "cleanup_stale_media",
                    return_value=0,
                ), patch.object(
                    app_main,
                    "validate_production_gemini_configs",
                    return_value=invalid,
                ), patch.object(
                    app_main,
                    "run_queue_loop",
                ) as sequential, patch.object(
                    app_main,
                    "run_combined_pipeline",
                ) as staged, patch.object(
                    app_main,
                    "create_repository",
                ) as repository_factory, patch(
                    "app.queue_worker.ensure_quota_reservation_event_safely"
                ) as reserve, patch(
                    "app.queue_worker.create_gemini_client"
                ) as provider:
                    import asyncio

                    asyncio.run(app_main.start_queue_worker())
                    wake_result = asyncio.run(app_main.wake(settings))
                self.assertFalse(wake_result["queue_loop_enabled"])
                self.assertTrue(wake_result["configuration_not_ready"])
                sequential.assert_not_called()
                staged.assert_not_called()
                repository_factory.assert_not_called()
                reserve.assert_not_called()
                provider.assert_not_called()

    def test_sequential_and_staged_use_same_canonical_builder(self) -> None:
        valid = fixture_response(json.dumps(canonical_recipe(), ensure_ascii=False))
        for mode in ("sequential", "staged"):
            with self.subTest(mode=mode):
                with patch(
                    "app.queue_worker.build_prepared_gemini_request",
                    wraps=build_prepared_gemini_request,
                ) as builder:
                    result = self._run(mode, [valid], {})
                self.assertEqual(result["terminal_status"], "completed")
                models = {
                    str(call.kwargs.get("model_id") or "")
                    for call in builder.call_args_list
                }
                self.assertIn("gemini-3.1-flash-lite", models)

    def test_schema_mutation_fails_primary_without_call_or_fallback(self) -> None:
        original = deepcopy(CANONICAL_RECIPE_SCHEMA)
        try:
            properties = CANONICAL_RECIPE_SCHEMA["properties"]
            assert isinstance(properties, dict)
            properties["servings"] = {"type": "boolean"}
            result = self._run(
                "sequential",
                [fixture_response(json.dumps(canonical_recipe()))],
                {"recipe_repair_enabled": False},
            )
        finally:
            CANONICAL_RECIPE_SCHEMA.clear()
            CANONICAL_RECIPE_SCHEMA.update(original)
        self.assertEqual(result["terminal_status"], "failed")
        self.assertEqual(result["call_purposes"], [])
        self.assertIn(
            "gemini_primary_preflight_failed",
            result["analysis_events"],
        )
        self.assertIn(
            "gemini_structured_extraction_fallback_skipped",
            result["analysis_events"],
        )
        self.assertEqual(
            result["primary_preflight"]["category"],  # type: ignore[index]
            "schema_mutation_detected",
        )
        self.assertFalse(
            result["primary_preflight"]["provider_request_attempted"]  # type: ignore[index]
        )

    def _run(
        self,
        mode: str,
        responses: list[object],
        overrides: dict[str, object],
        *,
        missing_file_reference: bool = False,
    ) -> dict[str, object]:
        settings = settings_for_case(
            gemini_api_key="fixture",
            worker_delete_temp_after_gemini_active=False,
            **overrides,
        )
        repository = OfflineRepository()
        job = make_job(90 if mode == "sequential" else 91, "tiktok").model_copy(
            update={"status": "resolving", "progress": 20}
        )
        repository.register(job)
        ensure_quota_reservation_event(repository, job)
        with tempfile.TemporaryDirectory() as root:
            media_path = Path(root) / "video.mp4"
            media_path.write_bytes(b"fixture-media")
            client = RecordingGeminiClient(
                responses,
                media_path,
                repository,
                job.id,
            )
            uploaded = (
                object()
                if missing_file_reference
                else SimpleNamespace(
                    name="files/offline-fixture",
                    uri="offline-file-reference",
                    mime_type="video/mp4",
                )
            )

            def upload(**_kwargs: object) -> object:
                if not media_path.is_file():
                    raise FileNotFoundError("upload source missing")
                return uploaded

            extract_result = {
                "platform": "tiktok",
                "resolver": "offline_fixture",
                "output_mode": "temp_file",
                "video_bytes_verified": True,
                "temp_file_path": str(media_path),
                "size_bytes": media_path.stat().st_size,
                "duration_seconds": 30,
                "width": 720,
                "height": 1280,
                "title": "Fixture",
                "source_context": {"caption": "Fixture caption"},
            }
            with patch(
                "app.queue_worker.create_gemini_client",
                return_value=client,
            ), patch(
                "app.pipeline.services.create_gemini_client",
                return_value=client,
            ), patch(
                "app.queue_worker.upload_gemini_video_file",
                side_effect=upload,
            ), patch(
                "app.queue_worker.wait_for_gemini_file_active",
                return_value=uploaded,
            ), patch("app.queue_worker.time.sleep", return_value=None):
                try:
                    if mode == "sequential":
                        token = _GEMINI_CALL_CONTEXT.set(
                            make_gemini_runtime_context(
                                repository,  # type: ignore[arg-type]
                                job,
                                settings,
                            )
                        )
                        try:
                            completed = run_gemini_video_poc_analysis(
                                repository=repository,  # type: ignore[arg-type]
                                job=job,
                                settings=settings,
                                extract_result=extract_result,
                                platform="tiktok",
                                uploaded_thumbnail_url=None,
                            )
                        finally:
                            _GEMINI_CALL_CONTEXT.reset(token)
                        finalize_quota_transition_safely(
                            repository,  # type: ignore[arg-type]
                            completed,
                        )
                    else:
                        media = IngestedMedia(
                            dispatch=DispatchEnvelope(
                                job_id=job.id,
                                platform="tiktok",
                            ),
                            normalized_platform="tiktok",
                            resolver_category="offline_fixture",
                            media_mode="progressive",
                            output_mode="temp_file",
                            video_bytes_verified=True,
                            size_bytes=media_path.stat().st_size,
                            duration_seconds=30,
                            width=720,
                            height=1280,
                            size_category="under_64mb",
                            duration_category="under_31s",
                            dimension_category="541_720",
                            source_context=SourceContext(
                                platform="tiktok",
                                title="Fixture",
                                caption="Fixture caption",
                            ),
                            cleanup_obligations=["local_media"],
                            timestamps=StageTimestamps(),
                            local_media_path=str(media_path),
                        )
                        analysis = analyze_media(
                            repository,  # type: ignore[arg-type]
                            settings,
                            media,
                        )
                        finalize_result(
                            repository,  # type: ignore[arg-type]
                            settings,
                            analysis,
                            0.0,
                        )
                except QueueJobFailure as exc:
                    record_queue_failure(
                        repository,  # type: ignore[arg-type]
                        job.id,
                        exc.code,
                        exc.message,
                        stage=exc.stage,
                        exception_type=exc.exception_type,
                        metadata=exc.metadata,
                    )
                    failed = repository.get_job(job.id)
                    assert failed is not None
                    finalize_quota_transition_safely(
                        repository,  # type: ignore[arg-type]
                        failed,
                    )
                finally:
                    cleanup_local_media(
                        repository,  # type: ignore[arg-type]
                        repository.get_job(job.id),
                        str(media_path),
                    )
            cleanup = not media_path.exists()

        final = repository.get_job(job.id)
        assert final is not None
        events = [
            str(event["event_type"])
            for event in repository.events
            if event["media_job_id"] == job.id
        ]
        call_purposes = [
            str(event["metadata"].get("call_purpose"))
            for event in repository.events
            if event["media_job_id"] == job.id
            and event["event_type"] == "gemini_api_call_started"
            and isinstance(event["metadata"], dict)
        ]
        call_numbers = [
            int(event["metadata"].get("call_number") or 0)
            for event in repository.events
            if event["media_job_id"] == job.id
            and event["event_type"] == "gemini_api_call_started"
            and isinstance(event["metadata"], dict)
        ]
        request_hash = hashlib.sha256(
            json.dumps(
                client.request_shapes,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        analysis_events = [
            event
            for event in events
            if event.startswith("gemini_")
            or event.startswith("recipe_card_repair_")
        ]
        primary_preflight = next(
            (
                dict(event["metadata"])
                for event in repository.events
                if event["media_job_id"] == job.id
                and event["event_type"] == "gemini_primary_preflight_failed"
                and isinstance(event["metadata"], dict)
            ),
            {},
        )
        sdk_failures = [
            dict(event["metadata"])
            for event in repository.events
            if event["media_job_id"] == job.id
            and event["event_type"] == "gemini_sdk_invocation_failed"
            and isinstance(event["metadata"], dict)
        ]
        return {
            "terminal_status": final.status,
            "call_purposes": call_purposes,
            "call_numbers": call_numbers,
            "request_hash": request_hash,
            "request_shapes": client.request_shapes,
            "analysis_events": analysis_events,
            "primary_preflight": primary_preflight,
            "sdk_failures": sdk_failures,
            "event_payload": json.dumps(repository.events, default=str),
            "quota_success": events.count("ai_quota_success_committed"),
            "cleanup": cleanup,
        }

    def _invalid_readiness(self) -> dict[str, object]:
        return {
            "ready": False,
            "gemini_config_ready": False,
            "gemini_request_shape_ready": False,
            "local_request_shape_ready": False,
            "gemini_live_canary_verified": "unknown",
            "google_genai_version": "2.13.0",
            "pydantic_version": "2.13.4",
            "pydantic_core_version": "2.46.4",
            "primary_ready": False,
            "fallback_ready": True,
            "primary_configuration_category": "response_schema_thinking_level",
            "fallback_configuration_category": (
                "response_schema_model_default_thinking"
            ),
            "primary_schema_fingerprint": "0" * 64,
            "fallback_schema_fingerprint": "1" * 64,
            "schema_mutation_detected": False,
            "normalized_error_locations": [
                "thinking_config.thinking_level"
            ],
            "normalized_error_types": ["literal_error"],
        }


if __name__ == "__main__":
    unittest.main()
