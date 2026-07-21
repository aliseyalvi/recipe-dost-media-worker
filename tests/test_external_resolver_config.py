from __future__ import annotations

import unittest
from unittest.mock import MagicMock
from uuid import UUID

from pydantic import ValidationError

from app.config import Settings, validate_external_resolver_runtime_config
from app.queue_worker import external_audience_route
from app.schemas import MediaJobRecord


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "WORKER_API_SECRET": "test",
        "supabase_url": "https://example.supabase.co",
        "supabase_service_role_key": "test",
        "media_resolver_mode": "legacy",
        "external_media_resolver_audience": "disabled",
        "instagram_resolver_chain": [],
        "tiktok_resolver_chain": [],
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def job() -> MediaJobRecord:
    return MediaJobRecord(
        id=UUID("00000000-0000-0000-0000-000000000111"),
        user_id=UUID("00000000-0000-0000-0000-000000000222"),
        recipe_item_id=None,
        source_url="https://source.invalid/video",
        platform="instagram",
        status="queued",
        progress=0,
    )


class ExternalResolverConfigTests(unittest.TestCase):
    def test_safe_defaults_disable_external_traffic(self) -> None:
        active = settings()
        self.assertEqual(active.media_resolver_mode, "legacy")
        self.assertEqual(active.external_media_resolver_audience, "disabled")
        self.assertFalse(active.cookie_media_fallback_enabled)

    def test_unknown_mode_and_adapter_fail_closed(self) -> None:
        with self.assertRaises(ValidationError):
            settings(media_resolver_mode="unknown")
        with self.assertRaises(ValidationError):
            settings(instagram_resolver_chain=["unknown_adapter"])

    def test_credentials_required_only_when_adapter_can_receive_traffic(self) -> None:
        validate_external_resolver_runtime_config(
            settings(instagram_resolver_chain=["rapidapi_instagram_reels13"])
        )
        with self.assertRaisesRegex(ValueError, "credentials"):
            validate_external_resolver_runtime_config(
                settings(
                    media_resolver_mode="external",
                    external_media_resolver_audience="all_authorized_ai",
                    instagram_resolver_chain=["rapidapi_instagram_reels13"],
                    instagram_rapidapi_host="provider.invalid",
                    instagram_rapidapi_base_url="https://provider.invalid",
                )
            )

    def test_all_authorized_ai_uses_server_access_without_granting_it(self) -> None:
        repository = MagicMock()
        active = settings(external_media_resolver_audience="all_authorized_ai")
        repository.get_ai_access_context.return_value = {"allowed": False, "reason": "limit_reached"}
        self.assertIsNone(external_audience_route(repository, active, job()))
        repository.get_ai_access_context.return_value = {
            "allowed": True,
            "reason": "free_credit_available",
            "role": "user",
        }
        self.assertEqual(external_audience_route(repository, active, job()), "free_credit")
        repository.get_ai_access_context.return_value = {
            "allowed": True,
            "reason": "premium_active",
            "role": "user",
        }
        self.assertEqual(external_audience_route(repository, active, job()), "premium")

    def test_test_user_audience_is_server_derived(self) -> None:
        repository = MagicMock()
        active = settings(external_media_resolver_audience="test_users")
        repository.get_ai_access_context.return_value = {
            "allowed": True,
            "reason": "free_credit_available",
            "role": "user",
            "is_test_user": False,
        }
        self.assertIsNone(external_audience_route(repository, active, job()))
        repository.get_ai_access_context.return_value["is_test_user"] = True
        self.assertEqual(external_audience_route(repository, active, job()), "test")


if __name__ == "__main__":
    unittest.main()
