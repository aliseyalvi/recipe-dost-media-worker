from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.media_resolvers.contracts import ResolverError
from app.worker_runtime.resources import ffmpeg_process_slot


@dataclass(frozen=True)
class MediaProbe:
    video_present: bool
    audio_present: bool
    duration_seconds: int | None
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None
    container: str | None


H264_CODECS = frozenset({"h264", "avc", "avc1"})
MP4_FORMATS = frozenset({"mp4", "mov", "m4v"})


def probe_media(path: Path, *, timeout_seconds: int) -> MediaProbe:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ResolverError(
            "resolver_configuration_error",
            "Media validation is not available on this worker.",
            technical=True,
        )
    try:
        with ffmpeg_process_slot():
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type,codec_name,width,height:format=duration,format_name",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=max(1, timeout_seconds),
                check=False,
            )
        payload = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ResolverError(
            "media_validation_failed",
            "Downloaded media could not be validated.",
            fallback_allowed=True,
            technical=True,
        ) from exc
    if result.returncode != 0 or not isinstance(payload, dict):
        raise ResolverError(
            "media_validation_failed",
            "Downloaded media could not be validated.",
            fallback_allowed=True,
        )
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    video = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"),
        None,
    )
    format_data = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    return MediaProbe(
        video_present=video is not None,
        audio_present=audio is not None,
        duration_seconds=_duration(format_data.get("duration")),
        width=_positive_int(video.get("width")) if video else None,
        height=_positive_int(video.get("height")) if video else None,
        video_codec=str(video.get("codec_name") or "") or None if video else None,
        audio_codec=str(audio.get("codec_name") or "") or None if audio else None,
        container=str(format_data.get("format_name") or "") or None,
    )


def validate_final_media(
    path: Path,
    *,
    byte_limit: int,
    duration_limit_seconds: int,
    timeout_seconds: int,
) -> MediaProbe:
    if not path.exists() or path.stat().st_size <= 0:
        raise ResolverError("media_validation_failed", "Downloaded media is empty.")
    if path.stat().st_size > byte_limit:
        raise ResolverError("media_size_exceeded", "This video is too large to process safely.")
    probe = probe_media(path, timeout_seconds=timeout_seconds)
    if not probe.video_present:
        raise ResolverError("media_validation_failed", "Downloaded media does not contain video.")
    validate_video_probe_compatibility(probe)
    if not probe.audio_present:
        raise ResolverError("media_has_no_audio", "This video does not contain usable audio.")
    if probe.duration_seconds is not None and probe.duration_seconds > duration_limit_seconds:
        raise ResolverError(
            "video_duration_exceeded",
            "This video is longer than the supported duration.",
        )
    return probe


def validate_video_probe_compatibility(probe: MediaProbe) -> None:
    if not probe.video_present:
        raise ResolverError(
            "media_validation_failed",
            "Downloaded media does not contain video.",
            fallback_allowed=True,
            failure_stage="ffprobe_video",
        )
    codec = str(probe.video_codec or "").strip().lower()
    if codec not in H264_CODECS:
        raise ResolverError(
            "unsupported_media",
            "Downloaded video uses an unsupported video codec.",
            fallback_allowed=True,
            failure_stage="ffprobe_video",
        )
    formats = {
        value.strip().lower()
        for value in str(probe.container or "").split(",")
        if value.strip()
    }
    if not formats.intersection(MP4_FORMATS):
        raise ResolverError(
            "unsupported_media",
            "Downloaded video uses an unsupported container.",
            fallback_allowed=True,
            failure_stage="ffprobe_video",
        )
    if not probe.width or not probe.height or probe.width <= 0 or probe.height <= 0:
        raise ResolverError(
            "media_validation_failed",
            "Downloaded video dimensions are invalid.",
            fallback_allowed=True,
            failure_stage="ffprobe_video",
        )


def mux_video_audio(
    video_path: Path,
    audio_path: Path,
    *,
    output_path: Path,
    timeout_seconds: int,
    byte_limit: int,
) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ResolverError(
            "resolver_configuration_error",
            "Media merge support is not available on this worker.",
            technical=True,
        )
    commands = [
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
    ]
    for command in commands:
        output_path.unlink(missing_ok=True)
        try:
            with ffmpeg_process_slot():
                result = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=max(1, timeout_seconds),
                    check=False,
                )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and output_path.exists() and 0 < output_path.stat().st_size <= byte_limit:
            return output_path
    output_path.unlink(missing_ok=True)
    raise ResolverError(
        "media_merge_failed",
        "Video and audio could not be prepared together.",
        fallback_allowed=True,
        technical=True,
    )


def _duration(value: object) -> int | None:
    try:
        duration = float(str(value))
    except (TypeError, ValueError):
        return None
    return int(duration) if duration >= 0 else None


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
