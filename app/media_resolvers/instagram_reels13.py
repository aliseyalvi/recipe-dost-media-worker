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
        payload, _status_class, retry_after = self._client.get_json(
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
        candidates = tuple(
            candidate
            for candidate in (_candidate_from_media(item) for item in medias)
            if candidate is not None
        )
        videos = [item for item in candidates if item.media_type == "video"]
        if not videos:
            raise ResolverError(
                "unsupported_media",
                "Instagram did not return a supported video.",
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
            retry_after_seconds=retry_after,
        )


def _candidate_from_media(value: object) -> MediaCandidate | None:
    if not isinstance(value, dict):
        return None
    media_type = str(value.get("type") or "").strip().lower()
    if media_type not in {"video", "audio"}:
        return None
    url = _https_url(value.get("url"))
    extension = str(value.get("extension") or "").strip().lower()
    if not url or extension not in {"mp4", "m4a"}:
        return None
    width, height = _dimensions(value.get("resolution"))
    return MediaCandidate(
        media_type=media_type,  # type: ignore[arg-type]
        _urls=(url,),
        width=width,
        height=height,
        codec=None,
        container=extension,
        quality_label=_safe_text(value.get("quality")),
        audio_state="unknown" if media_type == "video" else "present",
        candidate_id=_safe_identifier(value.get("id")),
    )


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
