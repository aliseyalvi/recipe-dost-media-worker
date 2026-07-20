from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from app.observability import sentry
from app.observability import sentry_test
from app import queue_worker


DSN = "https://public@example.ingest.sentry.io/123"


def render_env(app_env: str, branch: str, *, dev_test: str = "false") -> dict[str, str]:
    return {
        "APP_ENV": app_env,
        "SENTRY_DSN": DSN,
        "SENTRY_DEV_TEST": dev_test,
        "RENDER": "true",
        "RENDER_GIT_BRANCH": branch,
        "RENDER_GIT_COMMIT": "abcdef1234567890",
    }


def active_event() -> dict[str, object]:
    return {
        "event_id": "a" * 32,
        "environment": "production",
        "release": "recipe-dost-media-worker@abcdef1234567890",
        "tags": {
            "service": "recipe-dost-media-worker",
            "flow": "current",
            "feature": "ai_generation",
            "operation": "generate_recipe",
            "stage": "gemini_video_analysis",
            "error_code": "gemini_video_analysis_failed",
            "source_platform": "tiktok",
        },
        "contexts": {
            "worker": {
                "retry_attempt": 2,
                "retry_exhausted": True,
                "recoverable": False,
                "terminal": True,
                "unsafe_payload": {"recipe": "secret recipe"},
            },
            "response": {"body": "AI response"},
        },
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": (
                        "https://video.example/source Bearer token-value "
                        "recipe ingredients and instructions"
                    ),
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": "/service/app/queue_worker.py",
                                "function": "process_queue_job",
                                "lineno": 540,
                                "vars": {"SUPABASE_SERVICE_ROLE_KEY": "service-role-secret"},
                                "context_line": "prompt = raw_ai_response",
                            }
                        ]
                    },
                }
            ]
        },
        "request": {
            "url": "https://source.example/private",
            "headers": {"Authorization": "Bearer request-token"},
            "data": {"payload": "request body"},
        },
        "user": {"email": "person@example.com"},
        "extra": {
            "api_key": "provider-key",
            "prompt": "AI prompt",
            "response": "AI response",
            "recipe": {"ingredients": ["secret"]},
            "payload": {"request": "request payload"},
        },
        "breadcrumbs": {
            "values": [
                {"category": "http", "data": {"url": "https://secret.example"}},
                {"category": "query", "message": "select secret"},
            ]
        },
    }


def media_size_warning_event() -> dict[str, object]:
    event = active_event()
    event.pop("exception")
    event["level"] = "warning"
    event["message"] = sentry.MEDIA_SIZE_WARNING_MESSAGE
    event["fingerprint"] = list(sentry.MEDIA_SIZE_FINGERPRINT)
    event["tags"] = {
        "service": sentry.SERVICE_NAME,
        "flow": "current",
        "feature": "metadata",
        "operation": "extract_metadata",
        "stage": "unified_extractor",
        "error_code": "media_size_exceeded",
        "source_platform": "instagram",
    }
    event["contexts"] = {
        "worker": {
            "duration_bucket": "60_90_seconds",
            "size_bucket": "40_50_mib",
            "selected_resolution_bucket": "720p",
            "adaptive_attempt_count": 3,
            "cookie_retry_used": True,
            "terminal": True,
            "source_url": "https://secret.invalid/video",
            "temporary_path": "/tmp/private-video.mp4",
            "user_id": "private-user",
            "recipe_id": "private-recipe",
        }
    }
    return event


class SentryPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        sentry._reset_state_for_tests()

    def tearDown(self) -> None:
        sentry._reset_state_for_tests()

    def test_production_main_with_dsn_enables_runtime(self) -> None:
        config = sentry.compute_sentry_config(environment=render_env("production", "main"))
        self.assertTrue(config.enabled)
        self.assertEqual(config.environment, "production")
        self.assertEqual(config.release, "recipe-dost-media-worker@abcdef1234567890")

    def test_production_develop_disables_runtime(self) -> None:
        config = sentry.compute_sentry_config(environment=render_env("production", "develop"))
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "render_branch_mismatch")

    def test_development_normally_disables_runtime(self) -> None:
        config = sentry.compute_sentry_config(environment=render_env("development", "develop"))
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "runtime_requires_production")

    def test_development_flag_and_develop_enable_controlled_test(self) -> None:
        config = sentry.compute_sentry_config(
            controlled_test=True,
            environment=render_env("development", "develop", dev_test="true"),
        )
        self.assertTrue(config.enabled)
        self.assertEqual(config.environment, "development-test")

    def test_development_main_disables_controlled_test(self) -> None:
        config = sentry.compute_sentry_config(
            controlled_test=True,
            environment=render_env("development", "main", dev_test="true"),
        )
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "render_branch_mismatch")

    def test_missing_dsn_fails_closed(self) -> None:
        values = render_env("production", "main")
        values.pop("SENTRY_DSN")
        self.assertFalse(sentry.compute_sentry_config(environment=values).enabled)

    def test_frontend_dsn_name_is_never_used_as_worker_dsn(self) -> None:
        values = render_env("production", "main")
        values.pop("SENTRY_DSN")
        values["EXPO_PUBLIC_SENTRY_DSN"] = DSN
        self.assertFalse(sentry.compute_sentry_config(environment=values).enabled)

    def test_non_render_runtime_fails_closed_even_with_production_and_main(self) -> None:
        values = render_env("production", "main")
        values.pop("RENDER")
        config = sentry.compute_sentry_config(environment=values)
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "render_service_required")

    def test_non_render_controlled_test_fails_closed(self) -> None:
        values = render_env("development", "develop", dev_test="true")
        values.pop("RENDER")
        config = sentry.compute_sentry_config(controlled_test=True, environment=values)
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "render_service_required")

    def test_render_missing_branch_fails_closed(self) -> None:
        values = render_env("production", "main")
        values["RENDER_GIT_BRANCH"] = ""
        config = sentry.compute_sentry_config(environment=values)
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "render_branch_mismatch")

    def test_initialization_is_idempotent_and_disables_sensitive_capture(self) -> None:
        with patch.dict(os.environ, render_env("production", "main"), clear=True), patch(
            "app.observability.sentry.sentry_sdk.init"
        ) as init:
            self.assertTrue(sentry.initialize_sentry())
            self.assertTrue(sentry.initialize_sentry())
        init.assert_called_once()
        options = init.call_args.kwargs
        self.assertFalse(options["include_local_variables"])
        self.assertFalse(options["include_source_context"])
        self.assertEqual(options["max_request_body_size"], "never")
        self.assertFalse(options["default_integrations"])
        self.assertFalse(options["auto_enabling_integrations"])
        self.assertFalse(options["enable_logs"])
        self.assertEqual(options["traces_sample_rate"], 0.0)
        self.assertEqual(options["profiles_sample_rate"], 0.0)


class SentrySanitizerTests(unittest.TestCase):
    def test_active_flow_event_is_retained(self) -> None:
        sanitized = sentry.before_send(active_event(), {})
        self.assertIsNotNone(sanitized)
        assert sanitized is not None
        self.assertEqual(sanitized["tags"]["flow"], "current")
        self.assertEqual(sanitized["tags"]["feature"], "ai_generation")
        self.assertIn("exception", sanitized)

    def test_legacy_or_untagged_event_is_dropped(self) -> None:
        event = active_event()
        event["tags"] = {"flow": "legacy"}
        self.assertIsNone(sentry.before_send(event, {}))
        self.assertIsNone(sentry.before_send({"exception": event["exception"]}, {}))

    def test_sensitive_urls_tokens_payloads_ai_and_recipe_content_are_removed(self) -> None:
        sanitized = sentry.before_send(active_event(), {})
        self.assertIsNotNone(sanitized)
        serialized = json.dumps(sanitized, default=str).lower()
        for forbidden in (
            "https://",
            "bearer",
            "token-value",
            "service-role-secret",
            "provider-key",
            "ai prompt",
            "ai response",
            "secret recipe",
            "ingredients and instructions",
            "request body",
            "request payload",
            "person@example.com",
            "authorization",
            "supabase_service_role_key",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn("request", sanitized)
        self.assertNotIn("user", sanitized)
        self.assertNotIn("extra", sanitized)
        self.assertNotIn("breadcrumbs", sanitized)
        frame = sanitized["exception"]["values"][0]["stacktrace"]["frames"][0]
        self.assertNotIn("vars", frame)
        self.assertNotIn("context_line", frame)

    def test_all_breadcrumbs_are_dropped(self) -> None:
        self.assertIsNone(
            sentry.before_breadcrumb(
                {"category": "http", "data": {"url": "https://example.com"}},
                {},
            )
        )

    def test_capture_message_warning_is_retained_without_exception(self) -> None:
        sanitized = sentry.before_send(media_size_warning_event(), {})
        self.assertIsNotNone(sanitized)
        assert sanitized is not None
        self.assertEqual(sanitized["level"], "warning")
        self.assertEqual(sanitized["message"], sentry.MEDIA_SIZE_WARNING_MESSAGE)
        self.assertEqual(sanitized["fingerprint"], sentry.MEDIA_SIZE_FINGERPRINT)
        self.assertNotIn("exception", sanitized)

    def test_warning_retains_optional_missing_context_and_drops_unsafe_context(self) -> None:
        event = media_size_warning_event()
        event["contexts"] = {"worker": {"terminal": True, "source_url": "https://x"}}
        tags = dict(event["tags"])
        tags.pop("source_platform")
        event["tags"] = tags
        sanitized = sentry.before_send(event, {})
        self.assertIsNotNone(sanitized)
        assert sanitized is not None
        serialized = json.dumps(sanitized)
        self.assertNotIn("source_url", serialized)
        self.assertNotIn("https://", serialized)


class SentryReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        sentry._reset_state_for_tests()

    def tearDown(self) -> None:
        sentry._reset_state_for_tests()

    def _report(self, error: Exception, **overrides: object) -> str | None:
        values: dict[str, object] = {
            "feature": "ai_generation",
            "operation": "generate_recipe",
            "stage": "gemini_video_analysis",
            "error_code": "gemini_video_analysis_failed",
            "source_platform": "tiktok",
            "retry_exhausted": True,
            "terminal": True,
        }
        values.update(overrides)
        return sentry.report_worker_error(error, **values)  # type: ignore[arg-type]

    def test_expected_product_failure_is_not_reported(self) -> None:
        with patch("app.observability.sentry.initialize_sentry") as initialize, patch(
            "app.observability.sentry.sentry_sdk.capture_exception"
        ) as capture:
            result = self._report(ValueError("user input"), error_code="invalid_url")
        self.assertIsNone(result)
        initialize.assert_not_called()
        capture.assert_not_called()

    def test_intermediate_retry_is_not_reported(self) -> None:
        with patch("app.observability.sentry.initialize_sentry") as initialize, patch(
            "app.observability.sentry.sentry_sdk.capture_exception"
        ) as capture:
            result = self._report(
                RuntimeError("temporary"),
                retry_attempt=1,
                retry_exhausted=False,
            )
        self.assertIsNone(result)
        initialize.assert_not_called()
        capture.assert_not_called()

    def test_retry_exhaustion_is_reported_once(self) -> None:
        error = RuntimeError("provider detail must be sanitized")
        scope = MagicMock()
        scope.__enter__.return_value = MagicMock()
        with patch("app.observability.sentry.initialize_sentry", return_value=True), patch(
            "app.observability.sentry.sentry_sdk.new_scope", return_value=scope
        ), patch(
            "app.observability.sentry.sentry_sdk.capture_exception",
            return_value="b" * 32,
        ) as capture:
            first = self._report(error, retry_attempt=2, retry_exhausted=True)
            second = self._report(error, retry_attempt=2, retry_exhausted=True)
        self.assertEqual(first, "b" * 32)
        self.assertIsNone(second)
        capture.assert_called_once_with(error)

    def test_controlled_test_refuses_production(self) -> None:
        values = render_env("production", "main", dev_test="true")
        with patch.dict(os.environ, values, clear=True), patch(
            "app.observability.sentry_test.initialize_sentry"
        ) as initialize:
            result = sentry_test.main()
        self.assertEqual(result, 2)
        initialize.assert_not_called()

    def test_two_terminal_size_jobs_capture_two_occurrences_with_same_fingerprint(self) -> None:
        scopes = []
        for _ in range(2):
            manager = MagicMock()
            scope = MagicMock()
            manager.__enter__.return_value = scope
            scopes.append((manager, scope))
        with patch("app.observability.sentry.initialize_sentry", return_value=True), patch(
            "app.observability.sentry.sentry_sdk.new_scope",
            side_effect=[item[0] for item in scopes],
        ), patch(
            "app.observability.sentry.sentry_sdk.capture_message",
            side_effect=["c" * 32, "d" * 32],
        ) as capture:
            results = [
                sentry.report_terminal_worker_outcome(
                    error_code="media_size_exceeded",
                    source_platform="instagram",
                    duration_bucket="60_90_seconds",
                    size_bucket="40_50_mib",
                    selected_resolution_bucket="720p",
                    adaptive_attempt_count=3,
                    cookie_retry_used=True,
                )
                for _ in range(2)
            ]
        self.assertEqual(results, ["c" * 32, "d" * 32])
        self.assertEqual(capture.call_count, 2)
        for _manager, scope in scopes:
            self.assertEqual(scope.fingerprint, sentry.MEDIA_SIZE_FINGERPRINT)

    def test_sentry_warning_delivery_failure_does_not_escape(self) -> None:
        manager = MagicMock()
        manager.__enter__.return_value = MagicMock()
        with patch("app.observability.sentry.initialize_sentry", return_value=True), patch(
            "app.observability.sentry.sentry_sdk.new_scope", return_value=manager
        ), patch(
            "app.observability.sentry.sentry_sdk.capture_message",
            side_effect=RuntimeError("transport failed"),
        ):
            result = sentry.report_terminal_worker_outcome(
                error_code="media_size_exceeded",
                source_platform="instagram",
                duration_bucket=None,
                size_bucket=None,
                selected_resolution_bucket=None,
                adaptive_attempt_count=4,
                cookie_retry_used=False,
            )
        self.assertIsNone(result)


class ActiveQueueBoundaryTests(unittest.TestCase):
    def test_expected_queue_failure_without_technical_cause_is_not_reported(self) -> None:
        failure = queue_worker.QueueJobFailure(
            "video_duration_exceeded",
            "Video is too long.",
            stage="unified_extractor",
        )
        with patch("app.queue_worker.report_worker_error") as report:
            queue_worker.report_queue_job_failure(failure, "tiktok")
        report.assert_not_called()

    def test_terminal_persistence_failure_maps_to_active_persistence_boundary(self) -> None:
        cause = RuntimeError("database response payload")
        try:
            raise queue_worker.QueueJobFailure(
                "recipe_card_insert_failed",
                "Recipe card could not be saved.",
                stage="recipe_card_saving",
                exception_type="RuntimeError",
            ) from cause
        except queue_worker.QueueJobFailure as failure:
            with patch("app.queue_worker.report_worker_error") as report:
                queue_worker.report_queue_job_failure(failure, "instagram")
        report.assert_called_once_with(
            cause,
            feature="persistence",
            operation="persist_recipe_card",
            stage="recipe_card_saving",
            error_code="recipe_card_insert_failed",
            source_platform="instagram",
            retry_exhausted=True,
            recoverable=False,
            terminal=True,
        )

    def test_terminal_failure_persistence_reports_and_preserves_exception(self) -> None:
        repository = MagicMock()
        persistence_error = RuntimeError("database failure")
        with patch(
            "app.queue_worker.record_queue_failure",
            side_effect=persistence_error,
        ), patch("app.queue_worker.report_worker_error") as report:
            with self.assertRaisesRegex(RuntimeError, "database failure"):
                queue_worker.record_queue_failure_with_reporting(
                    repository,
                    MagicMock(),
                    "queue_job_failed",
                    "Queue job failed.",
                    stage="queue_claim",
                    source_platform="youtube",
                )
        report.assert_called_once_with(
            persistence_error,
            feature="persistence",
            operation="persist_job_failure",
            stage="terminal_failure",
            error_code="terminal_failure_persistence_failed",
            source_platform="youtube",
            retry_exhausted=True,
            recoverable=False,
            terminal=True,
        )


if __name__ == "__main__":
    unittest.main()
