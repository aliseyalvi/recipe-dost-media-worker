from __future__ import annotations

import base64
import io
import logging
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
    collect_candidate_formats,
    platform_headers,
    safe_float,
    safe_http_url,
    safe_str,
    sanitized_ytdlp_metadata,
)

logger = logging.getLogger("recipe_dost.worker")

MIB_BYTES = 1024 * 1024
QUALITY_LADDER = (720, 540, 480, 360)
SUPPORTED_VIDEO_EXTENSIONS = {"mp4", "m4v", "mov", "webm"}
TERMINAL_SIZE_MESSAGE = (
    "Recipe Dost tried a smaller video version, but this video is still too "
    "large for automatic recipe analysis. Try a shorter video or another recipe link."
)


@dataclass
class VideoBuffer:
    buffer: io.BytesIO
    size_bytes: int
    mime_type: str
    ext: str | None = None


@dataclass(frozen=True)
class MediaFormatCandidate:
    format_id: str
    width: int | None
    height: int | None
    quality_height: int | None
    total_bitrate_kbps: float | None
    fps: float | None
    ext: str
    has_audio: bool
    has_video: bool
    filesize: int | None
    filesize_approx: int | None
    estimated_size_bytes: int | None
    estimate_source: str
    raw: dict[str, Any]

    @property
    def resolution_bucket(self) -> str:
        return resolution_bucket(self.quality_height)


class HardByteLimitExceeded(Exception):
    pass


class HardByteGuard:
    def __init__(self, hard_limit_bytes: int) -> None:
        self.hard_limit_bytes = hard_limit_bytes
        self.observed_bytes = 0
        self.exceeded = False

    def __call__(self, progress: dict[str, Any]) -> None:
        observed = safe_int(progress.get("downloaded_bytes")) or 0
        self.observed_bytes = max(self.observed_bytes, observed)
        if observed > self.hard_limit_bytes:
            self.exceeded = True
            raise HardByteLimitExceeded("media_size_exceeded")


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
    if (
        normalized_platform == "instagram"
        and settings.enable_ytdlp_cookies
        and settings.enable_instagram_cookies
        and not cookie_file
    ):
        return extractor_failure(
            normalized_platform,
            "instagram_cookies_missing",
            "Instagram cookies are not configured for extraction.",
            resolver="ytdlp_stream_cookies",
            cookie_mode="platform_cookie_file",
            output_mode=output_mode,
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

    if (
        cookie_file
        and not use_cookie_first
        and should_retry_with_cookies(no_cookie_result)
    ):
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
    except Exception as exc:
        result = extractor_failure(
            platform,
            "unified_extractor_failed",
            "This social video could not be accessed.",
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode=output_mode,
        )
        if is_unexpected_ytdlp_exception(exc):
            result["_technical_exception"] = exc
        return result
    if not isinstance(info, dict):
        return extractor_failure(
            platform,
            "unified_extractor_failed",
            "This social video could not be accessed.",
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode=output_mode,
        )
    candidates = build_media_format_candidates(info, settings)
    attempts = build_adaptive_attempts(candidates, settings)
    selected_format = attempts[0].raw if attempts else None
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
            attempts,
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
        attempts,
        resolver=resolver,
        cookie_mode=cookie_mode,
    )


def ytdlp_options(
    platform: str,
    settings: Settings,
    cookie_file: str | None,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "noplaylist": True,
        "retries": settings.ytdlp_retries,
        "fragment_retries": settings.ytdlp_retries,
        "extractor_retries": settings.ytdlp_retries,
        "socket_timeout": settings.ytdlp_socket_timeout_seconds,
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
    attempts: list[MediaFormatCandidate],
    *,
    resolver: str,
    cookie_mode: str,
) -> dict[str, object]:
    from yt_dlp import YoutubeDL

    job_dir = Path(settings.temp_media_dir) / safe_path_segment(job_id)
    preferred_bytes = settings.preferred_video_size_bytes
    hard_bytes = settings.max_video_size_bytes
    last_size_bytes: int | None = None
    last_candidate: MediaFormatCandidate | None = None
    technical_exception: Exception | None = None
    size_fallback_used = False

    if not attempts:
        return no_candidate_result(
            platform,
            info,
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode="temp_file",
            settings=settings,
        )

    for index, candidate in enumerate(attempts, 1):
        last_candidate = candidate
        cleanup_job_dir(job_dir)
        job_dir.mkdir(parents=True, exist_ok=True)
        guard = HardByteGuard(hard_bytes)
        download_opts = {
            **opts,
            "format": candidate.format_id,
            "outtmpl": str(job_dir / "video.%(ext)s"),
            "progress_hooks": [guard],
        }
        try:
            with YoutubeDL(download_opts) as ydl:
                ydl.download([url])
        except Exception as exc:
            if technical_exception is None and is_unexpected_ytdlp_exception(exc):
                technical_exception = exc
            last_size_bytes = guard.observed_bytes or candidate.estimated_size_bytes
            cleanup_job_dir(job_dir)
            if guard.exceeded:
                size_fallback_used = True
                log_format_fallback(
                    index,
                    candidate,
                    next_candidate(attempts, index),
                    "hard_size_exceeded",
                )
                continue
            log_format_fallback(
                index,
                candidate,
                next_candidate(attempts, index),
                "download_failed",
            )
            continue

        file_path = locate_downloaded_file(job_dir)
        if file_path is None:
            cleanup_job_dir(job_dir)
            log_format_fallback(
                index,
                candidate,
                next_candidate(attempts, index),
                "download_missing",
            )
            continue
        size_bytes = file_path.stat().st_size
        last_size_bytes = size_bytes
        has_smaller_attempt = index < len(attempts)
        if size_bytes > hard_bytes:
            size_fallback_used = True
            cleanup_job_dir(job_dir)
            log_format_fallback(
                index,
                candidate,
                next_candidate(attempts, index),
                "hard_size_exceeded",
            )
            continue
        if size_bytes > preferred_bytes and has_smaller_attempt:
            size_fallback_used = True
            cleanup_job_dir(job_dir)
            log_format_fallback(
                index,
                candidate,
                next_candidate(attempts, index),
                "preferred_size_exceeded",
            )
            continue
        return extractor_result(
            platform,
            info,
            candidate.raw,
            success=True,
            temp_file_path=str(file_path),
            size_bytes=size_bytes,
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode="temp_file",
            adaptive_attempt_count=index,
            format_candidate_count=len(build_media_format_candidates(info, settings)),
            selected_resolution_bucket=candidate.resolution_bucket,
            estimated_size_bucket=size_bucket(candidate.estimated_size_bytes),
            actual_size_bucket=size_bucket(size_bytes),
            size_fallback_used=size_fallback_used,
        )

    cleanup_job_dir(job_dir)
    result = extractor_result(
        platform,
        info,
        last_candidate.raw if last_candidate else None,
        success=False,
        error_code=(
            "media_size_exceeded"
            if size_fallback_used or all_candidates_exceed_hard(info, settings)
            else "unified_extractor_failed"
        ),
        safe_error_message=(
            TERMINAL_SIZE_MESSAGE
            if size_fallback_used or all_candidates_exceed_hard(info, settings)
            else "This social video could not be downloaded."
        ),
        size_bytes=last_size_bytes,
        resolver=resolver,
        cookie_mode=cookie_mode,
        output_mode="temp_file",
        adaptive_attempt_count=len(attempts),
        format_candidate_count=len(build_media_format_candidates(info, settings)),
        selected_resolution_bucket=(last_candidate.resolution_bucket if last_candidate else None),
        estimated_size_bucket=(size_bucket(last_candidate.estimated_size_bytes) if last_candidate else None),
        actual_size_bucket=size_bucket(last_size_bytes),
        size_fallback_used=size_fallback_used,
    )
    if (
        result.get("error_code") != "media_size_exceeded"
        and technical_exception is not None
    ):
        result["_technical_exception"] = technical_exception
    return result


def extract_to_memory(
    url: str,
    platform: str,
    job_id: str,
    settings: Settings,
    opts: dict[str, Any],
    info: dict[str, Any],
    attempts: list[MediaFormatCandidate],
    *,
    resolver: str,
    cookie_mode: str,
) -> dict[str, object]:
    from yt_dlp import YoutubeDL

    preferred_bytes = min(
        settings.preferred_video_size_bytes,
        settings.max_memory_buffer_mb * MIB_BYTES,
    )
    hard_bytes = min(
        settings.max_video_size_bytes,
        settings.max_memory_buffer_mb * MIB_BYTES,
    )
    last_size_bytes: int | None = None
    last_candidate: MediaFormatCandidate | None = None
    technical_exception: Exception | None = None
    size_fallback_used = False
    if not attempts:
        return no_candidate_result(
            platform,
            info,
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode="memory_buffer",
            settings=settings,
        )

    for index, candidate in enumerate(attempts, 1):
        last_candidate = candidate
        writer = InMemoryVideoWriter()
        original_stdout = sys.stdout
        guard = HardByteGuard(hard_bytes)
        try:
            sys.stdout = writer  # type: ignore[assignment]
            download_opts = {
                **opts,
                "format": candidate.format_id,
                "outtmpl": "-",
                "progress_hooks": [guard],
            }
            with YoutubeDL(download_opts) as ydl:
                ydl.download([url])
        except Exception as exc:
            if technical_exception is None and is_unexpected_ytdlp_exception(exc):
                technical_exception = exc
            last_size_bytes = guard.observed_bytes or candidate.estimated_size_bytes
            writer.buffer.close()
            if guard.exceeded:
                size_fallback_used = True
            log_format_fallback(
                index,
                candidate,
                next_candidate(attempts, index),
                "hard_size_exceeded" if guard.exceeded else "download_failed",
            )
            continue
        finally:
            sys.stdout = original_stdout
        size_bytes = writer.buffer.getbuffer().nbytes
        last_size_bytes = size_bytes
        if size_bytes > hard_bytes or (
            size_bytes > preferred_bytes and index < len(attempts)
        ):
            size_fallback_used = True
            writer.buffer.close()
            log_format_fallback(
                index,
                candidate,
                next_candidate(attempts, index),
                (
                    "hard_size_exceeded"
                    if size_bytes > hard_bytes
                    else "preferred_size_exceeded"
                ),
            )
            continue
        writer.buffer.seek(0)
        return extractor_result(
            platform,
            info,
            candidate.raw,
            success=True,
            video_buffer=writer.buffer,
            size_bytes=size_bytes,
            resolver=resolver,
            cookie_mode=cookie_mode,
            output_mode="memory_buffer",
            adaptive_attempt_count=index,
            format_candidate_count=len(build_media_format_candidates(info, settings)),
            selected_resolution_bucket=candidate.resolution_bucket,
            estimated_size_bucket=size_bucket(candidate.estimated_size_bytes),
            actual_size_bucket=size_bucket(size_bytes),
            size_fallback_used=size_fallback_used,
        )

    result = extractor_result(
        platform,
        info,
        last_candidate.raw if last_candidate else None,
        success=False,
        error_code="media_size_exceeded" if size_fallback_used else "unified_extractor_failed",
        safe_error_message=(
            TERMINAL_SIZE_MESSAGE
            if size_fallback_used
            else "This social video could not be downloaded."
        ),
        size_bytes=last_size_bytes,
        resolver=resolver,
        cookie_mode=cookie_mode,
        output_mode="memory_buffer",
        adaptive_attempt_count=len(attempts),
        format_candidate_count=len(build_media_format_candidates(info, settings)),
        selected_resolution_bucket=(last_candidate.resolution_bucket if last_candidate else None),
        estimated_size_bucket=(size_bucket(last_candidate.estimated_size_bytes) if last_candidate else None),
        actual_size_bucket=size_bucket(last_size_bytes),
        size_fallback_used=size_fallback_used,
    )
    if (
        result.get("error_code") != "media_size_exceeded"
        and technical_exception is not None
    ):
        result["_technical_exception"] = technical_exception
    return result


def build_media_format_candidates(
    info: dict[str, Any],
    settings: Settings,
) -> list[MediaFormatCandidate]:
    duration_seconds = safe_float(info.get("duration"))
    candidates: list[MediaFormatCandidate] = []
    seen: set[str] = set()
    for item in collect_candidate_formats(info):
        format_id = safe_str(item.get("format_id"))
        if not format_id or format_id in seen:
            continue
        seen.add(format_id)
        ext = (safe_str(item.get("ext")) or "").lower()
        if ext not in SUPPORTED_VIDEO_EXTENSIONS:
            continue
        vcodec = (safe_str(item.get("vcodec")) or "").lower()
        acodec = (safe_str(item.get("acodec")) or "").lower()
        has_video = bool(vcodec and vcodec != "none")
        has_audio = bool(acodec and acodec != "none")
        if not has_video or not has_audio:
            continue
        width = safe_int(item.get("width"))
        height = safe_int(item.get("height"))
        quality_height = short_edge_resolution(width, height)
        if quality_height is None or quality_height < settings.min_video_height:
            continue
        filesize = positive_int(item.get("filesize"))
        filesize_approx = positive_int(item.get("filesize_approx"))
        total_bitrate = candidate_total_bitrate_kbps(item)
        estimated, source = estimate_candidate_size(
            filesize=filesize,
            filesize_approx=filesize_approx,
            duration_seconds=duration_seconds,
            total_bitrate_kbps=total_bitrate,
        )
        candidates.append(
            MediaFormatCandidate(
                format_id=format_id,
                width=width,
                height=height,
                quality_height=quality_height,
                total_bitrate_kbps=total_bitrate,
                fps=safe_float(item.get("fps")),
                ext=ext,
                has_audio=has_audio,
                has_video=has_video,
                filesize=filesize,
                filesize_approx=filesize_approx,
                estimated_size_bytes=estimated,
                estimate_source=source,
                raw=item,
            )
        )
    return candidates


def build_adaptive_attempts(
    candidates: list[MediaFormatCandidate],
    settings: Settings,
) -> list[MediaFormatCandidate]:
    preferred_bytes = settings.preferred_video_size_bytes
    hard_bytes = settings.max_video_size_bytes
    selected: list[MediaFormatCandidate] = []
    selected_ids: set[str] = set()

    for height_limit in QUALITY_LADDER:
        if height_limit > settings.max_video_height or height_limit < settings.min_video_height:
            continue
        fitting = [
            candidate
            for candidate in candidates
            if candidate.format_id not in selected_ids
            and candidate.quality_height is not None
            and settings.min_video_height <= candidate.quality_height <= height_limit
            and candidate.estimated_size_bytes is not None
            and candidate.estimated_size_bytes <= preferred_bytes
        ]
        if not fitting:
            continue
        chosen = max(fitting, key=quality_rank)
        selected.append(chosen)
        selected_ids.add(chosen.format_id)
        if len(selected) >= settings.media_adaptive_max_attempts:
            return selected

    hard_candidates = [
        candidate
        for candidate in candidates
        if candidate.format_id not in selected_ids
        and (
            candidate.quality_height is not None
            and candidate.quality_height <= settings.max_video_height
        )
        and (
            candidate.estimated_size_bytes is None
            or candidate.estimated_size_bytes <= hard_bytes
        )
    ]
    if not selected and hard_candidates:
        # Unknown sizes are bounded by the streaming guard. The final hard-limit
        # attempt deliberately uses the smallest useful source candidate.
        chosen = min(hard_candidates, key=smallest_useful_rank)
        selected.append(chosen)
    return selected[: settings.media_adaptive_max_attempts]


def estimate_candidate_size(
    *,
    filesize: int | None,
    filesize_approx: int | None,
    duration_seconds: float | None,
    total_bitrate_kbps: float | None,
) -> tuple[int | None, str]:
    if filesize is not None:
        return filesize, "filesize"
    if filesize_approx is not None:
        return filesize_approx, "filesize_approx"
    if duration_seconds and duration_seconds > 0 and total_bitrate_kbps and total_bitrate_kbps > 0:
        # yt-dlp bitrate fields are in Kbit/s. Five percent allows for container
        # overhead while the hard streaming guard remains authoritative.
        return int(duration_seconds * total_bitrate_kbps * 1000 / 8 * 1.05), "bitrate"
    return None, "unknown"


def candidate_total_bitrate_kbps(item: dict[str, Any]) -> float | None:
    total = safe_float(item.get("tbr"))
    if total is not None and total > 0:
        return total
    video = safe_float(item.get("vbr")) or 0.0
    audio = safe_float(item.get("abr")) or 0.0
    combined = video + audio
    return combined if combined > 0 else None


def quality_rank(candidate: MediaFormatCandidate) -> tuple[int, int, float, float, int]:
    return (
        candidate.quality_height or 0,
        1 if candidate.ext == "mp4" else 0,
        candidate.total_bitrate_kbps or 0.0,
        min(candidate.fps or 0.0, 60.0),
        -(candidate.estimated_size_bytes or 0),
    )


def smallest_useful_rank(candidate: MediaFormatCandidate) -> tuple[int, int, int, float]:
    return (
        (
            candidate.quality_height
            if candidate.quality_height is not None
            else settings_unknown_height()
        ),
        candidate.estimated_size_bytes if candidate.estimated_size_bytes is not None else 2**63,
        0 if candidate.ext == "mp4" else 1,
        -(candidate.total_bitrate_kbps or 0.0),
    )


def settings_unknown_height() -> int:
    return 10_000


def positive_int(value: object) -> int | None:
    parsed = safe_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def no_candidate_result(
    platform: str,
    info: dict[str, Any],
    *,
    resolver: str,
    cookie_mode: str,
    output_mode: str,
    settings: Settings,
) -> dict[str, object]:
    candidates = build_media_format_candidates(info, settings)
    size_limited = bool(candidates) and all_candidates_exceed_hard(info, settings)
    diagnostic_candidate = (
        min(candidates, key=smallest_useful_rank) if size_limited else None
    )
    return extractor_result(
        platform,
        info,
        diagnostic_candidate.raw if diagnostic_candidate else None,
        success=False,
        error_code="media_size_exceeded" if size_limited else "unified_extractor_failed",
        safe_error_message=(
            TERMINAL_SIZE_MESSAGE
            if size_limited
            else "This social video did not expose a supported video format with audio."
        ),
        resolver=resolver,
        cookie_mode=cookie_mode,
        output_mode=output_mode,
        adaptive_attempt_count=0,
        format_candidate_count=len(candidates),
        selected_resolution_bucket=(
            diagnostic_candidate.resolution_bucket if diagnostic_candidate else None
        ),
        estimated_size_bucket=(
            size_bucket(diagnostic_candidate.estimated_size_bytes)
            if diagnostic_candidate
            else None
        ),
        size_fallback_used=size_limited,
    )


def all_candidates_exceed_hard(info: dict[str, Any], settings: Settings) -> bool:
    candidates = [
        candidate
        for candidate in build_media_format_candidates(info, settings)
        if candidate.quality_height is not None
        and candidate.quality_height <= settings.max_video_height
    ]
    return bool(candidates) and all(
        candidate.estimated_size_bytes is not None
        and candidate.estimated_size_bytes > settings.max_video_size_bytes
        for candidate in candidates
    )


def should_retry_with_cookies(result: dict[str, object]) -> bool:
    return result.get("error_code") not in {
        "media_size_exceeded",
        "video_duration_exceeded",
    }


def is_unexpected_ytdlp_exception(exc: Exception) -> bool:
    # yt-dlp DownloadError represents ordinary access/format/provider failures.
    # Other exception classes indicate an unexpected technical boundary failure.
    return type(exc).__name__ not in {
        "DownloadError",
        "ExtractorError",
        "GeoRestrictedError",
        "HardByteLimitExceeded",
    }


def cleanup_job_dir(job_dir: Path) -> None:
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)


def next_candidate(
    attempts: list[MediaFormatCandidate],
    current_attempt_number: int,
) -> MediaFormatCandidate | None:
    return (
        attempts[current_attempt_number]
        if current_attempt_number < len(attempts)
        else None
    )


def log_format_fallback(
    attempt: int,
    candidate: MediaFormatCandidate,
    following: MediaFormatCandidate | None,
    reason: str,
) -> None:
    if following is None:
        return
    logger.info(
        "media_format_fallback",
        extra={
            "stage": "unified_extractor",
            "reason": reason,
            "attempt": attempt,
            "from_resolution": candidate.resolution_bucket,
            "to_resolution": following.resolution_bucket,
            "terminal": False,
        },
    )


def resolution_bucket(height: int | None) -> str:
    if height is None:
        return "unknown"
    for boundary in (360, 480, 540, 720, 1080, 1440, 2160):
        if height <= boundary:
            return f"{boundary}p"
    return "over_2160p"


def short_edge_resolution(width: int | None, height: int | None) -> int | None:
    dimensions = [value for value in (width, height) if value is not None and value > 0]
    return min(dimensions) if dimensions else None


def duration_bucket(duration_seconds: int | None) -> str:
    if duration_seconds is None:
        return "unknown"
    if duration_seconds <= 30:
        return "0_30_seconds"
    if duration_seconds <= 60:
        return "30_60_seconds"
    if duration_seconds <= 90:
        return "60_90_seconds"
    if duration_seconds <= 120:
        return "90_120_seconds"
    return "over_120_seconds"


def size_bucket(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "unknown"
    mib = size_bytes / MIB_BYTES
    if mib < 20:
        return "under_20_mib"
    if mib < 30:
        return "20_30_mib"
    if mib < 40:
        return "30_40_mib"
    if mib < 50:
        return "40_50_mib"
    return "50_mib_or_more"


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
        "thumbnail_source": "placeholder",
        "thumbnail_provider": platform,
        "thumbnail_strategy": "platform_first_v2",
        "thumbnail_candidate_count": 0,
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
    adaptive_attempt_count: int = 0,
    format_candidate_count: int = 0,
    selected_resolution_bucket: str | None = None,
    estimated_size_bucket: str | None = None,
    actual_size_bucket: str | None = None,
    size_fallback_used: bool = False,
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
    thumbnail = select_ytdlp_thumbnail(info, platform)
    return {
        "success": success,
        "platform": platform,
        "resolver": resolver,
        "title": safe_str(info.get("title")),
        "description": safe_str(info.get("description")),
        "author_username": safe_str(info.get("uploader") or info.get("channel")),
        "thumbnail_url": thumbnail.url,
        "thumbnail_source": thumbnail.source,
        "thumbnail_provider": thumbnail.provider,
        "thumbnail_strategy": thumbnail.strategy,
        "thumbnail_candidate_count": thumbnail.candidate_count,
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
        "adaptive_attempt_count": adaptive_attempt_count,
        "format_candidate_count": format_candidate_count,
        "selected_resolution_bucket": selected_resolution_bucket,
        "estimated_size_bucket": estimated_size_bucket,
        "actual_size_bucket": actual_size_bucket,
        "duration_bucket": duration_bucket(safe_duration_seconds(info.get("duration"))),
        "size_fallback_used": size_fallback_used,
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
        platform_thumbnail(
            tags.get("og:image"),
            source="existing_platform_thumbnail",
            provider=platform,
        ).url
        or platform_thumbnail(
            tags.get("twitter:image"),
            source="existing_platform_thumbnail",
            provider=platform,
        ).url
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
        "thumbnail_source": "existing_platform_thumbnail" if image else "placeholder",
        "thumbnail_provider": platform,
        "thumbnail_strategy": "platform_first_v2",
        "thumbnail_candidate_count": 1 if image else 0,
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
