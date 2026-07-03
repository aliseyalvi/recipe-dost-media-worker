from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

from app.config import Settings
from app.social.models import SocialResolverError, SocialVideoInfo
from app.social.platforms import detect_social_platform
from app.social.thumbnail_selection import select_ytdlp_thumbnail

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class SocialPipelineError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.metadata = metadata or {}


def resolve_social_video(
    url: str,
    settings: Settings,
    on_strategy_failed: Any | None = None,
) -> SocialVideoInfo:
    if settings.ytdlp_use_subprocess:
        return resolve_social_video_with_subprocess(url, settings, on_strategy_failed)
    return resolve_social_video_with_python_api(url, settings)


def resolve_youtube_metadata(
    url: str,
    settings: Settings,
) -> SocialVideoInfo:
    if not settings.enable_render_youtube_resolver:
        raise SocialResolverError(
            "youtube_resolver_disabled",
            "YouTube processing is not enabled yet.",
            {"platform": "youtube", "resolver": settings.youtube_resolver},
        )
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise SocialResolverError(
            "youtube_resolver_failed",
            "YouTube metadata could not be resolved.",
            {"platform": "youtube", "resolver": "ytdlp"},
        ) from exc

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": settings.ytdlp_socket_timeout_seconds,
        "retries": settings.ytdlp_retries,
        "fragment_retries": settings.ytdlp_retries,
        "format": format_selector(settings),
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise SocialResolverError(
            "youtube_resolver_failed",
            "YouTube metadata could not be resolved.",
            {"platform": "youtube", "resolver": "ytdlp"},
        ) from exc
    if not isinstance(info, dict):
        raise SocialResolverError(
            "youtube_resolver_failed",
            "YouTube metadata could not be resolved.",
            {"platform": "youtube", "resolver": "ytdlp"},
        )

    selected_format = select_optional_direct_video_format(info, settings)
    thumbnail = select_ytdlp_thumbnail(info, "youtube")
    stream_url = safe_http_url(selected_format.get("url")) if selected_format else ""
    filesize = safe_int(
        (selected_format or {}).get("filesize")
        or (selected_format or {}).get("filesize_approx")
        or info.get("filesize")
        or info.get("filesize_approx")
    )
    return SocialVideoInfo(
        platform="youtube",
        resolver="ytdlp",
        title=safe_str(info.get("title")),
        video_id=safe_str(info.get("id")),
        duration_seconds=safe_float(info.get("duration")),
        thumbnail_url=thumbnail.url,
        webpage_url=safe_http_url(info.get("webpage_url")),
        stream_url=stream_url or "",
        stream_headers=platform_headers("youtube"),
        ext=safe_str((selected_format or {}).get("ext") or info.get("ext")),
        mime_type=safe_str((selected_format or {}).get("mime_type")),
        extractor=safe_str(info.get("extractor")),
        filename=safe_str((selected_format or {}).get("filename") or info.get("filename")),
        uploader=safe_str(info.get("uploader") or info.get("channel")),
        filesize_approx=filesize,
        raw_metadata=sanitized_ytdlp_metadata(info, selected_format),
        description=safe_str(info.get("description")),
        width=safe_int((selected_format or {}).get("width") or info.get("width")),
        height=safe_int((selected_format or {}).get("height") or info.get("height")),
        thumbnail_source=thumbnail.source,
        thumbnail_provider=thumbnail.provider,
        thumbnail_strategy=thumbnail.strategy,
        thumbnail_candidate_count=thumbnail.candidate_count,
        thumbnail_selected=thumbnail.selected,
    )


def resolve_social_video_with_subprocess(
    url: str,
    settings: Settings,
    on_strategy_failed: Any | None = None,
) -> SocialVideoInfo:
    platform = detect_social_platform(url)
    cookie_file = get_cookie_file_for_platform(platform, settings)
    strategies = [False, True] if settings.ytdlp_enable_impersonation else [False]
    last_error: SocialPipelineError | None = None
    deadline = time.monotonic() + settings.ytdlp_extract_timeout_seconds
    for use_impersonation in strategies:
        remaining = max(1, int(deadline - time.monotonic()))
        try:
            info = run_ytdlp_json(
                url,
                settings,
                platform,
                cookie_file,
                timeout_seconds=remaining,
                use_impersonation=use_impersonation,
            )
            return build_social_video_info(info, url, settings, cookie_file is not None)
        except SocialPipelineError as exc:
            last_error = exc
            if on_strategy_failed is not None and not use_impersonation:
                on_strategy_failed(
                    {
                        "platform": platform,
                        "resolver": "yt-dlp",
                        "strategy": "base",
                        "error_code": exc.code,
                        "sanitized_message": str(
                            exc.metadata.get("sanitized_message", "")
                        )[:300],
                    }
                )
            if not settings.ytdlp_enable_impersonation or use_impersonation:
                raise
    assert last_error is not None
    raise last_error


def run_ytdlp_json(
    url: str,
    settings: Settings,
    platform: str,
    cookie_file: str | None,
    *,
    timeout_seconds: int,
    use_impersonation: bool,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--no-download",
        "--no-playlist",
        "--no-warnings",
        "--force-ipv4",
        "--socket-timeout",
        str(settings.ytdlp_socket_timeout_seconds),
        "--retries",
        str(settings.ytdlp_retries),
        "--fragment-retries",
        str(settings.ytdlp_retries),
        "--extractor-retries",
        str(settings.ytdlp_retries),
        "--format",
        format_selector(settings),
        "--user-agent",
        USER_AGENT,
        "--referer",
        platform_referer(platform),
    ]
    if use_impersonation:
        command.extend(["--impersonate", "chrome"])
    if settings.ytdlp_force_generic:
        command.append("--force-generic-extractor")
    if cookie_file:
        command.extend(["--cookies", cookie_file])
    command.append(url)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise SocialPipelineError(
            "yt_dlp_extract_timeout",
            "This social video took too long to access.",
            {"sanitized_message": "yt-dlp subprocess timeout"},
        ) from exc
    if completed.returncode != 0:
        code = classify_ytdlp_error(completed.stderr)
        raise SocialPipelineError(
            code,
            error_message_for_code(code),
            {
                "sanitized_message": sanitize_message(completed.stderr),
                "exit_code": completed.returncode,
            },
        )
    try:
        info = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SocialPipelineError(
            "yt_dlp_extract_failed",
            "This social video could not be accessed.",
            {"sanitized_message": "yt-dlp returned invalid JSON"},
        ) from exc
    if not isinstance(info, dict):
        raise SocialPipelineError(
            "yt_dlp_extract_failed",
            "This social video could not be accessed.",
        )
    return info


def resolve_social_video_with_python_api(
    url: str,
    settings: Settings,
) -> SocialVideoInfo:
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise SocialPipelineError(
            "yt_dlp_extract_failed",
            "Social video extraction is not available.",
        ) from exc
    platform = detect_social_platform(url)
    cookie_file = get_cookie_file_for_platform(platform, settings)
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "format": format_selector(settings),
        "socket_timeout": settings.ytdlp_socket_timeout_seconds,
        "retries": settings.ytdlp_retries,
        "fragment_retries": settings.ytdlp_retries,
        "force_generic_extractor": settings.ytdlp_force_generic,
        "http_headers": platform_headers(platform),
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise SocialPipelineError(
            "yt_dlp_extract_failed",
            "This social video could not be accessed.",
            {"sanitized_message": sanitize_message(str(exc))},
        ) from exc
    if not isinstance(info, dict):
        raise SocialPipelineError(
            "yt_dlp_extract_failed",
            "This social video could not be accessed.",
        )
    return build_social_video_info(info, url, settings, cookie_file is not None)


def build_social_video_info(
    info: dict[str, Any],
    url: str,
    settings: Settings,
    cookie_file_present: bool,
) -> SocialVideoInfo:
    selected_format = select_best_direct_video_format(info, settings)
    stream_url = safe_http_url(selected_format.get("url"))
    stream_headers = merge_safe_headers(
        platform_headers(detect_social_platform(str(info.get("webpage_url") or url))),
        info.get("http_headers"),
        selected_format.get("http_headers"),
    )
    if not stream_url:
        raise SocialPipelineError(
            "social_stream_url_missing",
            "A playable social video stream was not found.",
        )
    if is_hls_format(selected_format):
        raise SocialPipelineError(
            "social_hls_stream_not_supported",
            "This social video stream format is not supported yet.",
        )
    duration = safe_float(info.get("duration"))
    if duration is not None and duration > settings.max_video_duration_seconds:
        raise SocialPipelineError(
            "media_duration_too_long",
            "This video is too long for automatic recipe analysis.",
            {"duration_seconds": duration},
        )
    filesize = safe_int(
        selected_format.get("filesize")
        or selected_format.get("filesize_approx")
        or info.get("filesize")
        or info.get("filesize_approx")
    )
    thumbnail = select_ytdlp_thumbnail(info, detect_social_platform(str(info.get("webpage_url") or url)))
    return SocialVideoInfo(
        platform=detect_social_platform(str(info.get("webpage_url") or url)),
        resolver="yt-dlp",
        title=safe_str(info.get("title")),
        video_id=safe_str(info.get("id")),
        duration_seconds=duration,
        thumbnail_url=thumbnail.url,
        webpage_url=safe_http_url(info.get("webpage_url")),
        stream_url=stream_url,
        stream_headers=stream_headers,
        ext=safe_str(selected_format.get("ext") or info.get("ext")),
        mime_type=safe_str(selected_format.get("mime_type")),
        extractor=safe_str(info.get("extractor")),
        filename=safe_str(selected_format.get("filename") or info.get("filename")),
        uploader=safe_str(info.get("uploader")),
        filesize_approx=filesize,
        thumbnail_source=thumbnail.source,
        thumbnail_provider=thumbnail.provider,
        thumbnail_strategy=thumbnail.strategy,
        thumbnail_candidate_count=thumbnail.candidate_count,
        thumbnail_selected=thumbnail.selected,
    )


def get_cookie_file_for_platform(platform: str, settings: Settings) -> str | None:
    if not settings.enable_cookie_file:
        return None
    specific = platform_cookie_path(platform, settings)
    if specific and Path(specific).exists():
        return specific
    if settings.enable_generic_cookie_fallback and Path(settings.cookie_file_path).exists():
        return settings.cookie_file_path
    return None


def cookie_event_for_platform(
    platform: str,
    settings: Settings,
) -> tuple[str, dict[str, object]]:
    if get_cookie_file_for_platform(platform, settings):
        specific = platform_cookie_path(platform, settings)
        strategy = (
            "platform_specific"
            if specific and Path(specific).exists()
            else "generic_fallback"
        )
        return "social_cookie_file_detected", {
            "platform": platform,
            "cookie_file_present": True,
            "cookie_strategy": strategy,
        }
    if Path(settings.cookie_file_path).exists() and not settings.enable_generic_cookie_fallback:
        return "social_cookie_file_missing", {
            "platform": platform,
            "cookie_file_present": False,
            "reason": "generic_cookie_fallback_disabled",
            "cookie_strategy": "generic_fallback_disabled",
        }
    return "social_cookie_file_missing", {
        "platform": platform,
        "cookie_file_present": False,
        "cookie_strategy": "no_cookie_fallback_headers",
    }


def platform_cookie_path(platform: str, settings: Settings) -> str | None:
    return {
        "tiktok": settings.tiktok_cookie_file,
        "instagram": settings.instagram_cookie_file,
        "facebook": settings.facebook_cookie_file,
    }.get(platform)


def platform_headers(platform: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": platform_referer(platform),
    }


def platform_referer(platform: str) -> str:
    return {
        "tiktok": "https://www.tiktok.com/",
        "instagram": "https://www.instagram.com/",
        "facebook": "https://www.facebook.com/",
    }.get(platform, "https://www.google.com/")


def format_selector(settings: Settings) -> str:
    return (
        f"best[ext=mp4][height<={settings.max_video_height}]/"
        f"best[height<={settings.max_video_height}]/best"
    )


def select_best_direct_video_format(
    info_dict: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    max_bytes = settings.max_memory_buffer_mb * 1024 * 1024
    formats = collect_candidate_formats(info_dict)
    direct: list[dict[str, Any]] = []
    hls: list[dict[str, Any]] = []
    for item in formats:
        if not safe_http_url(item.get("url")):
            continue
        if is_hls_format(item):
            hls.append(item)
            continue
        height = safe_int(item.get("height"))
        if height is not None and height > settings.max_video_height:
            continue
        direct.append(item)

    def score(item: dict[str, Any]) -> tuple[int, int, int, int]:
        ext = safe_str(item.get("ext"))
        size = safe_int(item.get("filesize") or item.get("filesize_approx"))
        height = safe_int(item.get("height")) or 0
        return (
            1 if ext == "mp4" else 0,
            1 if size is None or size <= max_bytes else 0,
            min(height, settings.max_video_height),
            0 if size is None else -size,
        )

    preferred = [item for item in direct if safe_str(item.get("ext")) == "mp4"]
    selected_pool = preferred or direct
    if selected_pool:
        return max(selected_pool, key=score)
    if hls:
        raise SocialPipelineError(
            "social_hls_stream_not_supported",
            "This social video stream format is not supported yet.",
        )
    raise SocialPipelineError(
        "social_stream_url_missing",
        "A playable social video stream was not found.",
    )


def select_optional_direct_video_format(
    info_dict: dict[str, Any],
    settings: Settings,
) -> dict[str, Any] | None:
    try:
        return select_best_direct_video_format(info_dict, settings)
    except SocialPipelineError:
        return None


def collect_candidate_formats(info_dict: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    formats = info_dict.get("formats")
    if isinstance(formats, list):
        candidates.extend(item for item in formats if isinstance(item, dict))
    requested_downloads = info_dict.get("requested_downloads")
    if isinstance(requested_downloads, list):
        candidates.extend(item for item in requested_downloads if isinstance(item, dict))
    if safe_http_url(info_dict.get("url")):
        candidates.append(
            {
                "url": info_dict.get("url"),
                "http_headers": info_dict.get("http_headers"),
                "ext": info_dict.get("ext"),
                "width": info_dict.get("width"),
                "height": info_dict.get("height"),
                "filesize": info_dict.get("filesize"),
                "filesize_approx": info_dict.get("filesize_approx"),
                "protocol": info_dict.get("protocol"),
                "mime_type": info_dict.get("mime_type"),
            }
        )
    return candidates


def select_stream_url(
    formats: list[dict[str, Any]],
    max_height: int,
    max_memory_buffer_mb: int,
) -> str | None:
    max_bytes = max_memory_buffer_mb * 1024 * 1024
    usable = [
        item
        for item in formats
        if safe_http_url(item.get("url"))
        and not is_hls_format(item)
        and (safe_int(item.get("height")) or max_height) <= max_height
        and (
            safe_int(item.get("filesize") or item.get("filesize_approx")) is None
            or safe_int(item.get("filesize") or item.get("filesize_approx")) <= max_bytes
        )
    ]
    mp4 = [item for item in usable if item.get("ext") == "mp4"]
    chosen = (mp4 or usable)[-1] if (mp4 or usable) else None
    return str(chosen["url"]) if chosen else None


def has_hls_format(formats: list[dict[str, Any]]) -> bool:
    return any(is_hls_format(item) for item in formats)


def is_hls_stream(url: str) -> bool:
    return "m3u8" in url.lower()


def is_hls_format(item: dict[str, Any]) -> bool:
    protocol = str(item.get("protocol") or "").lower()
    return protocol in {"m3u8", "m3u8_native", "hls"} or is_hls_stream(
        str(item.get("url") or "")
    )


def safe_http_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def sanitized_ytdlp_metadata(
    info: dict[str, Any],
    selected_format: dict[str, Any] | None,
) -> dict[str, Any]:
    thumbnail = select_ytdlp_thumbnail(info, safe_str(info.get("extractor")) or "platform")
    return {
        "id": safe_str(info.get("id")),
        "title": safe_str(info.get("title")),
        "description_present": bool(safe_str(info.get("description"))),
        "duration": safe_float(info.get("duration")),
        "thumbnail_present": thumbnail.selected,
        "thumbnail_source": thumbnail.source,
        "uploader": safe_str(info.get("uploader") or info.get("channel")),
        "extractor": safe_str(info.get("extractor")),
        "ext": safe_str((selected_format or {}).get("ext") or info.get("ext")),
        "mime_type": safe_str((selected_format or {}).get("mime_type")),
        "width": safe_int((selected_format or {}).get("width") or info.get("width")),
        "height": safe_int((selected_format or {}).get("height") or info.get("height")),
        "filesize_approx": safe_int(
            (selected_format or {}).get("filesize")
            or (selected_format or {}).get("filesize_approx")
            or info.get("filesize")
            or info.get("filesize_approx")
        ),
        "direct_media_present": bool(
            safe_http_url((selected_format or {}).get("url"))
        ),
    }


def sanitize_message(message: str | None) -> str:
    cleaned = re.sub(r"https?://\S+", "[url]", message or "")
    return " ".join(cleaned.split())[:300]


def classify_ytdlp_error(stderr: str | None) -> str:
    text = (stderr or "").lower()
    if "timed out" in text or "timeout" in text:
        return "yt_dlp_extract_timeout"
    if "login" in text or "sign in" in text or "authenticate" in text:
        return "social_login_required"
    if "private" in text or "unavailable" in text:
        return "social_private_or_blocked"
    if "region" in text or "country" in text:
        return "social_region_blocked"
    if "no video formats" in text or "no formats" in text:
        return "yt_dlp_no_formats"
    return "yt_dlp_extract_failed"


def error_message_for_code(code: str) -> str:
    return {
        "yt_dlp_extract_timeout": "This social video took too long to access.",
        "social_login_required": "This social video requires login to access.",
        "social_private_or_blocked": "This social video is private, blocked, or unavailable.",
        "social_region_blocked": "This social video is not available from this region.",
        "yt_dlp_no_formats": "This social video did not expose playable formats.",
    }.get(code, "This social video could not be accessed.")


def merge_safe_headers(*header_sets: Any) -> dict[str, str]:
    merged: dict[str, str] = {}
    for headers in header_sets:
        if isinstance(headers, dict):
            for key, value in headers.items():
                if isinstance(key, str) and isinstance(value, str):
                    merged[key] = value
    return merged


def get_yt_dlp_version() -> str:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return completed.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def debug_social_resolve(
    url: str,
    platform: str,
    settings: Settings,
    *,
    test_stream: bool = False,
) -> dict[str, object]:
    started = time.monotonic()
    cookie_event, cookie_metadata = cookie_event_for_platform(platform, settings)
    try:
        info = resolve_social_video(url, settings)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        response: dict[str, object] = {
            "ok": True,
            "platform": platform,
            "yt_dlp_version": get_yt_dlp_version(),
            "cookie_strategy": cookie_metadata.get("cookie_strategy", cookie_event),
            "timed_out": False,
            "exit_code": 0,
            "elapsed_ms": elapsed_ms,
            "has_stream_url": True,
            "has_stream_headers": bool(info.stream_headers),
            "has_thumbnail": bool(info.thumbnail_url),
            "duration_seconds": info.duration_seconds,
            "extractor": info.extractor,
            "selected_ext": info.ext,
            "selected_height": None,
            "format_count": None,
            "selected_protocol": None,
            "header_keys": safe_header_keys(info.stream_headers),
            "sanitized_error": "",
        }
        if test_stream:
            response["stream_probe"] = probe_stream(
                info.stream_url,
                info.stream_headers,
                info.ext,
            )
        return response
    except SocialPipelineError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "platform": platform,
            "yt_dlp_version": get_yt_dlp_version(),
            "cookie_strategy": cookie_metadata.get("cookie_strategy", cookie_event),
            "timed_out": exc.code == "yt_dlp_extract_timeout",
            "exit_code": exc.metadata.get("exit_code"),
            "elapsed_ms": elapsed_ms,
            "has_stream_url": False,
            "has_stream_headers": False,
            "has_thumbnail": False,
            "duration_seconds": None,
            "extractor": None,
            "selected_ext": None,
            "selected_height": None,
            "format_count": None,
            "selected_protocol": None,
            "header_keys": [],
            "sanitized_error": str(exc.metadata.get("sanitized_message", exc.code))[:300],
        }


def probe_stream(
    stream_url: str,
    stream_headers: dict[str, str],
    ext: str | None,
) -> dict[str, object]:
    try:
        with requests.get(
            stream_url,
            stream=True,
            headers=stream_headers,
            timeout=(10, 30),
        ) as response:
            if response.status_code in {401, 403}:
                raise SocialPipelineError(
                    "social_stream_forbidden",
                    "This social video stream could not be accessed.",
                )
            if response.status_code == 404:
                raise SocialPipelineError(
                    "social_stream_expired",
                    "This social video stream expired before processing.",
                )
            response.raise_for_status()
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total >= 256 * 1024:
                    total = 256 * 1024
                    break
            content_type = response.headers.get("content-type", "").split(";", 1)[
                0
            ].lower()
            return {
                "status_code": response.status_code,
                "content_type": content_type,
                "bytes_read": total,
                "accepted": bool(total)
                and (
                    content_type.startswith("video/")
                    or (
                        content_type == "application/octet-stream"
                        and (ext or "").lower() == "mp4"
                    )
                ),
            }
    except SocialPipelineError:
        raise
    except requests.RequestException as exc:
        raise SocialPipelineError(
            "media_download_failed",
            "This social video could not be downloaded for analysis.",
            {"sanitized_message": sanitize_message(str(exc))},
        ) from exc


def safe_header_keys(headers: dict[str, str]) -> list[str]:
    forbidden = {"cookie", "authorization"}
    preferred = ["User-Agent", "Referer", "Accept-Language"]
    present = {key.lower(): key for key in headers if key.lower() not in forbidden}
    ordered = [key for key in preferred if key.lower() in present]
    rest = sorted(
        key
        for key in headers
        if key.lower() not in forbidden
        and key not in ordered
    )
    return ordered + rest


def safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def safe_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
