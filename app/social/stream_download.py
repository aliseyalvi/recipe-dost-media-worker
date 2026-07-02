from __future__ import annotations

import base64
import io
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from app.config import Settings
from app.social.models import SocialResolverError, SocialVideoInfo
from app.social.thumbnail_selection import platform_thumbnail, select_ytdlp_thumbnail
from app.social.yt_dlp_resolver import (
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
    cookie_file, cookie_setup_failed = get_platform_cookie_file(
        normalized_platform,
        settings,
    )
    use_cookie_first = normalized_platform == "instagram" and bool(cookie_file)
    opts = ytdlp_options(
        normalized_platform,
        settings,
        cookie_file if use_cookie_first else None,
    )

    no_cookie_result = try_ytdlp_extract(
        url,
        normalized_platform,
        job_id,
        settings,
        opts,
        cookie_mode="platform_cookie_file" if use_cookie_first else "none",
        resolver="ytdlp_stream_cookies" if use_cookie_first else "ytdlp_stream",
        output_mode=output_mode,
    )
    if use_cookie_first:
        no_cookie_result["cookie_retry_attempted"] = True
        no_cookie_result["cookie_setup_failed"] = cookie_setup_failed
    if no_cookie_result.get("success"):
        return no_cookie_result

    if cookie_file and not use_cookie_first:
        cookie_opts = ytdlp_options(normalized_platform, settings, cookie_file)
        cookie_result = try_ytdlp_extract(
            url,
            normalized_platform,
            job_id,
            settings,
            cookie_opts,
            cookie_mode="platform_cookie_file",
            resolver="ytdlp_stream_cookies",
            output_mode=output_mode,
        )
        cookie_result["cookie_retry_attempted"] = True
        cookie_result["cookie_setup_failed"] = cookie_setup_failed
        if cookie_result.get("success"):
            return cookie_result
        no_cookie_result = cookie_result
    else:
        no_cookie_result["cookie_setup_failed"] = cookie_setup_failed

    if should_use_metadata_fallback(settings):
        fallback_result = extract_opengraph_metadata(url, normalized_platform, settings)
        fallback_result["cookie_setup_failed"] = cookie_setup_failed
        fallback_result["cookie_retry_attempted"] = bool(cookie_file)
        if fallback_result.get("success"):
            return fallback_result
        return fallback_result

    return no_cookie_result


def try_ytdlp_extract(
    url: str,
    platform: str,
    job_id: str,
    settings: Settings,
    opts: dict[str, Any],
    *,
    cookie_mode: str,
    resolver: str,
    output_mode: str,
) -> dict[str, object]:
    from yt_dlp import YoutubeDL

    try:
        with YoutubeDL({**opts, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return extractor_failure(
            platform,
            "unified_extractor_failed",
            "This social video could not be accessed.",
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode=output_mode,
        )
    if not isinstance(info, dict):
        return extractor_failure(
            platform,
            "unified_extractor_failed",
            "This social video could not be accessed.",
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode=output_mode,
        )
    selected_format = select_optional_direct_video_format(info, settings)
    duration_seconds = safe_duration_seconds(info.get("duration"))
    if (
        duration_seconds is not None
        and duration_seconds > settings.max_video_duration_seconds
    ):
        return extractor_result(
            platform,
            info,
            selected_format,
            success=False,
            error_code="video_duration_exceeded",
            safe_error_message="This video is too long for automatic recipe analysis.",
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode=output_mode,
        )

    if output_mode == "memory_buffer":
        return extract_to_memory(
            url,
            platform,
            job_id,
            settings,
            opts,
            info,
            selected_format,
            resolver=resolver,
            cookie_mode=cookie_mode,
        )
    return extract_to_temp_file(
        url,
        platform,
        job_id,
        settings,
        opts,
        info,
        selected_format,
        resolver=resolver,
        cookie_mode=cookie_mode,
    )


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
    if settings.ytdlp_user_agent:
        opts["http_headers"] = {
            **platform_headers(platform),
            "User-Agent": settings.ytdlp_user_agent,
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
    *,
    resolver: str,
    cookie_mode: str,
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
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode="temp_file",
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
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode="temp_file",
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
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode="temp_file",
        )
    return extractor_result(
        platform,
        info,
        selected_format,
        success=True,
        temp_file_path=str(file_path),
        size_bytes=size_bytes,
        resolver=resolver,
        cookie_mode=cookie_mode,
        output_mode="temp_file",
    )


def extract_to_memory(
    url: str,
    platform: str,
    job_id: str,
    settings: Settings,
    opts: dict[str, Any],
    info: dict[str, Any],
    selected_format: dict[str, Any] | None,
    *,
    resolver: str,
    cookie_mode: str,
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
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode="memory_buffer",
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
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode="memory_buffer",
        )
    writer.buffer.seek(0)
    return extractor_result(
        platform,
        info,
        selected_format,
        success=True,
        video_buffer=writer.buffer,
        size_bytes=size_bytes,
        resolver=resolver,
        cookie_mode=cookie_mode,
        output_mode="memory_buffer",
    )


def extractor_failure(
    platform: str,
    error_code: str,
    message: str,
    *,
    resolver: str = "ytdlp_stream",
    cookie_mode: str | None = None,
    output_mode: str | None = None,
) -> dict[str, object]:
    return {
        "success": False,
        "platform": platform,
        "resolver": resolver,
        "title": None,
        "description": None,
        "author_username": None,
        "thumbnail_url": None,
        "thumbnail_source": "fallback",
        "thumbnail_selected": False,
        "duration_seconds": None,
        "cdn_download_url": None,
        "video_buffer": None,
        "temp_file_path": None,
        "size_bytes": None,
        "mime_type": None,
        "width": None,
        "height": None,
        "raw_metadata": None,
        "resolution_mode": "metadata_only",
        "video_bytes_verified": False,
        "output_mode": output_mode,
        "cookie_mode": cookie_mode,
        "temp_file_exists": False,
        "cookie_retry_attempted": False,
        "cookie_setup_failed": False,
        "metadata_fallback_attempted": False,
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
    resolver: str = "ytdlp_stream",
    cookie_mode: str | None = None,
    output_mode: str | None = None,
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
    has_local_media = bool(temp_file_path or video_buffer)
    thumbnail = select_ytdlp_thumbnail(info)
    return {
        "success": success,
        "platform": platform,
        "resolver": resolver,
        "title": safe_str(info.get("title")),
        "description": safe_str(info.get("description")),
        "author_username": safe_str(info.get("uploader") or info.get("channel")),
        "thumbnail_url": thumbnail.url,
        "thumbnail_source": thumbnail.source,
        "thumbnail_selected": thumbnail.selected,
        "duration_seconds": safe_duration_seconds(info.get("duration")),
        "cdn_download_url": cdn_url,
        "video_buffer": video_buffer,
        "temp_file_path": temp_file_path,
        "size_bytes": filesize,
        "mime_type": safe_str(selected.get("mime_type") or info.get("mime_type")),
        "width": safe_int(selected.get("width") or info.get("width")),
        "height": safe_int(selected.get("height") or info.get("height")),
        "raw_metadata": sanitized_ytdlp_metadata(info, selected_format),
        "resolution_mode": "local_file_ready" if has_local_media else "metadata_only",
        "video_bytes_verified": success and has_local_media,
        "output_mode": output_mode,
        "cookie_mode": cookie_mode or "none",
        "temp_file_exists": bool(temp_file_path and Path(temp_file_path).exists()),
        "cookie_retry_attempted": cookie_mode == "platform_cookie_file",
        "cookie_setup_failed": False,
        "metadata_fallback_attempted": False,
        "error_code": error_code,
        "safe_error_message": safe_error_message,
    }


def extract_opengraph_metadata(
    url: str,
    platform: str,
    settings: Settings,
) -> dict[str, object]:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": settings.ytdlp_user_agent or platform_headers(platform)["User-Agent"]},
            timeout=(10, 20),
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.RequestException:
        return metadata_fallback_failure(platform)
    tags = parse_open_graph_tags(response.text[:512 * 1024])
    title = tags.get("og:title") or tags.get("twitter:title")
    description = tags.get("og:description") or tags.get("twitter:description")
    image = (
        platform_thumbnail(tags.get("og:image")).url
        or platform_thumbnail(tags.get("twitter:image")).url
    )
    if not title and not image:
        return metadata_fallback_failure(platform)
    return {
        "success": True,
        "platform": platform,
        "resolver": f"{platform}_opengraph",
        "title": title,
        "description": description,
        "author_username": None,
        "thumbnail_url": image,
        "thumbnail_source": "platform_thumbnail" if image else "fallback",
        "thumbnail_selected": bool(image),
        "duration_seconds": None,
        "cdn_download_url": None,
        "video_buffer": None,
        "temp_file_path": None,
        "size_bytes": None,
        "mime_type": None,
        "width": None,
        "height": None,
        "raw_metadata": {
            "title_present": bool(title),
            "description_present": bool(description),
            "thumbnail_present": bool(image),
            "og_video_present": bool(safe_http_url(tags.get("og:video"))),
        },
        "resolution_mode": "metadata_only",
        "video_bytes_verified": False,
        "output_mode": None,
        "cookie_mode": "none",
        "temp_file_exists": False,
        "cookie_retry_attempted": False,
        "cookie_setup_failed": False,
        "metadata_fallback_attempted": True,
        "error_code": None,
        "safe_error_message": None,
    }


def metadata_fallback_failure(platform: str) -> dict[str, object]:
    return {
        **extractor_failure(
            platform,
            f"{platform}_metadata_unavailable",
            "This social video could not be accessed publicly.",
            resolver=f"{platform}_opengraph",
            cookie_mode="none",
            output_mode=None,
        ),
        "metadata_fallback_attempted": True,
    }


def parse_open_graph_tags(html: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    key_first = re.compile(
        r"<meta\s+[^>]*(?:property|name)=[\"']([^\"']+)[\"'][^>]*content=[\"']([^\"']*)[\"'][^>]*>",
        re.IGNORECASE,
    )
    content_first = re.compile(
        r"<meta\s+[^>]*content=[\"']([^\"']*)[\"'][^>]*(?:property|name)=[\"']([^\"']+)[\"'][^>]*>",
        re.IGNORECASE,
    )
    for key, value in key_first.findall(html):
        normalized = key.strip().lower()
        if normalized.startswith(("og:", "twitter:")) and normalized not in tags:
            tags[normalized] = unescape_html(value.strip())
    for value, key in content_first.findall(html):
        normalized = key.strip().lower()
        if normalized.startswith(("og:", "twitter:")) and normalized not in tags:
            tags[normalized] = unescape_html(value.strip())
    return tags


def unescape_html(value: str) -> str:
    return (
        value.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def should_use_metadata_fallback(settings: Settings) -> bool:
    return (
        settings.ai_analysis_mode == "mock_fixture"
        and settings.enable_metadata_only_mock_fallback
        and settings.enable_og_metadata_fallback
        and settings.allow_mock_completion_without_video
    )


def get_platform_cookie_file(
    platform: str,
    settings: Settings,
) -> tuple[str | None, bool]:
    if not platform_cookie_enabled(platform, settings):
        return None, False
    configured_file = platform_cookie_file(platform, settings)
    if configured_file and Path(configured_file).exists():
        return configured_file, False
    encoded = platform_cookie_b64(platform, settings)
    if encoded is None:
        return None, False
    try:
        decoded = base64.b64decode(encoded.get_secret_value(), validate=True)
        cookie_dir = Path("/tmp/recipe-dost-cookies")
        cookie_dir.mkdir(parents=True, exist_ok=True)
        file_path = cookie_dir / f"{platform}-cookies.txt"
        file_path.write_bytes(decoded)
        try:
            os.chmod(file_path, 0o600)
        except OSError:
            pass
        return str(file_path), False
    except Exception:
        return None, True


def platform_cookie_enabled(platform: str, settings: Settings) -> bool:
    legacy_file_enabled = settings.enable_cookie_file and bool(
        platform_legacy_cookie_file(platform, settings)
    )
    explicit_enabled = settings.enable_ytdlp_cookies and {
        "instagram": settings.enable_instagram_cookies,
        "youtube": settings.enable_youtube_cookies,
        "facebook": settings.enable_facebook_cookies,
        "tiktok": settings.enable_tiktok_cookies,
    }.get(platform, False)
    return legacy_file_enabled or explicit_enabled


def platform_cookie_file(platform: str, settings: Settings) -> str | None:
    explicit = {
        "instagram": settings.instagram_cookies_file,
        "youtube": settings.youtube_cookies_file,
        "facebook": settings.facebook_cookies_file,
        "tiktok": settings.tiktok_cookies_file,
    }.get(platform)
    return explicit or platform_legacy_cookie_file(platform, settings)


def platform_legacy_cookie_file(platform: str, settings: Settings) -> str | None:
    return {
        "instagram": settings.instagram_cookie_file,
        "facebook": settings.facebook_cookie_file,
        "tiktok": settings.tiktok_cookie_file,
    }.get(platform)


def platform_cookie_b64(platform: str, settings: Settings):
    return {
        "instagram": settings.instagram_cookies_b64,
        "youtube": settings.youtube_cookies_b64,
        "facebook": settings.facebook_cookies_b64,
        "tiktok": settings.tiktok_cookies_b64,
    }.get(platform)


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
