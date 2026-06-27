from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import requests

from app.config import Settings
from app.social.models import SocialResolverError, SocialVideoInfo

TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "vt.tiktok.com",
    "vm.tiktok.com",
    "m.tiktok.com",
}


def resolve_tikwm_metadata(source_url: str, settings: Settings) -> SocialVideoInfo:
    return resolve_with_tikwm(source_url, "tiktok", settings)


def resolve_with_tikwm(
    source_url: str,
    platform: str,
    settings: Settings,
) -> SocialVideoInfo:
    if not settings.enable_tikwm_resolver:
        raise SocialResolverError(
            "tikwm_disabled",
            "TikWM resolver is disabled.",
            {"platform": platform, "resolver": "tikwm"},
        )
    if platform != "tiktok" or platform not in settings.tikwm_enabled_platforms:
        raise SocialResolverError(
            "unsupported_social_platform",
            "TikWM is only enabled for TikTok.",
            {"platform": platform, "resolver": "tikwm"},
        )
    if not is_tiktok_url(source_url):
        raise SocialResolverError(
            "invalid_tiktok_url",
            "Only public or authorized TikTok URLs are supported.",
            {"platform": platform, "resolver": "tikwm"},
        )

    try:
        response = requests.post(
            settings.tikwm_api_url,
            headers={
                "User-Agent": settings.tikwm_user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"url": source_url, "hd": "1" if settings.tikwm_hd else "0"},
            timeout=settings.tikwm_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise SocialResolverError(
            "tikwm_api_failed",
            sanitize_message(str(exc)) or "TikWM metadata request failed.",
            {"platform": platform, "resolver": "tikwm"},
        ) from exc

    if response.status_code != 200:
        raise SocialResolverError(
            "tikwm_api_http_error",
            f"TikWM metadata request returned HTTP {response.status_code}.",
            {
                "platform": platform,
                "resolver": "tikwm",
                "status_code": response.status_code,
            },
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise SocialResolverError(
            "tikwm_invalid_response",
            "TikWM returned invalid JSON.",
            {"platform": platform, "resolver": "tikwm"},
        ) from exc

    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise SocialResolverError(
            "tikwm_api_failed",
            "TikWM did not return a successful metadata response.",
            {"platform": platform, "resolver": "tikwm"},
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        raise SocialResolverError(
            "tikwm_invalid_response",
            "TikWM response did not include video metadata.",
            {"platform": platform, "resolver": "tikwm"},
        )

    stream_url = safe_http_url(data.get("hdplay")) or safe_http_url(data.get("play"))
    if not stream_url:
        raise SocialResolverError(
            "tikwm_stream_url_missing",
            "TikWM response did not include a playable video stream.",
            {"platform": platform, "resolver": "tikwm"},
        )
    filesize = safe_int(data.get("size") or data.get("hd_size"))
    max_bytes = settings.tikwm_max_video_size_mb * 1024 * 1024
    if filesize is not None and filesize > max_bytes:
        raise SocialResolverError(
            "media_memory_limit_exceeded",
            "This video is too large for automatic recipe analysis.",
            {
                "platform": platform,
                "resolver": "tikwm",
                "filesize_approx": filesize,
                "max_size_mb": settings.tikwm_max_video_size_mb,
            },
        )

    return SocialVideoInfo(
        platform="tiktok",
        resolver="tikwm",
        title=safe_str(data.get("title")),
        video_id=safe_str(data.get("id")),
        duration_seconds=safe_float(data.get("duration")),
        thumbnail_url=safe_http_url(data.get("cover")),
        webpage_url=source_url,
        stream_url=stream_url,
        stream_headers={
            "User-Agent": settings.tikwm_user_agent,
            "Referer": "https://www.tiktok.com/",
        },
        ext="mp4",
        mime_type="video/mp4",
        extractor="tikwm",
        filename=None,
        uploader=safe_str(data.get("author", {}).get("unique_id"))
        if isinstance(data.get("author"), dict)
        else None,
        filesize_approx=filesize,
        raw_metadata=data,
        description=safe_str(data.get("title")),
    )


def probe_tikwm_stream(
    stream_url: str,
    stream_headers: dict[str, str],
    settings: Settings,
) -> dict[str, object]:
    max_bytes = max(0, settings.tikwm_probe_bytes)
    try:
        with requests.get(
            stream_url,
            headers=stream_headers,
            stream=True,
            timeout=(10, 30),
        ) as response:
            if response.status_code in {401, 403}:
                raise SocialResolverError(
                    "tikwm_stream_forbidden",
                    "TikWM stream could not be accessed.",
                    {"resolver": "tikwm", "status_code": response.status_code},
                )
            if response.status_code == 404:
                raise SocialResolverError(
                    "tikwm_stream_expired",
                    "TikWM stream expired before probing.",
                    {"resolver": "tikwm", "status_code": response.status_code},
                )
            response.raise_for_status()
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                remaining = max_bytes - total
                if remaining <= 0:
                    break
                total += min(len(chunk), remaining)
                if total >= max_bytes:
                    break
            content_type = response.headers.get("content-type", "").split(";", 1)[
                0
            ].lower()
            return {
                "status_code": response.status_code,
                "content_type": content_type,
                "bytes_read": total,
                "accepted": total > 0 and response.status_code == 200,
            }
    except SocialResolverError:
        raise
    except requests.RequestException as exc:
        raise SocialResolverError(
            "tikwm_stream_probe_failed",
            sanitize_message(str(exc)) or "TikWM stream probe failed.",
            {"resolver": "tikwm"},
        ) from exc


def debug_tikwm(
    source_url: str,
    settings: Settings,
    *,
    test_stream: bool = False,
) -> dict[str, object]:
    try:
        info = resolve_tikwm_metadata(source_url, settings)
        response: dict[str, object] = {
            "ok": True,
            "platform": info.platform,
            "resolver": info.resolver,
            "has_video_url": True,
            "has_thumbnail": bool(info.thumbnail_url),
            "title_present": bool(info.title),
            "video_id_present": bool(info.video_id),
            "duration_seconds": info.duration_seconds,
            "selected_ext": info.ext,
        }
        if test_stream:
            response["stream_probe"] = probe_tikwm_stream(
                info.stream_url,
                info.stream_headers,
                settings,
            )
        return response
    except SocialResolverError as exc:
        return {
            "ok": False,
            "platform": "tiktok",
            "resolver": "tikwm",
            "error_code": exc.code,
            "message": exc.message,
        }


def is_tiktok_url(source_url: str) -> bool:
    try:
        parsed = urlparse(source_url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme in {"http", "https"}
        and parsed.username is None
        and parsed.password is None
        and host in TIKTOK_HOSTS
    )


def safe_http_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def safe_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def sanitize_message(message: str | None) -> str:
    cleaned = re.sub(r"https?://\S+", "[url]", message or "")
    return " ".join(cleaned.split())[:300]
