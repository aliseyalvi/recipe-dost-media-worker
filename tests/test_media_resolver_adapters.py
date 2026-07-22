from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.media_resolvers.contracts import MediaResolutionRequest, ResolverError
from app.media_resolvers.instagram_reels13 import InstagramReels13Resolver
from app.media_resolvers.selection import (
    evaluate_video_candidates,
    rank_video_candidates,
    select_video_candidate,
)
from app.media_resolvers.tiktok_akng import TikTokAkngResolver, normalize_akng_input
from app.media_resolvers.tiktok_input import TikTokCanonicalInput

FIXTURES = Path(__file__).parent / "fixtures" / "media_resolvers"


class FakeClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.kwargs: dict[str, object] = {}

    def get_json(self, **kwargs: object):
        self.kwargs = kwargs
        return self.payload, "2xx", None


class SequencedClient(FakeClient):
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        super().__init__(payloads[0])
        self.payloads = list(payloads)
        self.calls: list[dict[str, object]] = []

    def get_json(self, **kwargs: object):
        self.calls.append(kwargs)
        return self.payloads.pop(0), "2xx", None


def fixture(adapter: str, name: str) -> dict[str, object]:
    return json.loads((FIXTURES / adapter / name).read_text(encoding="utf-8"))


def request(platform: str) -> MediaResolutionRequest:
    source_url = (
        "https://www.tiktok.com/@fixture/video/7651218940919598000"
        if platform == "tiktok"
        else f"https://{platform}.example/video"
    )
    return MediaResolutionRequest(
        platform=platform,
        canonical_source_url=source_url,
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
        selected = select_video_candidate(
            result.candidates,
            preferred_bytes=36 * 1024 * 1024,
            hard_limit_bytes=50 * 1024 * 1024,
            allowed_host_suffixes=("provider.invalid",),
        )
        self.assertEqual(selected.candidate_id, "video-720")
        self.assertIsNone(selected.size_bytes)
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
        self.assertEqual(len([c for c in result.candidates if c.media_type == "audio"]), 3)
        self.assertEqual(result.raw_video_candidate_count, 5)
        self.assertEqual(result.raw_audio_candidate_count, 3)
        self.assertEqual(result.contract_diagnostic["provider_status_class"], "2xx")
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

    def test_initial_provider_host_is_allowlisted_without_path_suffix_assumption(self) -> None:
        result = self.resolver("success_multi_quality.json").resolve(request("tiktok"))
        eligible, rejections = evaluate_video_candidates(
            result.candidates,
            hard_limit_bytes=50 * 1024 * 1024,
            max_short_edge=720,
            allowed_host_suffixes=("provider.invalid",),
        )
        self.assertEqual(len(eligible), 2)
        self.assertNotIn("disallowed_initial_host", rejections)
        unknown, unknown_rejections = evaluate_video_candidates(
            result.candidates,
            hard_limit_bytes=50 * 1024 * 1024,
            max_short_edge=720,
            allowed_host_suffixes=("api.akng.io.vn",),
        )
        self.assertEqual(unknown, [])
        self.assertEqual(unknown_rejections["disallowed_initial_host"], 2)

    def test_missing_bitrate_contract_is_technical(self) -> None:
        with self.assertRaises(ResolverError) as captured:
            self.resolver("missing_bitrate_info.json").resolve(request("tiktok"))
        self.assertEqual(captured.exception.code, "resolver_contract_error")
        self.assertTrue(captured.exception.technical)
        self.assertEqual(captured.exception.http_status_class, "2xx")

    def test_missing_data_is_contract_error_and_empty_candidates_are_media_outcome(self) -> None:
        with self.assertRaises(ResolverError) as malformed:
            self.resolver("malformed.json").resolve(request("tiktok"))
        self.assertEqual(malformed.exception.code, "resolver_contract_error")
        self.assertEqual(malformed.exception.contract_diagnostic["data_type"], "string")
        with self.assertRaises(ResolverError) as empty:
            self.resolver("no_download_candidate.json").resolve(request("tiktok"))
        self.assertEqual(empty.exception.code, "unsupported_media")

    def test_all_malformed_candidates_are_contract_error_with_reason_counts(self) -> None:
        payload = {
            "success": True,
            "data": {
                "itemInfo": {
                    "itemStruct": {
                        "video": {
                            "bitrateInfo": [
                                {"PlayAddr": {"UrlList": [], "DataSize": "123", "Width": 576, "Height": 1024}},
                                {"PlayAddr": {"UrlList": ["http://unsafe.invalid/media"], "DataSize": "123", "Width": 576, "Height": 1024}, "CodecType": "h264", "Format": "mp4"},
                            ]
                        }
                    }
                }
            },
        }
        resolver = TikTokAkngResolver(
            client=FakeClient(payload),  # type: ignore[arg-type]
            base_url="https://provider.invalid",
            access_token="fixture-secret",
        )
        with self.assertRaises(ResolverError) as captured:
            resolver.resolve(request("tiktok"))
        self.assertEqual(captured.exception.code, "resolver_contract_error")
        self.assertEqual(captured.exception.raw_video_candidate_count, 2)
        self.assertEqual(captured.exception.normalization_rejection_counts["empty_url_list"], 1)
        self.assertEqual(captured.exception.normalization_rejection_counts["non_https_url"], 1)

    def test_long_url_is_sent_as_numeric_id(self) -> None:
        client = FakeClient(fixture("akng_tiktok", "success_multi_quality.json"))
        resolver = TikTokAkngResolver(
            client=client,  # type: ignore[arg-type]
            base_url="https://provider.invalid",
            access_token="fixture-secret",
        )
        media_request = request("tiktok")
        media_request = type(media_request)(
            platform="tiktok",
            canonical_source_url="https://www.tiktok.com/@fixture/video/7651218940919598000?tracking=removed",
            correlation_id=media_request.correlation_id,
            capability=media_request.capability,
            byte_limit=media_request.byte_limit,
            preferred_byte_target=media_request.preferred_byte_target,
            timeout_seconds=media_request.timeout_seconds,
        )
        resolver.resolve(media_request)
        params = client.kwargs["params"]
        self.assertIsInstance(params, dict)
        self.assertEqual(params["input"], "7651218940919598000")
        self.assertEqual(
            resolver.resolve(media_request).contract_diagnostic["provider_input_kind"],
            "numeric_id",
        )

    def test_short_url_cannot_be_normalized_for_direct_provider_use(self) -> None:
        with self.assertRaises(ResolverError) as captured:
            normalize_akng_input(
                "https://vt.tiktok.com/ZSFixture/?_tracking=removed#fragment"
            )
        self.assertEqual(captured.exception.code, "media_unavailable")

    def test_non_tiktok_url_is_rejected_before_provider_call(self) -> None:
        with self.assertRaises(ResolverError) as captured:
            normalize_akng_input("https://example.invalid/video/7651218940919598000")
        self.assertEqual(captured.exception.code, "media_unavailable")

    def test_input_not_valid_is_terminal_media_outcome_with_2xx_status(self) -> None:
        with self.assertRaises(ResolverError) as captured:
            self.resolver("input_not_valid.json").resolve(request("tiktok"))
        self.assertEqual(captured.exception.code, "media_unavailable")
        self.assertFalse(captured.exception.retryable)
        self.assertFalse(captured.exception.fallback_allowed)
        self.assertEqual(captured.exception.http_status_class, "2xx")
        self.assertEqual(captured.exception.contract_diagnostic["success_value"], False)

    def test_empty_numeric_contract_uses_canonical_url_once(self) -> None:
        empty = {"success": True, "message": "", "data": {}}
        client = SequencedClient(
            [empty, fixture("akng_tiktok", "success_multi_quality.json")]
        )
        resolver = TikTokAkngResolver(
            client=client,  # type: ignore[arg-type]
            base_url="https://provider.invalid",
            access_token="fixture-secret",
        )
        result = resolver.resolve(request("tiktok"))
        self.assertEqual(len(client.calls), 2)
        first_params = client.calls[0]["params"]
        second_params = client.calls[1]["params"]
        self.assertIsInstance(first_params, dict)
        self.assertIsInstance(second_params, dict)
        self.assertEqual(result.contract_diagnostic["provider_input_kind"], "canonical_url")
        self.assertTrue(result.contract_diagnostic["alternate_input_attempted"])
        self.assertNotIn("tiktok.com", str(first_params["input"]))
        self.assertIn("tiktok.com", str(second_params["input"]))

    def test_both_verified_input_forms_empty_fail_as_media_unavailable(self) -> None:
        empty = {"success": True, "message": "", "data": {}}
        client = SequencedClient([empty, empty])
        resolver = TikTokAkngResolver(
            client=client,  # type: ignore[arg-type]
            base_url="https://provider.invalid",
            access_token="fixture-secret",
        )
        with self.assertRaises(ResolverError) as captured:
            resolver.resolve(request("tiktok"))
        self.assertEqual(captured.exception.code, "media_unavailable")
        self.assertEqual(len(client.calls), 2)

    def test_malformed_nested_contract_does_not_try_alternate_input(self) -> None:
        malformed = {"success": True, "data": {"itemInfo": {"unexpected": True}}}
        client = SequencedClient([malformed])
        resolver = TikTokAkngResolver(
            client=client,  # type: ignore[arg-type]
            base_url="https://provider.invalid",
            access_token="fixture-secret",
        )
        with self.assertRaises(ResolverError) as captured:
            resolver.resolve(request("tiktok"))
        self.assertEqual(captured.exception.code, "resolver_contract_error")
        self.assertEqual(len(client.calls), 1)

    def test_short_url_is_expanded_and_never_sent_directly(self) -> None:
        client = FakeClient(fixture("akng_tiktok", "success_multi_quality.json"))
        resolver = TikTokAkngResolver(
            client=client,  # type: ignore[arg-type]
            base_url="https://provider.invalid",
            access_token="fixture-secret",
        )
        media_request = request("tiktok")
        short_request = type(media_request)(
            platform="tiktok",
            canonical_source_url="https://vt.tiktok.com/ZSFixture/",
            correlation_id=media_request.correlation_id,
            capability=media_request.capability,
            byte_limit=media_request.byte_limit,
            preferred_byte_target=media_request.preferred_byte_target,
            timeout_seconds=media_request.timeout_seconds,
        )
        expanded = TikTokCanonicalInput(
            canonical_url="https://www.tiktok.com/@fixture/video/7651218940919598000",
            video_id="7651218940919598000",
            original_input_kind="short_url",
            redirect_count=1,
            canonical_source="redirect_path",
        )
        with patch(
            "app.media_resolvers.tiktok_akng.resolve_tiktok_canonical_input",
            return_value=expanded,
        ):
            resolver.resolve(short_request)
        params = client.kwargs["params"]
        self.assertIsInstance(params, dict)
        self.assertNotIn("vt.tiktok.com", str(params["input"]))
        self.assertNotIn("tiktok.com", str(params["input"]))

    def test_six_candidate_contract_uses_live_proven_smaller_h264_policy(self) -> None:
        result = self.resolver("success_six_candidates.json").resolve(request("tiktok"))
        ranked = rank_video_candidates(
            result.candidates,
            preferred_bytes=36 * 1024 * 1024,
            hard_limit_bytes=50 * 1024 * 1024,
            allowed_host_suffixes=("provider.invalid",),
            prefer_smallest_compatible=True,
        )
        self.assertEqual(result.raw_video_candidate_count, 6)
        self.assertEqual(result.raw_audio_candidate_count, 0)
        self.assertEqual(result.contract_diagnostic["download_urls_count"], 22)
        self.assertEqual(ranked[0].candidate_id, "h264-lower")
        self.assertEqual(ranked[0].size_bytes, 4_721_022)
        self.assertEqual(ranked[0].bitrate, 728_173)
        self.assertEqual(ranked[1].candidate_id, "h264-high")
        self.assertEqual(ranked[2].candidate_id, "top_level_progressive")
        self.assertNotIn("h265", " ".join(str(item.candidate_id) for item in ranked))

    def test_proxy_url_without_media_suffix_is_accepted(self) -> None:
        result = self.resolver("success_multi_quality.json").resolve(request("tiktok"))
        selected = next(item for item in result.candidates if item.candidate_id == "lower-540")
        self.assertIn("/tiktok/download?", selected.urls()[0])
        self.assertFalse(url_path_has_media_suffix(selected.urls()[0]))

    def test_source_context_is_bounded_and_omits_sensitive_provider_fields(self) -> None:
        result = self.resolver("success_multi_quality.json").resolve(request("tiktok"))
        context = result.source_context.to_prompt_dict()
        serialized = json.dumps(context)
        self.assertIn("Fixture recipe caption", serialized)
        self.assertNotIn("example.invalid", serialized)
        self.assertNotIn("555", serialized)
        self.assertNotIn("redacted-author-id", serialized)
        self.assertNotIn("secUid", serialized)
        self.assertLessEqual(len(str(context.get("caption") or "")), 1500)
        self.assertLessEqual(len(context.get("hashtags") or []), 20)


def url_path_has_media_suffix(url: str) -> bool:
    return url.split("?", 1)[0].endswith((".mp4", ".m4a"))


if __name__ == "__main__":
    unittest.main()
