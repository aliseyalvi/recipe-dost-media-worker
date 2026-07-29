from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID

from app.config import Settings
from app.media_resolvers.contracts import ResolverError
from app.queue_worker import QueueJobFailure, resolve_queue_media
from app.schemas import MediaJobRecord


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "WORKER_API_SECRET": "test",
        "supabase_url": "https://example.supabase.co",
        "supabase_service_role_key": "test",
        "media_resolver_mode": "external",
        "external_media_resolver_audience": "all_authorized_ai",
        "instagram_resolver_chain": ["rapidapi_instagram_unified_v1"],
        "tiktok_resolver_chain": ["akng_tiktok"],
        "instagram_rapidapi_key": "secret",
        "instagram_rapidapi_host": "provider.invalid",
        "instagram_rapidapi_base_url": "https://provider.invalid",
        "tiktok_akng_access_token": "secret",
        "tiktok_akng_base_url": "https://provider.invalid",
        "ai_analysis_mode": "gemini_video_poc",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def job(platform: str = "instagram") -> MediaJobRecord:
    return MediaJobRecord(
        id=UUID("00000000-0000-0000-0000-000000000111"),
        user_id=UUID("00000000-0000-0000-0000-000000000222"),
        recipe_item_id=None,
        source_url="https://source.invalid/video",
        platform=platform,
        status="resolving",
        progress=20,
    )


class ExternalQueueRoutingTests(unittest.TestCase):
    def repository(self, reason: str = "free_credit_available") -> MagicMock:
        repository = MagicMock()
        repository.get_ai_access_context.return_value = {
            "allowed": True,
            "reason": reason,
            "role": "user",
        }
        return repository

    def test_free_credit_and_premium_authorized_jobs_use_external_route(self) -> None:
        for reason in ("free_credit_available", "premium_active"):
            repository = self.repository(reason)
            prepared = {
                "success": True,
                "resolver": "rapidapi_instagram_unified_v1",
                "temp_file_path": "/tmp/fixture.mp4",
                "video_bytes_verified": True,
            }
            with patch("app.queue_worker.resolve_external_media", return_value=prepared.copy()) as external, patch(
                "app.queue_worker.extract_social_video"
            ) as legacy:
                result = resolve_queue_media(
                    repository=repository,
                    settings=settings(),
                    job=job(),
                    platform="instagram",
                )
            external.assert_called_once()
            legacy.assert_not_called()
            self.assertEqual(result["resolver_strategy"], "external")

    def test_unauthorized_context_never_calls_external_provider(self) -> None:
        repository = self.repository()
        repository.get_ai_access_context.return_value = {"allowed": False, "reason": "limit_reached"}
        with patch("app.queue_worker.resolve_external_media") as external:
            with self.assertRaises(Exception):
                resolve_queue_media(repository=repository, settings=settings(), job=job(), platform="instagram")
        external.assert_not_called()

    def test_legacy_cookie_use_requires_mode_and_independent_flag(self) -> None:
        repository = self.repository()
        legacy_result = {"success": True, "resolver": "ytdlp_stream"}
        active = settings(
            media_resolver_mode="legacy",
            external_media_resolver_audience="disabled",
            cookie_media_fallback_enabled=False,
            enable_ytdlp_cookies=True,
            enable_instagram_cookies=True,
        )
        with patch("app.queue_worker.extract_social_video", return_value=legacy_result) as legacy:
            resolve_queue_media(repository=repository, settings=active, job=job(), platform="instagram")
        passed_settings = legacy.call_args.args[3]
        self.assertFalse(passed_settings.enable_ytdlp_cookies)
        self.assertFalse(passed_settings.enable_instagram_cookies)

    def test_youtube_never_uses_external_resolver(self) -> None:
        repository = self.repository()
        active = settings(media_resolver_mode="legacy", external_media_resolver_audience="disabled")
        with patch("app.queue_worker.resolve_external_media") as external, patch(
            "app.queue_worker.extract_social_video", return_value={"success": True, "resolver": "legacy"}
        ):
            resolve_queue_media(repository=repository, settings=active, job=job("youtube"), platform="youtube")
        external.assert_not_called()

    def test_external_contract_failure_retains_attempt_and_status_class(self) -> None:
        repository = self.repository()
        error = ResolverError(
            "resolver_contract_error",
            "The TikTok resolver returned an unexpected media contract.",
            http_status_class="2xx",
            technical=True,
        )
        error.adapter_id = "akng_tiktok"
        error.provider_attempt = 1
        with patch("app.queue_worker.resolve_external_media", side_effect=error):
            with self.assertRaises(QueueJobFailure) as captured:
                resolve_queue_media(
                    repository=repository,
                    settings=settings(),
                    job=job("tiktok"),
                    platform="tiktok",
                )
        self.assertEqual(captured.exception.code, "resolver_contract_error")
        self.assertEqual(captured.exception.metadata["provider_attempt"], 1)
        self.assertEqual(captured.exception.metadata["provider_status_class"], "2xx")
        self.assertEqual(captured.exception.metadata["resolver_adapter"], "akng_tiktok")


if __name__ == "__main__":
    unittest.main()
