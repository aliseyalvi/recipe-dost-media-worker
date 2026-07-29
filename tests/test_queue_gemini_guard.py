from __future__ import annotations

import unittest
from unittest.mock import MagicMock
from uuid import UUID

from app.config import Settings
from app.queue_worker import (
    QueueJobFailure,
    _GEMINI_CALL_CONTEXT,
    guarded_generate_content,
)
from app.schemas import MediaJobRecord


class QueueGeminiGuardTests(unittest.TestCase):
    def test_every_active_call_is_reserved_and_limit_is_enforced(self) -> None:
        settings = Settings(
            _env_file=None,
            WORKER_API_SECRET="test",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="test",
            app_env="production",
            media_resolver_mode="legacy",
            external_media_resolver_audience="disabled",
            instagram_resolver_chain=[],
            tiktok_resolver_chain=[],
            gemini_max_calls_per_job=2,
        )
        job = MediaJobRecord(
            id=UUID("00000000-0000-0000-0000-000000000111"),
            user_id=UUID("00000000-0000-0000-0000-000000000222"),
            recipe_item_id=None,
            source_url="https://source.invalid/video",
            platform="youtube",
            status="analyzing",
            progress=60,
        )
        repository = MagicMock()
        repository.count_job_gemini_calls.side_effect = [0, 1, 2]
        repository.count_daily_gemini_calls.return_value = 0
        client = MagicMock()
        client.models.generate_content.return_value = object()
        token = _GEMINI_CALL_CONTEXT.set((repository, job, settings))
        try:
            guarded_generate_content(client, model="fixture-model", contents=["x"], stage="gemini_structured_extraction")
            guarded_generate_content(client, model="fixture-model", contents=["x"], stage="gemini_structured_extraction")
            with self.assertRaises(QueueJobFailure) as captured:
                guarded_generate_content(client, model="fixture-model", contents=["x"], stage="gemini_structured_extraction")
        finally:
            _GEMINI_CALL_CONTEXT.reset(token)
        self.assertEqual(captured.exception.code, "gemini_call_budget_exhausted")
        self.assertEqual(client.models.generate_content.call_count, 2)
        started = [
            call
            for call in repository.insert_event.call_args_list
            if call.kwargs.get("event_type") == "gemini_api_call_started"
        ]
        self.assertEqual(len(started), 2)

    def test_failure_event_name_and_control_flow_gain_only_safe_provider_detail(self) -> None:
        from google.genai.errors import ServerError

        settings = Settings(
            _env_file=None,
            WORKER_API_SECRET="test",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="test",
            app_env="production",
            media_resolver_mode="legacy",
            external_media_resolver_audience="disabled",
            instagram_resolver_chain=[],
            tiktok_resolver_chain=[],
        )
        job = MediaJobRecord(
            id=UUID("00000000-0000-0000-0000-000000000111"),
            user_id=UUID("00000000-0000-0000-0000-000000000222"),
            recipe_item_id=None,
            source_url="https://source.invalid/video",
            platform="youtube",
            status="analyzing",
            progress=60,
        )
        repository = MagicMock()
        repository.count_job_gemini_calls.return_value = 0
        repository.count_daily_gemini_calls.return_value = 0
        client = MagicMock()
        provider_error = ServerError(
            503,
            {
                "error": {
                    "status": "UNAVAILABLE",
                    "message": "sensitive provider response",
                }
            },
        )
        client.models.generate_content.side_effect = provider_error

        token = _GEMINI_CALL_CONTEXT.set((repository, job, settings))
        try:
            with self.assertRaises(ServerError) as captured:
                guarded_generate_content(
                    client,
                    model="gemini-3.1-flash-lite",
                    contents=["sensitive prompt"],
                    stage="gemini_structured_extraction",
                )
        finally:
            _GEMINI_CALL_CONTEXT.reset(token)

        self.assertIs(captured.exception, provider_error)
        failed = [
            call
            for call in repository.insert_event.call_args_list
            if call.kwargs.get("event_type") == "gemini_api_call_failed"
        ]
        self.assertEqual(len(failed), 1)
        metadata = failed[0].kwargs["metadata"]
        self.assertEqual(metadata["failure_category"], "gemini_server_error")
        self.assertEqual(metadata["provider_error_category"], "provider_unavailable")
        self.assertEqual(metadata["provider_http_status"], 503)
        self.assertEqual(metadata["provider_status_name"], "UNAVAILABLE")
        self.assertNotIn("sensitive prompt", str(metadata))
        self.assertNotIn("sensitive provider response", str(metadata))


if __name__ == "__main__":
    unittest.main()
