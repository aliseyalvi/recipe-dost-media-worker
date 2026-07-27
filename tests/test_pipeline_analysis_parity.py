from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    QueueJobFailure,
    _GEMINI_CALL_CONTEXT,
    finalize_quota_transition_safely,
    guarded_generate_content,
    make_gemini_runtime_context,
    record_queue_failure,
    run_gemini_video_poc_analysis,
)
from app.quota_finalization import ensure_quota_reservation_event


class RecordingGeminiClient(FixtureGeminiClient):
    def __init__(self, responses: list[object], media_path: Path) -> None:
        super().__init__(responses)
        self.media_path = media_path
        self.request_shapes: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> object:
        if not self.media_path.is_file():
            raise FileNotFoundError("fixture media was removed before analysis")
        config = kwargs.get("config")
        config_dict = config if isinstance(config, dict) else {}
        contents = kwargs.get("contents")
        content_values = contents if isinstance(contents, list) else []
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
                    "text" if isinstance(value, str) else "file"
                    for value in content_values
                ],
            }
        )
        return super().generate_content(**kwargs)


class PipelineAnalysisParityTests(unittest.TestCase):
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
        self.assertEqual(
            result["request_shapes"][0]["model"],  # type: ignore[index]
            "gemini-3.1-flash-lite",
        )
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
                    "primary_capability_preflight": ["model_fallback"],
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
            "response_json_schema",
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
            client = RecordingGeminiClient(responses, media_path)
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
        return {
            "terminal_status": final.status,
            "call_purposes": call_purposes,
            "call_numbers": call_numbers,
            "request_hash": request_hash,
            "request_shapes": client.request_shapes,
            "analysis_events": analysis_events,
            "primary_preflight": primary_preflight,
            "quota_success": events.count("ai_quota_success_committed"),
            "cleanup": cleanup,
        }


if __name__ == "__main__":
    unittest.main()
