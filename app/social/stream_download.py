from __future__ import annotations

import io
from dataclasses import dataclass

import requests

from app.config import Settings
from app.social.models import SocialResolverError, SocialVideoInfo


@dataclass
class VideoBuffer:
    buffer: io.BytesIO
    size_bytes: int
    mime_type: str
    ext: str | None = None


def stream_video_to_memory(
    info: SocialVideoInfo,
    settings: Settings,
    max_size_mb: int | None = None,
) -> VideoBuffer:
    effective_max_size_mb = (
        min(max_size_mb, settings.max_memory_buffer_mb)
        if max_size_mb is not None
        else settings.max_memory_buffer_mb
    )
    max_bytes = effective_max_size_mb * 1024 * 1024
    request_headers = dict(info.stream_headers)
    buffer = io.BytesIO()
    total = 0
    mime_type = ""
    try:
        with requests.get(
            info.stream_url,
            stream=True,
            timeout=(10, 120),
            headers=request_headers,
            allow_redirects=True,
        ) as response:
            if response.status_code in {401, 403}:
                raise SocialResolverError(
                    "social_stream_forbidden",
                    "This social video stream could not be accessed.",
                    {
                        "platform": info.platform,
                        "resolver": info.resolver,
                        "status_code": response.status_code,
                    },
                )
            if response.status_code == 404:
                raise SocialResolverError(
                    "social_stream_expired",
                    "This social video stream expired before processing.",
                    {
                        "platform": info.platform,
                        "resolver": info.resolver,
                        "status_code": response.status_code,
                    },
                )
            response.raise_for_status()
            content_length = safe_int(response.headers.get("content-length"))
            if content_length is not None and content_length > max_bytes:
                buffer.close()
                raise SocialResolverError(
                    "media_size_exceeded",
                    "This video is too large for automatic recipe analysis.",
                    {
                        "platform": info.platform,
                        "resolver": info.resolver,
                        "size_bytes": content_length,
                    },
                )
            mime_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if not is_supported_type(mime_type, info.ext):
                raise SocialResolverError(
                    "media_unsupported_type",
                    "This media type is not supported.",
                    {
                        "platform": info.platform,
                        "resolver": info.resolver,
                        "status_code": response.status_code,
                        "mime_type": mime_type,
                    },
                )
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    response.close()
                    buffer.close()
                    raise SocialResolverError(
                        "media_memory_limit_exceeded",
                        "This video is too large for automatic recipe analysis.",
                        {
                            "platform": info.platform,
                            "resolver": info.resolver,
                            "size_bytes": total,
                        },
                    )
                buffer.write(chunk)
    except SocialResolverError:
        raise
    except Exception as exc:
        buffer.close()
        raise SocialResolverError(
            "media_download_failed",
            "This social video could not be downloaded for analysis.",
            {"platform": info.platform, "resolver": info.resolver},
        ) from exc
    if total == 0:
        buffer.close()
        raise SocialResolverError(
            "media_download_failed",
            "This social video could not be downloaded for analysis.",
            {"platform": info.platform, "resolver": info.resolver},
        )
    buffer.seek(0)
    return VideoBuffer(buffer=buffer, size_bytes=total, mime_type=mime_type or info.mime_type or "video/mp4", ext=info.ext)


def is_supported_type(mime_type: str, ext: str | None) -> bool:
    return mime_type.startswith("video/") or (mime_type == "application/octet-stream" and (ext or "").lower() in {"mp4", "mov", "webm", "m4v"})


def probe_video_stream(
    info: SocialVideoInfo,
    max_probe_bytes: int = 256 * 1024,
) -> dict[str, object]:
    try:
        with requests.get(
            info.stream_url,
            headers=info.stream_headers,
            stream=True,
            timeout=(10, 30),
            allow_redirects=True,
        ) as response:
            if response.status_code in {401, 403}:
                raise SocialResolverError(
                    "social_stream_forbidden",
                    "This social video stream could not be accessed.",
                    {
                        "platform": info.platform,
                        "resolver": info.resolver,
                        "status_code": response.status_code,
                    },
                )
            if response.status_code == 404:
                raise SocialResolverError(
                    "social_stream_expired",
                    "This social video stream expired before probing.",
                    {
                        "platform": info.platform,
                        "resolver": info.resolver,
                        "status_code": response.status_code,
                    },
                )
            response.raise_for_status()
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                remaining = max_probe_bytes - total
                if remaining <= 0:
                    break
                total += min(len(chunk), remaining)
                if total >= max_probe_bytes:
                    break
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
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
            "media_download_failed",
            "This social video stream could not be probed.",
            {"platform": info.platform, "resolver": info.resolver},
        ) from exc


def safe_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
