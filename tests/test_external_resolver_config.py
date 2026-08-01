from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

from pydantic import ValidationError

from app.config import (
    INSTAGRAM_UNIFIED_V1_RAPIDAPI_HOST,
    Settings,
    validate_external_resolver_runtime_config,
)
from app.media_resolvers.contracts import ResolverError
from app.media_resolvers.instagram_unified_v1 import InstagramUnifiedV1Resolver
from app.media_resolvers.registry import build_resolver, registered_adapter_ids
from app.queue_worker import external_audience_route
from app.schemas import MediaJobRecord
from scripts import check_instagram_media_candidate as instagram_diagnostic


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "WORKER_API_SECRET": "test",
        "supabase_url": "https://example.supabase.co",
        "supabase_service_role_key": "test",
        "media_resolver_mode": "legacy",
        "external_media_resolver_audience": "disabled",
        "instagram_resolver_chain": [],
        "tiktok_resolver_chain": [],
        "cookie_media_fallback_enabled": False,
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
            settings(instagram_resolver_chain=["rapidapi_instagram_unified_v1"])
        )
        with self.assertRaisesRegex(ValueError, "credentials"):
            validate_external_resolver_runtime_config(
                settings(
                    media_resolver_mode="external",
                    external_media_resolver_audience="all_authorized_ai",
                    instagram_resolver_chain=["rapidapi_instagram_unified_v1"],
                )
            )

    def test_retired_instagram_adapter_cannot_be_primary_or_fallback(self) -> None:
        for chain in (
            ["rapidapi_instagram_reels13"],
            [
                "rapidapi_instagram_unified_v1",
                "rapidapi_instagram_reels13",
            ],
        ):
            with self.subTest(chain=chain), self.assertRaisesRegex(
                ValidationError,
                "rapidapi_instagram_reels13 is retired and cannot be enabled",
            ):
                settings(instagram_resolver_chain=chain)

    def test_registry_exposes_and_constructs_only_active_adapters(self) -> None:
        self.assertEqual(
            registered_adapter_ids(),
            {"rapidapi_instagram_unified_v1", "akng_tiktok"},
        )
        active = settings(
            instagram_rapidapi_key="test-only",
            instagram_rapidapi_host=INSTAGRAM_UNIFIED_V1_RAPIDAPI_HOST,
            instagram_rapidapi_base_url=(
                f"https://{INSTAGRAM_UNIFIED_V1_RAPIDAPI_HOST}"
            ),
            instagram_media_host_suffixes=["fbcdn.net", "cdninstagram.com"],
        )
        self.assertIsInstance(
            build_resolver("rapidapi_instagram_unified_v1", active),
            InstagramUnifiedV1Resolver,
        )
        with self.assertRaises(ResolverError):
            build_resolver("rapidapi_instagram_reels13", active)

    def test_fresh_registry_import_does_not_import_reels13_module(self) -> None:
        code = (
            "import sys; import app.media_resolvers.registry; "
            "assert 'app.media_resolvers.instagram_reels13' not in sys.modules"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env={"APP_ENV": "production", "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_owner_diagnostic_uses_unified_instagram_only(self) -> None:
        active = instagram_diagnostic._diagnostic_settings("test-only")
        self.assertEqual(
            active.instagram_resolver_chain,
            ["rapidapi_instagram_unified_v1"],
        )
        self.assertNotIn("instagram_reels13_enabled", type(active).model_fields)

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
