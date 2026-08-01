from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID

from app import queue_worker
from app.config import Settings
from app.queue_worker import (
    GeminiProviderCallBudget,
    QueueJobFailure,
    _GEMINI_CALL_CONTEXT,
    guarded_generate_content,
)
from app.schemas import MediaJobRecord


class QueueGeminiGuardTests(unittest.TestCase):
    def settings(self, **overrides: object) -> Settings:
        values: dict[str, object] = {
            "_env_file": None,
            "WORKER_API_SECRET": "test",
            "supabase_url": "https://example.invalid",
            "supabase_service_role_key": "test",
            "app_env": "production",
            "media_resolver_mode": "legacy",
            "external_media_resolver_audience": "disabled",
            "instagram_resolver_chain": [],
            "tiktok_resolver_chain": [],
        }
        values.update(overrides)
        return Settings(**values)

    def job(self) -> MediaJobRecord:
        return MediaJobRecord(
            id=UUID("00000000-0000-0000-0000-000000000111"),
            user_id=UUID("00000000-0000-0000-0000-000000000222"),
            recipe_item_id=None,
            source_url="https://source.invalid/video",
            platform="youtube",
            status="analyzing",
            progress=60,
        )

    def test_every_active_call_is_reserved_and_limit_is_enforced(self) -> None:
        settings = self.settings(gemini_max_calls_per_job=2)
        job = self.job()
        repository = MagicMock()
        client = MagicMock()
        client.models.generate_content.return_value = object()
        budget = GeminiProviderCallBudget(max_calls=2)
        token = _GEMINI_CALL_CONTEXT.set((repository, job, settings))
        try:
            guarded_generate_content(
                client,
                model="fixture-model",
                contents=["x"],
                stage="gemini_structured_extraction",
                call_budget=budget,
            )
            guarded_generate_content(
                client,
                model="fixture-model",
                contents=["x"],
                stage="gemini_structured_extraction",
                call_budget=budget,
            )
            with self.assertRaises(QueueJobFailure) as captured:
                guarded_generate_content(
                    client,
                    model="fixture-model",
                    contents=["x"],
                    stage="gemini_structured_extraction",
                    call_budget=budget,
                )
        finally:
            _GEMINI_CALL_CONTEXT.reset(token)
        self.assertEqual(captured.exception.code, "gemini_call_budget_exhausted")
        self.assertEqual(budget.attempts, 2)
        self.assertEqual(client.models.generate_content.call_count, 2)
        repository.count_job_gemini_calls.assert_not_called()
        repository.count_daily_gemini_calls.assert_not_called()
        started = [
            call
            for call in repository.insert_event.call_args_list
            if call.kwargs.get("event_type") == "gemini_api_call_started"
        ]
        self.assertEqual(len(started), 2)

    def test_failure_event_name_and_control_flow_gain_only_safe_provider_detail(self) -> None:
        from google.genai.errors import ServerError

        settings = self.settings()
        job = self.job()
        repository = MagicMock()
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

    def test_started_and_completed_event_failures_do_not_block_provider(self) -> None:
        for failing_event in (
            "gemini_api_call_started",
            "gemini_api_call_completed",
        ):
            with self.subTest(failing_event=failing_event):
                repository = MagicMock()

                def insert_event(*_args: object, **kwargs: object) -> None:
                    if kwargs.get("event_type") == failing_event:
                        raise RuntimeError("telemetry fixture failure")

                repository.insert_event.side_effect = insert_event
                client = MagicMock()
                response = object()
                client.models.generate_content.return_value = response
                token = _GEMINI_CALL_CONTEXT.set(
                    (repository, self.job(), self.settings())
                )
                try:
                    actual = guarded_generate_content(
                        client,
                        model="gemini-3.1-flash-lite",
                        contents=["fixture"],
                        stage="gemini_structured_extraction",
                    )
                finally:
                    _GEMINI_CALL_CONTEXT.reset(token)
                self.assertIs(actual, response)
                client.models.generate_content.assert_called_once()

    def test_failed_event_failure_preserves_original_provider_exception(self) -> None:
        provider_error = RuntimeError("provider fixture failure")
        repository = MagicMock()

        def insert_event(*_args: object, **kwargs: object) -> None:
            if kwargs.get("event_type") == "gemini_api_call_failed":
                raise RuntimeError("telemetry fixture failure")

        repository.insert_event.side_effect = insert_event
        client = MagicMock()
        client.models.generate_content.side_effect = provider_error
        token = _GEMINI_CALL_CONTEXT.set(
            (repository, self.job(), self.settings())
        )
        try:
            with self.assertRaises(RuntimeError) as captured:
                guarded_generate_content(
                    client,
                    model="gemini-3.1-flash-lite",
                    contents=["fixture"],
                    stage="gemini_structured_extraction",
                )
        finally:
            _GEMINI_CALL_CONTEXT.reset(token)
        self.assertIs(captured.exception, provider_error)
        client.models.generate_content.assert_called_once()

    def test_missing_or_broken_context_never_blocks_provider(self) -> None:
        for broken_lookup in (False, True):
            with self.subTest(broken_lookup=broken_lookup):
                client = MagicMock()
                response = object()
                client.models.generate_content.return_value = response
                if broken_lookup:
                    context = MagicMock()
                    context.get.side_effect = RuntimeError("context fixture failure")
                    patcher = patch.object(
                        queue_worker,
                        "_GEMINI_CALL_CONTEXT",
                        context,
                    )
                else:
                    token = _GEMINI_CALL_CONTEXT.set(None)
                    patcher = None
                try:
                    if patcher is not None:
                        patcher.start()
                    actual = guarded_generate_content(
                        client,
                        model="gemini-3.1-flash-lite",
                        contents=["fixture"],
                        stage="gemini_structured_extraction",
                    )
                finally:
                    if patcher is not None:
                        patcher.stop()
                    else:
                        _GEMINI_CALL_CONTEXT.reset(token)
                self.assertIs(actual, response)
                client.models.generate_content.assert_called_once()

    def test_broken_context_metadata_and_warning_logger_never_block_provider(
        self,
    ) -> None:
        class BrokenJob:
            @property
            def id(self):
                raise RuntimeError("context metadata fixture failure")

        client = MagicMock()
        response = object()
        client.models.generate_content.return_value = response
        token = _GEMINI_CALL_CONTEXT.set(
            (MagicMock(), BrokenJob(), self.settings())  # type: ignore[arg-type]
        )
        try:
            with patch.object(
                queue_worker.logger,
                "warning",
                side_effect=RuntimeError("logger fixture failure"),
            ):
                actual = guarded_generate_content(
                    client,
                    model="gemini-3.1-flash-lite",
                    contents=["fixture"],
                    stage="gemini_structured_extraction",
                )
        finally:
            _GEMINI_CALL_CONTEXT.reset(token)
        self.assertIs(actual, response)
        client.models.generate_content.assert_called_once()


if __name__ == "__main__":
    unittest.main()
