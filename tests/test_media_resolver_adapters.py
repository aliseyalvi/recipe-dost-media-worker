from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.media_resolvers.contracts import MediaResolutionRequest, ResolverError
from app.media_resolvers.instagram_reels13 import InstagramReels13Resolver
from app.media_resolvers.selection import select_video_candidate
from app.media_resolvers.tiktok_akng import TikTokAkngResolver

FIXTURES = Path(__file__).parent / "fixtures" / "media_resolvers"


class FakeClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def get_json(self, **_kwargs: object):
        return self.payload, "2xx", None


def fixture(adapter: str, name: str) -> dict[str, object]:
    return json.loads((FIXTURES / adapter / name).read_text(encoding="utf-8"))


def request(platform: str) -> MediaResolutionRequest:
    return MediaResolutionRequest(
        platform=platform,
        canonical_source_url=f"https://{platform}.example/video",
        correlation_id="fixture-job",
        capability="video_with_audio",
        byte_limit=50 * 1024 * 1024,
        preferred_byte_target=36 * 1024 * 1024,
        timeout_seconds=15,
    )


class InstagramAdapterTests(unittest.TestCase):
    def resolver(self, name: str) -> InstagramReels13Resolver:
        return InstagramReels13Resolver(
            client=FakeClient(fixture("rapidapi_instagram_reels13", name)),  # type: ignore[arg-type]
            base_url="https://provider.invalid",
            rapidapi_host="provider.invalid",
            rapidapi_key="fixture-secret",
        )

    def test_split_video_audio_and_safe_serialization(self) -> None:
        result = self.resolver("success.json").resolve(request("instagram"))
        self.assertEqual(result.media_mode, "split_av")
        self.assertEqual(len([c for c in result.candidates if c.media_type == "video"]), 1)
        serialized = json.dumps(result.safe_to_dict())
        self.assertNotIn("provider.invalid", serialized)
        self.assertNotIn("fixture-secret", serialized)
        self.assertNotIn("fixture-title", serialized)
        self.assertNotIn("provider.invalid", repr(result))

    def test_no_video_is_rejected(self) -> None:
        with self.assertRaisesRegex(ResolverError, "unsupported_media"):
            self.resolver("no_video.json").resolve(request("instagram"))

    def test_private_and_invalid_are_terminal(self) -> None:
        with self.assertRaises(ResolverError) as private:
            self.resolver("private_or_deleted.json").resolve(request("instagram"))
        self.assertEqual(private.exception.code, "media_private_or_deleted")
        self.assertFalse(private.exception.fallback_allowed)
        with self.assertRaises(ResolverError) as invalid:
            self.resolver("invalid_url.json").resolve(request("instagram"))
        self.assertEqual(invalid.exception.code, "media_unavailable")


class TikTokAdapterTests(unittest.TestCase):
    def resolver(self, name: str) -> TikTokAkngResolver:
        return TikTokAkngResolver(
            client=FakeClient(fixture("akng_tiktok", name)),  # type: ignore[arg-type]
            base_url="https://provider.invalid",
            access_token="fixture-secret",
        )

    def test_multi_quality_contract_selects_24_8_mb_h264_candidate(self) -> None:
        result = self.resolver("success_multi_quality.json").resolve(request("tiktok"))
        selected = select_video_candidate(
            result.candidates,
            preferred_bytes=36 * 1024 * 1024,
            hard_limit_bytes=50 * 1024 * 1024,
        )
        self.assertEqual(selected.candidate_id, "lower-540")
        self.assertEqual(selected.size_bytes, 24_828_499)
        self.assertEqual(selected.codec, "h264")
        self.assertEqual(len([c for c in result.candidates if c.media_type == "audio"]), 1)
        serialized = json.dumps(result.safe_to_dict())
        self.assertNotIn("provider.invalid", serialized)
        self.assertNotIn("fixture-secret", serialized)

    def test_h264_720_within_target_is_preferred_when_available(self) -> None:
        result = self.resolver("success_multi_quality.json").resolve(request("tiktok"))
        fixture_candidate = result.candidates[3]
        candidate_720 = type(fixture_candidate)(
            media_type="video",
            _urls=("https://provider.invalid/media/720-h264",),
            width=720,
            height=1280,
            size_bytes=30 * 1024 * 1024,
            codec="h264",
            container="mp4",
            candidate_id="h264-720",
        )
        selected = select_video_candidate(
            result.candidates + (candidate_720,),
            preferred_bytes=36 * 1024 * 1024,
            hard_limit_bytes=50 * 1024 * 1024,
        )
        self.assertEqual(selected.candidate_id, "h264-720")

    def test_missing_bitrate_contract_is_technical(self) -> None:
        with self.assertRaises(ResolverError) as captured:
            self.resolver("missing_bitrate_info.json").resolve(request("tiktok"))
        self.assertEqual(captured.exception.code, "resolver_configuration_error")
        self.assertTrue(captured.exception.technical)


if __name__ == "__main__":
    unittest.main()
