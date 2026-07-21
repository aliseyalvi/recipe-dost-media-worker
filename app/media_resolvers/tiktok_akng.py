from __future__ import annotations

import re
from urllib.parse import urlparse

from app.media_resolvers.contracts import (
    MediaCandidate,
    MediaResolution,
    MediaResolutionRequest,
    PublicMetadata,
    ResolverError,
)
from app.media_resolvers.http_client import SafeProviderHttpClient

ADAPTER_ID = "akng_tiktok"


class TikTokAkngResolver:
    adapter_id = ADAPTER_ID

    def __init__(
        self,
        *,
        client: SafeProviderHttpClient,
        base_url: str,
        access_token: str,
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._token = access_token

    def resolve(self, request: MediaResolutionRequest) -> MediaResolution:
        host = urlparse(self._base_url).hostname or ""
        payload, _status_class, retry_after = self._client.get_json(
            base_url=self._base_url,
            path="/tiktok/video",
            params={
                "input": request.canonical_source_url,
                "access_token": self._token,
            },
            headers={"Accept": "application/json"},
            expected_host=host,
        )
        if payload.get("success") is not True:
            message = str(payload.get("message") or "").lower()
            if "private" in message or "deleted" in message or "not found" in message:
                raise ResolverError(
                    "media_private_or_deleted",
                    "This TikTok video is private, deleted, or unavailable.",
                )
            if "invalid" in message:
                raise ResolverError("media_unavailable", "This TikTok link is invalid.")
            raise ResolverError(
                "resolver_temporarily_unavailable",
                "TikTok media could not be resolved.",
                retryable=True,
                fallback_allowed=True,
            )
        item = _mapping(_mapping(payload.get("data")).get("itemInfo")).get("itemStruct")
        item_map = _mapping(item)
        video = _mapping(item_map.get("video"))
        bitrate_info = video.get("bitrateInfo")
        if not isinstance(bitrate_info, list):
            raise ResolverError(
                "resolver_configuration_error",
                "The TikTok resolver returned an invalid media contract.",
                fallback_allowed=True,
                technical=True,
            )
        videos = tuple(
            candidate
            for candidate in (_video_candidate(value) for value in bitrate_info)
            if candidate is not None
        )
        audio_info = video.get("bitrateAudioInfo")
        audio_values = audio_info if isinstance(audio_info, list) else []
        audios = tuple(
            candidate
            for candidate in (_audio_candidate(value) for value in audio_values)
            if candidate is not None
        )
        if not videos:
            raise ResolverError(
                "unsupported_media",
                "TikTok did not return a supported video candidate.",
            )
        duration = _positive_int(video.get("duration"))
        return MediaResolution(
            adapter_id=self.adapter_id,
            media_mode="progressive",
            candidates=videos + audios,
            metadata=PublicMetadata(
                title=_safe_text(item_map.get("desc")),
                thumbnail_url=_https_url(video.get("cover")),
                duration_seconds=duration,
            ),
            retry_after_seconds=retry_after,
        )


def _video_candidate(value: object) -> MediaCandidate | None:
    item = _mapping(value)
    play_addr = _mapping(item.get("PlayAddr"))
    urls = _url_tuple(play_addr.get("UrlList"))
    if not urls:
        return None
    return MediaCandidate(
        media_type="video",
        _urls=urls,
        width=_positive_int(play_addr.get("Width")),
        height=_positive_int(play_addr.get("Height")),
        size_bytes=_positive_int(play_addr.get("DataSize")),
        codec=_safe_codec(item.get("CodecType")),
        container=_safe_container(item.get("Format")),
        bitrate=_positive_int(item.get("Bitrate")),
        quality_label=_safe_text(item.get("GearName")),
        audio_state="unknown",
        candidate_id=_safe_identifier(item.get("GearName")),
    )


def _audio_candidate(value: object) -> MediaCandidate | None:
    item = _mapping(value)
    url_data = _mapping(item.get("UrlList"))
    urls = _url_tuple(
        [url_data.get("MainUrl"), url_data.get("BackupUrl"), url_data.get("FallbackUrl")]
    )
    if not urls:
        return None
    return MediaCandidate(
        media_type="audio",
        _urls=urls,
        size_bytes=_positive_int(item.get("AudioDataSize")),
        codec=_safe_codec(item.get("CodecType")),
        container=_safe_container(item.get("AudioFormat") or item.get("Format")),
        bitrate=_positive_int(item.get("Bitrate")),
        quality_label=_safe_text(item.get("AudioQualityString")),
        audio_state="present",
        candidate_id=_safe_identifier(item.get("FileId")),
    )


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _url_tuple(value: object) -> tuple[str, ...]:
    values = value if isinstance(value, list) else []
    urls: list[str] = []
    for item in values:
        url = _https_url(item)
        if url and url not in urls:
            urls.append(url)
    return tuple(urls[:3])


def _https_url(value: object) -> str | None:
    text = str(value or "").strip()
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return text


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _safe_codec(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text[:40] if re.fullmatch(r"[a-z0-9_.-]+", text) else None


def _safe_container(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text[:20] if re.fullmatch(r"[a-z0-9_.-]+", text) else None


def _safe_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text[:500] if text else None


def _safe_identifier(value: object) -> str | None:
    text = str(value or "").strip()
    return text[:80] if re.fullmatch(r"[A-Za-z0-9_.-]+", text) else None
