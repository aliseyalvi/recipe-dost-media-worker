from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from app.config import Settings

TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "vt.tiktok.com",
    "vm.tiktok.com",
    "m.tiktok.com",
}


class TikWMResolverError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TikWMInfo:
    platform: str
    resolver: str
    video_id: str | None
    title: str | None
    thumbnail_url: str | None
    duration_seconds: float | None
    stream_url: str
    stream_headers: dict[str, str]
    ext: str
    mime_type: str


def resolve_tikwm_metadata(source_url: str, settings: Settings) -> TikWMInfo:
    if not settings.enable_tikwm_resolver:
        raise TikWMResolverError(
            "tikwm_disabled",
            "TikWM resolver is disabled.",
        )
    if not is_tiktok_url(source_url):
        raise TikWMResolverError(
            "invalid_tiktok_url",
            "Only public or authorized TikTok URLs are supported.",
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
        raise TikWMResolverError(
            "tikwm_api_failed",
            sanitize_message(str(exc)) or "TikWM metadata request failed.",
        ) from exc

    if response.status_code != 200:
        raise TikWMResolverError(
            "tikwm_api_http_error",
            f"TikWM metadata request returned HTTP {response.status_code}.",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise TikWMResolverError(
            "tikwm_invalid_response",
            "TikWM returned invalid JSON.",
        ) from exc

    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise TikWMResolverError(
            "tikwm_api_failed",
            "TikWM did not return a successful metadata response.",
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        raise TikWMResolverError(
            "tikwm_invalid_response",
            "TikWM response did not include video metadata.",
        )

    stream_url = safe_http_url(data.get("hdplay")) or safe_http_url(data.get("play"))
    if not stream_url:
        raise TikWMResolverError(
            "tikwm_stream_url_missing",
            "TikWM response did not include a playable video stream.",
        )

    return TikWMInfo(
        platform="tiktok",
        resolver="tikwm",
        video_id=safe_str(data.get("id")),
        title=safe_str(data.get("title")),
        thumbnail_url=safe_http_url(data.get("cover")),
        duration_seconds=safe_float(data.get("duration")),
        stream_url=stream_url,
        stream_headers={
            "User-Agent": settings.tikwm_user_agent,
            "Referer": "https://www.tiktok.com/",
        },
        ext="mp4",
        mime_type="video/mp4",
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
                raise TikWMResolverError(
                    "tikwm_stream_forbidden",
                    "TikWM stream could not be accessed.",
                )
            if response.status_code == 404:
                raise TikWMResolverError(
                    "tikwm_stream_expired",
                    "TikWM stream expired before probing.",
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
    except TikWMResolverError:
        raise
    except requests.RequestException as exc:
        raise TikWMResolverError(
            "tikwm_stream_probe_failed",
            sanitize_message(str(exc)) or "TikWM stream probe failed.",
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
    except TikWMResolverError as exc:
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


def sanitize_message(message: str | None) -> str:
    cleaned = re.sub(r"https?://\S+", "[url]", message or "")
    return " ".join(cleaned.split())[:300]
