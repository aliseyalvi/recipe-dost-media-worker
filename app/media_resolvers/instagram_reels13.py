"""Deprecated provider retained temporarily for offline test migration.

This adapter is not registered or callable from runtime configuration and is
scheduled for deletion after the M23.W9C development soak.
"""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

from app.media_resolvers.contracts import (
    MediaCandidate,
    MediaResolution,
    MediaResolutionRequest,
    PublicMetadata,
    ResolverError,
)
from app.media_resolvers.http_client import SafeProviderHttpClient
from app.media_resolvers.selection import declared_video_compatibility

ADAPTER_ID = "rapidapi_instagram_reels13"


class InstagramReels13Resolver:
    adapter_id = ADAPTER_ID

    def __init__(
        self,
        *,
        client: SafeProviderHttpClient,
        base_url: str,
        rapidapi_host: str,
        rapidapi_key: str,
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._host = rapidapi_host
        self._key = rapidapi_key

    def resolve(self, request: MediaResolutionRequest) -> MediaResolution:
        payload, status_class, retry_after = self._client.get_json(
            base_url=self._base_url,
            path="/download",
            params={"url": request.canonical_source_url},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-RapidAPI-Host": self._host,
                "X-RapidAPI-Key": self._key,
            },
            expected_host=self._host,
        )
        if payload.get("status") is not True:
            error_text = str(payload.get("error") or "").lower()
            if "private" in error_text or "deleted" in error_text or "not found" in error_text:
                raise ResolverError(
                    "media_private_or_deleted",
                    "This Instagram media is private, deleted, or unavailable.",
                )
            if "invalid" in error_text:
                raise ResolverError("media_unavailable", "This Instagram link is invalid.")
            raise ResolverError(
                "resolver_temporarily_unavailable",
                "Instagram media could not be resolved.",
                retryable=True,
                fallback_allowed=True,
            )
        medias = payload.get("medias")
        if not isinstance(medias, list):
            raise ResolverError(
                "resolver_configuration_error",
                "The Instagram resolver returned an invalid response.",
                fallback_allowed=True,
                technical=True,
            )
        candidates, summary = normalize_instagram_candidates(
            medias,
            byte_limit=request.byte_limit,
        )
        videos = [item for item in candidates if item.media_type == "video"]
        if not videos:
            raise ResolverError(
                "unsupported_media",
                "Instagram did not return a supported video.",
                failure_stage="candidate_selection",
                raw_video_candidate_count=int(summary["raw_video_count"]),
                raw_audio_candidate_count=int(summary["raw_audio_count"]),
                normalization_rejection_counts=dict(summary["rejection_counts"]),
                contract_diagnostic={
                    "provider_call_attempted": True,
                    "provider_status_class": status_class,
                    "contract_complete": True,
                    "instagram_candidate_normalization_summary": summary,
                },
            )
        audio = [item for item in candidates if item.media_type == "audio"]
        return MediaResolution(
            adapter_id=self.adapter_id,
            media_mode="split_av" if audio else "progressive",
            candidates=candidates,
            metadata=PublicMetadata(
                title=_safe_text(payload.get("title")),
                author=_safe_text(payload.get("author")),
                thumbnail_url=_https_url(payload.get("thumbnail")),
            ),
            raw_video_candidate_count=int(summary["raw_video_count"]),
            raw_audio_candidate_count=int(summary["raw_audio_count"]),
            normalization_rejection_counts=dict(summary["rejection_counts"]),
            contract_diagnostic={
                "provider_call_attempted": True,
                "provider_status_class": status_class,
                "contract_complete": True,
                "instagram_candidate_normalization_summary": summary,
            },
            retry_after_seconds=retry_after,
        )


def normalize_instagram_candidates(
    medias: list[object],
    *,
    byte_limit: int,
) -> tuple[tuple[MediaCandidate, ...], dict[str, object]]:
    candidates: list[MediaCandidate] = []
    rejections: Counter[str] = Counter()
    raw_video_count = 0
    raw_audio_count = 0
    unknown_role_count = 0
    declared_codec_present_count = 0
    declared_container_present_count = 0
    declared_mime_present_count = 0
    size_present_count = 0
    dimensions_present_count = 0

    for value in medias:
        if not isinstance(value, dict):
            unknown_role_count += 1
            rejections["malformed_candidate"] += 1
            continue
        media_type = str(value.get("type") or "").strip().lower()
        if media_type == "video":
            raw_video_count += 1
        elif media_type == "audio":
            raw_audio_count += 1
        else:
            unknown_role_count += 1
            rejections["role_not_video"] += 1
            continue

        codec = _safe_declaration(value.get("codec"))
        container = _safe_declaration(value.get("container") or value.get("extension"))
        mime_type = _safe_declaration(
            value.get("mime_type") or value.get("mimeType") or value.get("mime")
        )
        size_bytes = _positive_int(value.get("size_bytes") or value.get("size"))
        if codec:
            declared_codec_present_count += 1
        if container:
            declared_container_present_count += 1
        if mime_type:
            declared_mime_present_count += 1
        if size_bytes is not None:
            size_present_count += 1
            if size_bytes > byte_limit:
                rejections["size_exceeds_limit"] += 1
                continue

        resolution_value = value.get("resolution")
        width, height = _dimensions(resolution_value)
        if width is not None and height is not None:
            dimensions_present_count += 1
        elif (
            resolution_value is not None
            and resolution_value != ""
            and media_type == "video"
        ):
            rejections["dimensions_invalid"] += 1

        urls, url_rejections = _https_urls(value)
        rejections.update(url_rejections)
        if not urls:
            continue
        candidate = MediaCandidate(
            media_type=media_type,  # type: ignore[arg-type]
            _urls=urls,
            width=width,
            height=height,
            size_bytes=size_bytes,
            mime_type=mime_type,
            codec=codec,
            container=container,
            quality_label=_safe_text(value.get("quality")),
            audio_state="unknown" if media_type == "video" else "present",
            candidate_id=_safe_identifier(value.get("id")),
        )
        if (
            media_type == "video"
            and declared_video_compatibility(candidate) == "incompatible_declared"
        ):
            if codec and codec not in {"h264", "avc", "avc1", "unknown"}:
                rejections["explicit_unsupported_codec"] += 1
            elif container and container not in {"mp4", "m4v", "unknown"}:
                rejections["explicit_unsupported_container"] += 1
            continue
        candidates.append(candidate)

    summary: dict[str, object] = {
        "raw_candidate_count": len(medias),
        "raw_video_count": raw_video_count,
        "raw_audio_count": raw_audio_count,
        "unknown_role_count": unknown_role_count,
        "declared_codec_present_count": declared_codec_present_count,
        "declared_container_present_count": declared_container_present_count,
        "declared_mime_present_count": declared_mime_present_count,
        "size_present_count": size_present_count,
        "dimensions_present_count": dimensions_present_count,
        "eligible_declared_video_count": sum(
            1
            for candidate in candidates
            if candidate.media_type == "video"
            and declared_video_compatibility(candidate) == "compatible_declared"
        ),
        "probe_required_video_count": sum(
            1
            for candidate in candidates
            if candidate.media_type == "video"
            and declared_video_compatibility(candidate) == "probe_required"
        ),
        "eligible_audio_count": sum(
            1 for candidate in candidates if candidate.media_type == "audio"
        ),
        "rejection_counts": dict(rejections),
    }
    return tuple(candidates), summary


def _candidate_from_media(value: object) -> MediaCandidate | None:
    candidates, _summary = normalize_instagram_candidates(
        [value],
        byte_limit=2**63 - 1,
    )
    return candidates[0] if candidates else None


def _https_urls(value: dict[str, object]) -> tuple[tuple[str, ...], Counter[str]]:
    rejections: Counter[str] = Counter()
    raw_values: list[object] = []
    for key in ("urls", "url_alternatives", "download_urls"):
        candidate_values = value.get(key)
        if isinstance(candidate_values, list):
            raw_values.extend(candidate_values)
        elif candidate_values is not None and candidate_values != "":
            raw_values.append(candidate_values)
    primary = value.get("url")
    if isinstance(primary, list):
        raw_values.extend(primary)
    elif primary is not None and primary != "":
        raw_values.insert(0, primary)
    if not raw_values:
        rejections["missing_url"] += 1
        return (), rejections
    urls: list[str] = []
    for raw in raw_values:
        text = str(raw or "").strip()
        try:
            parsed = urlparse(text)
        except ValueError:
            rejections["invalid_url"] += 1
            continue
        if parsed.scheme != "https":
            rejections["non_https"] += 1
            continue
        if not parsed.hostname or parsed.username or parsed.password:
            rejections["invalid_url"] += 1
            continue
        if text not in urls:
            urls.append(text)
    return tuple(urls), rejections


def _safe_declaration(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text[:80] if text else None


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _dimensions(value: object) -> tuple[int | None, int | None]:
    match = re.fullmatch(r"\s*(\d{2,5})\s*[xX]\s*(\d{2,5})\s*", str(value or ""))
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _https_url(value: object) -> str | None:
    text = str(value or "").strip()
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return text


def _safe_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text[:500] if text else None


def _safe_identifier(value: object) -> str | None:
    text = str(value or "").strip()
    return text[:80] if re.fullmatch(r"[A-Za-z0-9_.-]+", text) else None
