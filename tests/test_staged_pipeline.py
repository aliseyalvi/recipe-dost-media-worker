from __future__ import annotations

import asyncio
import threading
import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.pipeline.contracts import (
    DispatchEnvelope,
    GeminiAnalysisResult,
    GeminiAnalysisInput,
    IngestedMedia,
    SourceContext,
    StageTimestamps,
)
from app.qa.replay import OfflineRepository, canonical_recipe, make_job
from app.qa.staged import run_staged_batch
from app.pipeline.services import BackpressureDebouncer, analyzer_loop
from app.pipeline.telemetry import PipelineTransitionRecorder
from app.pipeline.transport import InProcessStageTransport
from app.queue_worker import (
    DeferredGeminiRecipe,
    delete_temp_media,
    public_source_metadata,
    run_gemini_video_poc_analysis,
    upload_gemini_video_file,
    validate_and_normalize_canonical_recipe,
)
from app.workers._standalone import standalone_main


CASES = [
    "tiktok_success_1",
    "tiktok_success_2",
    "instagram_progressive_success",
    "instagram_split_av_success",
    "deliberate_gemini_parse_failure",
    "provider_failure",
    "private_metadata_failure",
]


class StagedPipelineTests(unittest.TestCase):
    def test_safe_pipeline_defaults_and_standalone_refusal(self) -> None:
        settings = Settings(
            _env_file=None,
            WORKER_API_SECRET="fixture",
            supabase_url="https://example.invalid",
            supabase_service_role_key="fixture",
        )
        self.assertEqual(settings.worker_pipeline_mode, "sequential")
        self.assertEqual(settings.worker_dispatch_concurrency, 1)
        self.assertEqual(settings.worker_ingest_concurrency, 1)
        self.assertEqual(settings.worker_gemini_analysis_concurrency, 1)
        self.assertEqual(settings.worker_finalize_concurrency, 1)
        self.assertEqual(settings.worker_ffmpeg_concurrency, 1)
        self.assertEqual(settings.worker_gemini_upload_concurrency, 1)
        self.assertFalse(settings.worker_delete_temp_after_gemini_active)
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit):
                standalone_main("ingest")

    def test_dispatch_contract_contains_no_runtime_job_or_source_url(self) -> None:
        job = make_job(1, "tiktok")
        envelope = DispatchEnvelope(
            job_id=job.id,
            platform=job.platform,
        )
        serialized = envelope.model_dump(mode="json")
        self.assertNotIn("runtime_job", serialized)
        self.assertNotIn("source_url", str(serialized))
        media = IngestedMedia(
            dispatch=envelope,
            normalized_platform="tiktok",
            resolver_category="fixture",
            media_mode="progressive",
            output_mode="temp_file",
            size_category="under_64mb",
            duration_category="under_31s",
            dimension_category="541_720",
            source_context=SourceContext(platform="tiktok"),
            timestamps=StageTimestamps(),
            local_media_path="/private/runtime-only",
        )
        stage_data = GeminiAnalysisInput(media=media).model_dump(mode="json")
        serialized_text = str(stage_data)
        for forbidden in (
            "runtime_client",
            "runtime_extract_result",
            "schema",
            "repository",
            "call_budget",
            "uploaded_file",
            "/private/runtime-only",
        ):
            self.assertNotIn(forbidden, serialized_text)

    def test_staged_pipeline_and_concurrency_two_are_deterministic(self) -> None:
        report = run_staged_batch(CASES)
        self.assertEqual(report["fifo_claim_order"], CASES)
        self.assertEqual(report["fifo_enqueue_order"], CASES)
        self.assertEqual(report["maximum_active_stages"]["ingest"], 1)
        self.assertEqual(report["maximum_active_stages"]["analysis"], 1)
        self.assertEqual(report["maximum_active_stages"]["finalize"], 1)
        self.assertTrue(report["stage_overlap_observed"])
        self.assertEqual(report["terminal_count"], len(CASES))
        self.assertEqual(report["active_reservation_count"], 0)
        self.assertTrue(report["cleanup_success"])
        self.assertTrue(report["graceful_shutdown"])
        self.assertTrue(report["restart_rematerialization_proved"])
        self.assertFalse(report["durable_lease_recovery_supported"])
        self.assertTrue(report["exactly_one_terminal_state"])
        self.assertTrue(all(report["stage_failure_probes"].values()))
        probe = report["concurrency_two_probe"]
        self.assertEqual(probe["maximum_active_stages"]["analysis"], 2)
        self.assertEqual(probe["maximum_active_stages"]["ingest"], 1)
        self.assertEqual(probe["terminal_count"], len(CASES))
        self.assertEqual(probe["active_reservation_count"], 0)
        self.assertTrue(probe["provider_budget_isolated_per_job"])
        self.assertTrue(probe["failure_isolation"])

    def test_cleanup_does_not_remove_unrecognized_parent_or_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "ordinary"
            parent.mkdir()
            media = parent / "media.mp4"
            sibling = parent / "keep.txt"
            media.write_bytes(b"media")
            sibling.write_text("keep", encoding="utf-8")
            self.assertTrue(delete_temp_media(str(media)))
            self.assertFalse(media.exists())
            self.assertFalse(delete_temp_media(str(media)))
            self.assertTrue(sibling.exists())

    def test_upload_cannot_read_a_deleted_file(self) -> None:
        class Files:
            def upload(self, *, file: str, config: object) -> object:
                del config
                if not Path(file).is_file():
                    raise FileNotFoundError("fixture upload source missing")
                return object()

        with tempfile.TemporaryDirectory() as root:
            media = Path(root) / "video.mp4"
            media.write_bytes(b"fixture")
            media.unlink()
            client = MagicMock()
            client.files = Files()
            with self.assertRaises(FileNotFoundError):
                upload_gemini_video_file(
                    client=client,
                    temp_file_path=str(media),
                    mime_type="video/mp4",
                )

    def test_pipeline_transition_sequence_precedes_consumer_start(self) -> None:
        async def scenario() -> list[dict[str, object]]:
            settings = Settings(
                _env_file=None,
                WORKER_API_SECRET="fixture",
                supabase_url="https://example.invalid",
                supabase_service_role_key="fixture",
            )
            repository = OfflineRepository()
            job = make_job(70, "tiktok")
            repository.register(job)
            recorder = PipelineTransitionRecorder()
            transport = InProcessStageTransport(
                ingest_size=1,
                analysis_size=1,
                finalize_size=1,
            )
            envelope = DispatchEnvelope(job_id=job.id, platform="tiktok")

            async def consume() -> None:
                consumed = await transport.dequeue_ingest()
                await recorder.emit(
                    repository,
                    consumed.job_id,
                    "worker_stage_started",
                    stage="ingest",
                    settings=settings,
                )
                transport.task_done("ingest")

            consumer = asyncio.create_task(consume())
            await recorder.emit(
                repository,
                job.id,
                "worker_pipeline_started",
                stage="dispatch",
                settings=settings,
            )
            await recorder.enqueue(
                repository,
                job.id,
                stage="ingest",
                settings=settings,
                queue_depth=0,
                configured_concurrency=1,
                enqueue=transport.enqueue_ingest(envelope),
            )
            await consumer
            return repository.events

        events = asyncio.run(scenario())
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "worker_pipeline_started",
                "worker_stage_enqueue_requested",
                "worker_stage_enqueued",
                "worker_stage_started",
            ],
        )
        self.assertEqual(
            [event["metadata"]["stage_sequence"] for event in events],
            [1, 2, 3, 4],
        )

    def test_two_analyzer_slots_report_shared_active_count(self) -> None:
        async def scenario() -> list[dict[str, object]]:
            settings = Settings(
                _env_file=None,
                WORKER_API_SECRET="fixture",
                supabase_url="https://example.invalid",
                supabase_service_role_key="fixture",
                worker_gemini_analysis_concurrency=2,
            )
            repository = OfflineRepository()
            transport = InProcessStageTransport(
                ingest_size=1,
                analysis_size=2,
                finalize_size=2,
            )
            recorder = PipelineTransitionRecorder()
            active_counts = {"ingest": 0, "analysis": 0, "finalize": 0}
            barrier = threading.Barrier(2)
            for index in (71, 72):
                job = make_job(index, "tiktok")
                repository.register(job)
                media = IngestedMedia(
                    dispatch=DispatchEnvelope(
                        job_id=job.id,
                        platform="tiktok",
                    ),
                    normalized_platform="tiktok",
                    resolver_category="fixture",
                    media_mode="progressive",
                    output_mode="temp_file",
                    size_category="under_64mb",
                    duration_category="under_31s",
                    dimension_category="541_720",
                    source_context=SourceContext(platform="tiktok"),
                    timestamps=StageTimestamps(),
                )
                await transport.enqueue_analysis(GeminiAnalysisInput(media=media))

            def analyze(
                _repository: object,
                _settings: Settings,
                media: IngestedMedia,
            ) -> GeminiAnalysisResult:
                barrier.wait(timeout=2)
                return GeminiAnalysisResult(
                    dispatch=media.dispatch,
                    normalized_platform="tiktok",
                    resolver_category="fixture",
                    source_context=media.source_context,
                    timestamps=StageTimestamps(),
                )

            with patch(
                "app.pipeline.services.create_repository",
                return_value=repository,
            ), patch(
                "app.pipeline.services.analyze_media",
                side_effect=analyze,
            ):
                workers = [
                    asyncio.create_task(
                        analyzer_loop(
                            settings=settings,
                            transport=transport,
                            active_counts=active_counts,
                            transitions=recorder,
                            worker_slot=slot,
                        )
                    )
                    for slot in (1, 2)
                ]
                await asyncio.wait_for(transport.join_stage("analysis"), timeout=3)
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
            return repository.events

        events = asyncio.run(scenario())
        started = [
            event["metadata"]
            for event in events
            if event["event_type"] == "worker_stage_started"
        ]
        self.assertEqual({item["worker_slot"] for item in started}, {1, 2})
        self.assertEqual(
            {item["configured_concurrency"] for item in started},
            {2},
        )
        self.assertIn(2, {item["active_stage_count"] for item in started})

    def test_backpressure_is_debounced_and_cleared(self) -> None:
        debouncer = BackpressureDebouncer(minimum_interval_seconds=60)
        with patch("app.pipeline.services.logger.info") as logged:
            debouncer.blocked(reason="ingest_capacity_active", queue_depth=1)
            debouncer.blocked(reason="ingest_capacity_active", queue_depth=1)
            debouncer.blocked(reason="ingest_queue_full", queue_depth=1)
            debouncer.cleared(queue_depth=0)
        self.assertEqual(
            [call.args[0] for call in logged.call_args_list],
            [
                "worker_stage_backpressure",
                "worker_stage_backpressure",
                "worker_stage_backpressure_cleared",
            ],
        )
    def test_thumbnail_and_cdn_metadata_definitions(self) -> None:
        metadata = public_source_metadata(
            {
                "platform": "instagram",
                "thumbnail_url": None,
                "thumbnail_selected": True,
                "cdn_download_url": "https://provider.invalid/ephemeral",
            },
            "https://storage.invalid/thumbnail",
        )
        self.assertTrue(metadata["thumbnail_source_present"])
        self.assertTrue(metadata["media"]["has_cdn_url"])
        self.assertFalse(metadata["media"]["cdn_url_stored_private"])

    def test_local_media_is_retained_after_active_by_default(self) -> None:
        settings = Settings(
            _env_file=None,
            WORKER_API_SECRET="fixture",
            supabase_url="https://example.invalid",
            supabase_service_role_key="fixture",
            gemini_api_key="fixture",
        )
        job = make_job(40, "tiktok")
        repository = OfflineRepository()
        repository.register(job)
        recipe, errors = validate_and_normalize_canonical_recipe(
            canonical_recipe(),
            {"platform": "tiktok", "title": "Fixture"},
            settings,
        )
        self.assertFalse(errors)
        with tempfile.TemporaryDirectory() as root:
            job_directory = Path(root) / str(job.id)
            job_directory.mkdir()
            media = job_directory / "video.mp4"
            media.write_bytes(b"fixture-media")
            client = MagicMock()
            uploaded = MagicMock()
            uploaded.name = "files/fixture"
            with patch(
                "app.queue_worker.create_gemini_client",
                return_value=client,
            ), patch(
                "app.queue_worker.upload_gemini_video_file",
                return_value=uploaded,
            ), patch(
                "app.queue_worker.wait_for_gemini_file_active",
                return_value=uploaded,
            ), patch(
                "app.queue_worker.run_single_pass_structured_extraction",
                return_value=(recipe, settings.gemini_primary_model, False),
            ):
                deferred = run_gemini_video_poc_analysis(
                    repository=repository,  # type: ignore[arg-type]
                    job=job,
                    settings=settings,
                    extract_result={
                        "video_bytes_verified": True,
                        "temp_file_path": str(media),
                        "size_bytes": media.stat().st_size,
                        "duration_seconds": 30,
                        "resolver": "fixture",
                    },
                    platform="tiktok",
                    uploaded_thumbnail_url=None,
                    defer_finalization=True,
                )
            self.assertIsInstance(deferred, DeferredGeminiRecipe)
            self.assertTrue(media.exists())
            cleanup_events = [
                event
                for event in repository.events
                if event["event_type"] == "temp_video_deleted"
            ]
            self.assertEqual(cleanup_events, [])
            self.assertTrue(delete_temp_media(str(media)))
            self.assertFalse(media.exists())


if __name__ == "__main__":
    unittest.main()
