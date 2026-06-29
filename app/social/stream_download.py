from __future__ import annotations

import io
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from app.config import Settings
from app.social.models import SocialResolverError, SocialVideoInfo
from app.social.yt_dlp_resolver import (
    get_cookie_file_for_platform,
    platform_headers,
    safe_float,
    safe_http_url,
    safe_str,
    sanitized_ytdlp_metadata,
    select_optional_direct_video_format,
)


@dataclass
class VideoBuffer:
    buffer: io.BytesIO
    size_bytes: int
    mime_type: str
    ext: str | None = None


class InMemoryVideoWriter:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        self.encoding = "utf-8"

    def write(self, value: str) -> int:
        encoded = value.encode("utf-8")
        self.buffer.write(encoded)
        return len(value)

    def flush(self) -> None:
        return None


def extract_social_video(
    url: str,
    platform: str,
    job_id: str,
    settings: Settings,
) -> dict[str, object]:
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return extractor_failure(
            platform,
            "unified_extractor_failed",
            "Social video extraction is not available.",
        )

    normalized_platform = platform.strip().lower() or "unknown"
    output_mode = settings.media_extract_output_mode
    cookie_file = get_cookie_file_for_platform(normalized_platform, settings)
    opts = ytdlp_options(normalized_platform, settings, cookie_file)

    try:
        with YoutubeDL({**opts, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return extractor_failure(
            normalized_platform,
            "unified_extractor_failed",
            "This social video could not be accessed.",
        )
    if not isinstance(info, dict):
        return extractor_failure(
            normalized_platform,
            "unified_extractor_failed",
            "This social video could not be accessed.",
        )

    selected_format = select_optional_direct_video_format(info, settings)
    duration_seconds = safe_duration_seconds(info.get("duration"))
    if (
        duration_seconds is not None
        and duration_seconds > settings.max_video_duration_seconds
    ):
        return extractor_result(
            normalized_platform,
            info,
            selected_format,
            success=False,
            error_code="video_duration_exceeded",
            safe_error_message="This video is too long for automatic recipe analysis.",
        )

    if output_mode == "memory_buffer":
        return extract_to_memory(url, normalized_platform, job_id, settings, opts, info, selected_format)
    return extract_to_temp_file(url, normalized_platform, job_id, settings, opts, info, selected_format)


def ytdlp_options(
    platform: str,
    settings: Settings,
    cookie_file: str | None,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "format": "best[ext=mp4][height<=720]/best[height<=720]/best",
        "noplaylist": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 15,
        "quiet": True,
        "no_warnings": True,
        "logtostderr": True,
        "http_headers": platform_headers(platform),
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts


def extract_to_temp_file(
    url: str,
    platform: str,
    job_id: str,
    settings: Settings,
    opts: dict[str, Any],
    info: dict[str, Any],
    selected_format: dict[str, Any] | None,
) -> dict[str, object]:
    from yt_dlp import YoutubeDL

    job_dir = Path(settings.temp_media_dir) / safe_path_segment(job_id)
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    job_dir.mkdir(parents=True, exist_ok=True)
    download_opts = {
        **opts,
        "outtmpl": str(job_dir / "video.%(ext)s"),
    }
    try:
        with YoutubeDL(download_opts) as ydl:
            ydl.download([url])
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        return extractor_result(
            platform,
            info,
            selected_format,
            success=False,
            error_code="unified_extractor_failed",
            safe_error_message="This social video could not be downloaded.",
        )

    file_path = locate_downloaded_file(job_dir)
    if file_path is None:
        shutil.rmtree(job_dir, ignore_errors=True)
        return extractor_result(
            platform,
            info,
            selected_format,
            success=False,
            error_code="unified_extractor_failed",
            safe_error_message="This social video could not be downloaded.",
        )
    size_bytes = file_path.stat().st_size
    max_bytes = settings.max_video_size_mb * 1024 * 1024
    if size_bytes > max_bytes:
        shutil.rmtree(job_dir, ignore_errors=True)
        return extractor_result(
            platform,
            info,
            selected_format,
            success=False,
            error_code="media_size_exceeded",
            safe_error_message="This video is too large for automatic recipe analysis.",
            size_bytes=size_bytes,
        )
    return extractor_result(
        platform,
        info,
        selected_format,
        success=True,
        temp_file_path=str(file_path),
        size_bytes=size_bytes,
    )


def extract_to_memory(
    url: str,
    platform: str,
    job_id: str,
    settings: Settings,
    opts: dict[str, Any],
    info: dict[str, Any],
    selected_format: dict[str, Any] | None,
) -> dict[str, object]:
    from yt_dlp import YoutubeDL

    writer = InMemoryVideoWriter()
    original_stdout = sys.stdout
    max_bytes = settings.max_video_size_mb * 1024 * 1024
    try:
        sys.stdout = writer  # type: ignore[assignment]
        download_opts = {
            **opts,
            "outtmpl": "-",
        }
        with YoutubeDL(download_opts) as ydl:
            ydl.download([url])
    except Exception:
        writer.buffer.close()
        return extractor_result(
            platform,
            info,
            selected_format,
            success=False,
            error_code="unified_extractor_failed",
            safe_error_message="This social video could not be downloaded.",
        )
    finally:
        sys.stdout = original_stdout
    size_bytes = writer.buffer.getbuffer().nbytes
    if size_bytes > max_bytes:
        writer.buffer.close()
        return extractor_result(
            platform,
            info,
            selected_format,
            success=False,
            error_code="media_size_exceeded",
            safe_error_message="This video is too large for automatic recipe analysis.",
            size_bytes=size_bytes,
        )
    writer.buffer.seek(0)
    return extractor_result(
        platform,
        info,
        selected_format,
        success=True,
        video_buffer=writer.buffer,
        size_bytes=size_bytes,
    )


def extractor_failure(
    platform: str,
    error_code: str,
    message: str,
) -> dict[str, object]:
    return {
        "success": False,
        "platform": platform,
        "resolver": "ytdlp_stream",
        "title": None,
        "description": None,
        "author_username": None,
        "thumbnail_url": None,
        "duration_seconds": None,
        "cdn_download_url": None,
        "video_buffer": None,
        "temp_file_path": None,
        "size_bytes": None,
        "mime_type": None,
        "width": None,
        "height": None,
        "raw_metadata": None,
        "error_code": error_code,
        "safe_error_message": message,
    }


def extractor_result(
    platform: str,
    info: dict[str, Any],
    selected_format: dict[str, Any] | None,
    *,
    success: bool,
    error_code: str | None = None,
    safe_error_message: str | None = None,
    video_buffer: io.BytesIO | None = None,
    temp_file_path: str | None = None,
    size_bytes: int | None = None,
) -> dict[str, object]:
    selected = selected_format or {}
    cdn_url = safe_http_url(selected.get("url")) or safe_http_url(info.get("url"))
    filesize = safe_int(
        size_bytes
        or selected.get("filesize")
        or selected.get("filesize_approx")
        or info.get("filesize")
        or info.get("filesize_approx")
    )
    return {
        "success": success,
        "platform": platform,
        "resolver": "ytdlp_stream",
        "title": safe_str(info.get("title")),
        "description": safe_str(info.get("description")),
        "author_username": safe_str(info.get("uploader") or info.get("channel")),
        "thumbnail_url": safe_http_url(info.get("thumbnail")),
        "duration_seconds": safe_duration_seconds(info.get("duration")),
        "cdn_download_url": cdn_url,
        "video_buffer": video_buffer,
        "temp_file_path": temp_file_path,
        "size_bytes": filesize,
        "mime_type": safe_str(selected.get("mime_type") or info.get("mime_type")),
        "width": safe_int(selected.get("width") or info.get("width")),
        "height": safe_int(selected.get("height") or info.get("height")),
        "raw_metadata": sanitized_ytdlp_metadata(info, selected_format),
        "error_code": error_code,
        "safe_error_message": safe_error_message,
    }


def locate_downloaded_file(job_dir: Path) -> Path | None:
    files = [path for path in job_dir.iterdir() if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_size)


def safe_path_segment(value: str) -> str:
    return "".join(char for char in value if char.isalnum() or char in {"-", "_"})[:80] or "job"


def safe_duration_seconds(value: object) -> int | None:
    parsed = safe_float(value)
    return int(parsed) if parsed is not None else None


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
