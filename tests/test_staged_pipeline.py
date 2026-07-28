from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.pipeline.contracts import DispatchEnvelope
from app.qa.replay import OfflineRepository, canonical_recipe, make_job
from app.qa.staged import run_staged_batch
from app.queue_worker import (
    DeferredGeminiRecipe,
    delete_temp_media,
    public_source_metadata,
    run_gemini_video_poc_analysis,
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
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit):
                standalone_main("ingest")

    def test_runtime_job_snapshot_is_not_serialized(self) -> None:
        job = make_job(1, "tiktok")
        envelope = DispatchEnvelope(
            job_id=job.id,
            platform=job.platform,
            runtime_job=job,
        )
        serialized = envelope.model_dump(mode="json")
        self.assertNotIn("runtime_job", serialized)
        self.assertNotIn("source_url", str(serialized))

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
            self.assertTrue(sibling.exists())

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

    def test_local_media_is_deleted_only_after_active(self) -> None:
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
        self.assertFalse(media.exists())
        cleanup_events = [
            event
            for event in repository.events
            if event["event_type"] == "temp_video_deleted"
        ]
        self.assertEqual(len(cleanup_events), 1)
        self.assertEqual(
            cleanup_events[0]["metadata"]["cleanup_point"],
            "after_gemini_file_active",
        )


if __name__ == "__main__":
    unittest.main()
