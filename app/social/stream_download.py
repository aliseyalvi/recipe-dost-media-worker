from __future__ import annotations

import io
from dataclasses import dataclass

import requests

from app.config import Settings
from app.social.yt_dlp_resolver import SocialPipelineError


@dataclass
class VideoBuffer:
    buffer: io.BytesIO
    size_bytes: int
    mime_type: str
    ext: str | None = None


def stream_video_to_memory(
    stream_url: str,
    stream_headers: dict[str, str],
    settings: Settings,
    *,
    ext: str | None = None,
) -> VideoBuffer:
    max_bytes = settings.max_memory_buffer_mb * 1024 * 1024
    request_headers = dict(stream_headers)
    buffer = io.BytesIO()
    total = 0
    mime_type = ""
    try:
        with requests.get(
            stream_url,
            stream=True,
            timeout=(10, 120),
            headers=request_headers,
        ) as response:
            if response.status_code in {401, 403}:
                raise SocialPipelineError(
                    "social_stream_forbidden",
                    "This social video stream could not be accessed.",
                    {"status_code": response.status_code},
                )
            if response.status_code == 404:
                raise SocialPipelineError(
                    "social_stream_expired",
                    "This social video stream expired before processing.",
                    {"status_code": response.status_code},
                )
            response.raise_for_status()
            mime_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if not is_supported_type(mime_type, ext):
                raise SocialPipelineError(
                    "media_unsupported_type",
                    "This media type is not supported.",
                    {"status_code": response.status_code, "mime_type": mime_type},
                )
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    response.close()
                    buffer.close()
                    raise SocialPipelineError(
                        "media_memory_limit_exceeded",
                        "This video is too large for automatic recipe analysis.",
                    )
                buffer.write(chunk)
    except SocialPipelineError:
        raise
    except Exception as exc:
        buffer.close()
        raise SocialPipelineError("media_download_failed", "This social video could not be downloaded for analysis.") from exc
    if total == 0:
        buffer.close()
        raise SocialPipelineError("media_download_failed", "This social video could not be downloaded for analysis.")
    buffer.seek(0)
    return VideoBuffer(buffer=buffer, size_bytes=total, mime_type=mime_type or "video/mp4", ext=ext)


def is_supported_type(mime_type: str, ext: str | None) -> bool:
    return mime_type.startswith("video/") or (mime_type == "application/octet-stream" and (ext or "").lower() in {"mp4", "mov", "webm", "m4v"})
