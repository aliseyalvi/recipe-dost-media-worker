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


if __name__ == "__main__":
    unittest.main()
