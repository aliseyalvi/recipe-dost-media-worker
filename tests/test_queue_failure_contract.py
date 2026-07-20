from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID

from app.config import Settings
from app.queue_worker import process_queue_job, record_queue_failure
from app.schemas import MediaJobRecord
from app.social.stream_download import TERMINAL_SIZE_MESSAGE
from app.social import stream_download


JOB_ID = UUID("00000000-0000-0000-0000-000000000111")
USER_ID = UUID("00000000-0000-0000-0000-000000000222")


def settings() -> Settings:
    return Settings(
        _env_file=None,
        WORKER_API_SECRET="test",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="test",
        ai_analysis_mode="gemini_video_poc",
        enable_unified_ytdlp_stream_extractor=True,
        preferred_video_size_mb=36,
        max_video_size_mb=50,
        gemini_file_max_size_bytes=2_000_000_000,
    )


def job() -> MediaJobRecord:
    return MediaJobRecord(
        id=JOB_ID,
        user_id=USER_ID,
        recipe_item_id=None,
        source_url="https://source.invalid/not-a-real-incident",
        platform="instagram",
        status="queued",
        progress=0,
    )


def repository_for(active_job: MediaJobRecord) -> MagicMock:
    repository = MagicMock()
    state = active_job

    def update_job(_job_id: UUID, **values: object) -> MediaJobRecord:
        nonlocal state
        state = state.model_copy(update=values)
        return state

    repository.update_job.side_effect = update_job
    return repository


class FrontendFailureContractTests(unittest.TestCase):
    def test_media_size_code_and_safe_message_use_existing_fields(self) -> None:
        repository = repository_for(job())
        record_queue_failure(
            repository,
            JOB_ID,
            "media_size_exceeded",
            TERMINAL_SIZE_MESSAGE,
            stage="unified_extractor",
            metadata={
                "duration_bucket": "60_90_seconds",
                "size_bucket": "50_mib_or_more",
                "adaptive_attempt_count": 3,
            },
        )
        failed_call = repository.update_job.call_args
        self.assertEqual(failed_call.kwargs["status"], "failed")
        self.assertEqual(failed_call.kwargs["progress"], 100)
        self.assertEqual(failed_call.kwargs["error_code"], "media_size_exceeded")
        self.assertEqual(failed_call.kwargs["error_message"], TERMINAL_SIZE_MESSAGE)
        repository.add_event.assert_called_once()

    def test_incident_terminal_failure_persists_once_and_captures_one_warning(self) -> None:
        repository = repository_for(job())
        active = settings()
        incident_info = {
            "duration": 63,
            "formats": [
                {
                    "format_id": bucket,
                    "ext": "mp4",
                    "width": height,
                    "height": int(height * 16 / 9),
                    "vcodec": "h264",
                    "acodec": "aac",
                    "filesize": size_mib * 1024 * 1024,
                }
                for bucket, height, size_mib in (
                    ("720", 720, 60),
                    ("540", 540, 55),
                    ("480", 480, 51),
                )
            ],
        }
        extractor_result = stream_download.no_candidate_result(
            "instagram",
            incident_info,
            resolver="ytdlp_stream_cookies",
            cookie_mode="platform_cookie_file",
            output_mode="temp_file",
            settings=active,
        )
        extractor_result["cookie_retry_attempted"] = True
        with patch("app.queue_worker.extract_social_video", return_value=extractor_result), patch(
            "app.queue_worker.report_terminal_worker_outcome", return_value="e" * 32
        ) as warning, patch("app.queue_worker.report_worker_error") as technical:
            process_queue_job(repository, active, job())

        warning.assert_called_once_with(
            error_code="media_size_exceeded",
            source_platform="instagram",
            duration_bucket="60_90_seconds",
            size_bucket="50_mib_or_more",
            selected_resolution_bucket="480p",
            adaptive_attempt_count=0,
            cookie_retry_used=True,
        )
        technical.assert_not_called()
        failed_updates = [
            call
            for call in repository.update_job.call_args_list
            if call.kwargs.get("status") == "failed"
        ]
        self.assertEqual(len(failed_updates), 1)
        self.assertEqual(failed_updates[0].kwargs["error_code"], "media_size_exceeded")
        self.assertEqual(failed_updates[0].kwargs["error_message"], TERMINAL_SIZE_MESSAGE)
        repository.insert_recipe_card.assert_not_called()

    def test_unexpected_extractor_exception_is_reported_once_as_error(self) -> None:
        repository = repository_for(job())
        technical_cause = RuntimeError("unsafe provider detail https://secret.invalid")
        extractor_result = {
            "success": False,
            "platform": "instagram",
            "resolver": "ytdlp_stream_cookies",
            "error_code": "unified_extractor_failed",
            "safe_error_message": "This social video could not be downloaded.",
            "metadata_fallback_attempted": False,
            "cookie_retry_attempted": True,
            "adaptive_attempt_count": 3,
            "duration_bucket": "60_90_seconds",
            "actual_size_bucket": "unknown",
            "selected_resolution_bucket": "480p",
            "_technical_exception": technical_cause,
        }
        with patch("app.queue_worker.extract_social_video", return_value=extractor_result), patch(
            "app.queue_worker.report_terminal_worker_outcome"
        ) as warning, patch("app.queue_worker.report_worker_error") as report:
            process_queue_job(repository, settings(), job())
        warning.assert_not_called()
        report.assert_called_once_with(
            technical_cause,
            feature="metadata",
            operation="extract_metadata",
            stage="unified_extractor",
            error_code="unified_extractor_failed",
            source_platform="instagram",
            retry_exhausted=True,
            recoverable=False,
            terminal=True,
        )


if __name__ == "__main__":
    unittest.main()
