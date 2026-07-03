from __future__ import annotations

import re
from typing import Any

import requests

from app.config import Settings
from app.social.models import SocialResolverError, SocialVideoInfo
from app.social.thumbnail_selection import platform_thumbnail

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def resolve_with_cobalt(
    source_url: str,
    platform: str,
    settings: Settings,
) -> SocialVideoInfo:
    enabled = set(settings.cobalt_enabled_platforms)
    if not settings.enable_cobalt_resolver:
        raise SocialResolverError(
            "social_resolver_unavailable",
            "Cobalt resolver is disabled.",
            {"platform": platform, "resolver": "cobalt"},
        )
    if not settings.cobalt_api_url:
        raise SocialResolverError(
            "social_resolver_unavailable",
            "Cobalt resolver is not configured.",
            {"platform": platform, "resolver": "cobalt"},
        )
    if platform in {"youtube", "tiktok"} or platform not in enabled:
        raise SocialResolverError(
            "unsupported_social_platform",
            "Cobalt is not enabled for this platform.",
            {"platform": platform, "resolver": "cobalt"},
        )

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if settings.cobalt_api_key is not None:
        headers["Authorization"] = (
            f"Api-Key {settings.cobalt_api_key.get_secret_value()}"
        )

    try:
        response = requests.post(
            cobalt_endpoint(settings),
            headers=headers,
            json={
                "url": source_url,
                "videoQuality": settings.cobalt_video_quality,
                "downloadMode": "auto",
                "filenameStyle": "basic",
                "alwaysProxy": settings.cobalt_always_proxy,
                "localProcessing": "disabled",
                "disableMetadata": True,
            },
            timeout=settings.cobalt_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise SocialResolverError(
            "cobalt_resolver_failed",
            sanitize_message(str(exc)) or "Cobalt resolver request failed.",
            {"platform": platform, "resolver": "cobalt"},
        ) from exc

    if response.status_code >= 400:
        raise SocialResolverError(
            "cobalt_resolver_failed",
            f"Cobalt resolver returned HTTP {response.status_code}.",
            {
                "platform": platform,
                "resolver": "cobalt",
                "status_code": response.status_code,
            },
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise SocialResolverError(
            "cobalt_resolver_failed",
            "Cobalt returned invalid JSON.",
            {"platform": platform, "resolver": "cobalt"},
        ) from exc

    if not isinstance(payload, dict):
        raise SocialResolverError(
            "cobalt_resolver_failed",
            "Cobalt returned an invalid response.",
            {"platform": platform, "resolver": "cobalt"},
        )

    status = safe_str(payload.get("status"))
    if status in {"tunnel", "redirect"}:
        stream_url = safe_http_url(payload.get("url"))
        if not stream_url:
            raise SocialResolverError(
                "cobalt_resolver_failed",
                "Cobalt response did not include a media stream.",
                {"platform": platform, "resolver": "cobalt", "status": status},
            )
        filename = safe_str(payload.get("filename"))
        ext = "mp4" if filename and filename.lower().endswith(".mp4") else None
        thumbnail = platform_thumbnail(
            payload.get("thumbnail"),
            source="existing_platform_thumbnail",
            provider=platform,
        )
        return SocialVideoInfo(
            platform=platform,
            resolver="cobalt",
            title=filename,
            video_id=None,
            duration_seconds=None,
            thumbnail_url=thumbnail.url,
            webpage_url=source_url,
            stream_url=stream_url,
            stream_headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
            ext=ext,
            mime_type="video/mp4",
            extractor="cobalt",
            filename=filename,
            uploader=None,
            filesize_approx=None,
            thumbnail_source=thumbnail.source,
            thumbnail_provider=thumbnail.provider,
            thumbnail_strategy=thumbnail.strategy,
            thumbnail_candidate_count=thumbnail.candidate_count,
            thumbnail_selected=thumbnail.selected,
        )
    if status == "picker":
        raise SocialResolverError(
            "cobalt_picker_not_supported",
            "Cobalt returned multiple media choices, which is not supported yet.",
            {"platform": platform, "resolver": "cobalt"},
        )
    if status == "local-processing":
        raise SocialResolverError(
            "cobalt_local_processing_not_supported",
            "Cobalt local processing is not supported by this worker.",
            {"platform": platform, "resolver": "cobalt"},
        )
    if status == "error":
        error_code = safe_str(payload.get("error", {}).get("code")) if isinstance(payload.get("error"), dict) else None
        raise SocialResolverError(
            "cobalt_resolver_failed",
            "Cobalt could not resolve this social video.",
            {
                "platform": platform,
                "resolver": "cobalt",
                "cobalt_error_code": sanitize_message(error_code),
            },
        )
    raise SocialResolverError(
        "cobalt_resolver_failed",
        "Cobalt returned an unsupported response.",
        {"platform": platform, "resolver": "cobalt", "status": status or "unknown"},
    )


def cobalt_endpoint(settings: Settings) -> str:
    return f"{str(settings.cobalt_api_url).rstrip('/')}/"


def safe_http_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def safe_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def sanitize_message(message: str | None) -> str:
    cleaned = re.sub(r"https?://\S+", "[url]", message or "")
    return " ".join(cleaned.split())[:300]
