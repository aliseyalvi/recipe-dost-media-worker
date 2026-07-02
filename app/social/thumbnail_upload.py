from __future__ import annotations

import shutil
import subprocess
from uuid import UUID

import requests

from app.config import Settings
from app.supabase_client import MediaJobRepository

ALLOWED = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
SAMPLE_WIDTH = 160
SAMPLE_HEIGHT = 90


def upload_source_thumbnail(
    thumbnail_url: str | None,
    *,
    user_id: UUID,
    media_job_id: UUID,
    repository: MediaJobRepository,
    settings: Settings,
) -> str | None:
    if not thumbnail_url or not settings.use_source_thumbnail_first:
        return None
    response = requests.get(thumbnail_url, timeout=(5, 20), headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in ALLOWED:
        raise ValueError("Unsupported thumbnail type.")
    data = response.content
    if len(data) > 3 * 1024 * 1024:
        raise ValueError("Thumbnail too large.")
    path = f"media-jobs/{media_job_id}/thumbnail.{ALLOWED[content_type]}"
    return repository.upload_public_object(
        settings.thumbnail_bucket,
        path,
        data,
        content_type,
    )


def sample_video_frame_thumbnail(
    video_path: str | None,
    *,
    duration_seconds: int | None,
    user_id: UUID,
    media_job_id: UUID,
    repository: MediaJobRepository,
    settings: Settings,
) -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if not video_path or not ffmpeg:
        return None
    timestamp = select_frame_timestamp(ffmpeg, video_path, duration_seconds)
    if timestamp is None:
        return None
    image = extract_jpeg_frame(ffmpeg, video_path, timestamp)
    if not image:
        return None
    path = f"media-jobs/{media_job_id}/thumbnail-sampled.jpg"
    return repository.upload_public_object(
        settings.thumbnail_bucket,
        path,
        image,
        "image/jpeg",
    )


def select_frame_timestamp(
    ffmpeg: str,
    video_path: str,
    duration_seconds: int | None,
) -> float | None:
    timestamps = candidate_timestamps(duration_seconds)
    best: tuple[float, float] | None = None
    seen_frames: list[bytes] = []
    for timestamp in timestamps:
        frame = extract_raw_frame(ffmpeg, video_path, timestamp)
        if frame is None or is_near_duplicate(frame, seen_frames):
            continue
        seen_frames.append(frame)
        score = frame_score(frame)
        if score <= 0:
            continue
        if best is None or score > best[1]:
            best = (timestamp, score)
    return best[0] if best else None


def candidate_timestamps(duration_seconds: int | None) -> list[float]:
    if duration_seconds and duration_seconds > 0:
        return [
            max(0.5, duration_seconds * ratio)
            for ratio in (0.10, 0.25, 0.40, 0.60)
        ]
    return [1.0, 3.0, 5.0, 8.0]


def extract_raw_frame(ffmpeg: str, video_path: str, timestamp: float) -> bytes | None:
    command = [
        ffmpeg,
        "-v",
        "error",
        "-ss",
        f"{timestamp:.2f}",
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-vf",
        f"scale={SAMPLE_WIDTH}:{SAMPLE_HEIGHT}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except Exception:
        return None
    expected = SAMPLE_WIDTH * SAMPLE_HEIGHT * 3
    return completed.stdout if len(completed.stdout) == expected else None


def extract_jpeg_frame(ffmpeg: str, video_path: str, timestamp: float) -> bytes | None:
    command = [
        ffmpeg,
        "-v",
        "error",
        "-ss",
        f"{timestamp:.2f}",
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except Exception:
        return None
    data = completed.stdout
    if completed.returncode != 0 or not data.startswith(b"\xff\xd8") or len(data) > 3 * 1024 * 1024:
        return None
    return data


def frame_score(frame: bytes) -> float:
    pixels = len(frame) // 3
    if pixels <= 0:
        return 0
    brightness_total = 0.0
    color_total = 0.0
    lumas: list[float] = []
    for index in range(0, len(frame), 3):
        red = frame[index]
        green = frame[index + 1]
        blue = frame[index + 2]
        brightness = (red + green + blue) / 3
        brightness_total += brightness
        color_total += max(red, green, blue) - min(red, green, blue)
        lumas.append(brightness)
    brightness_avg = brightness_total / pixels
    if brightness_avg < 25 or brightness_avg > 245:
        return 0
    color_avg = color_total / pixels
    if color_avg < 6:
        return 0
    sharpness = average_neighbor_delta(lumas)
    return color_avg * 1.4 + sharpness + min(brightness_avg, 255 - brightness_avg) * 0.15


def average_neighbor_delta(lumas: list[float]) -> float:
    if len(lumas) < 2:
        return 0
    total = 0.0
    count = 0
    for index in range(1, len(lumas)):
        total += abs(lumas[index] - lumas[index - 1])
        count += 1
    return total / max(1, count)


def is_near_duplicate(frame: bytes, seen_frames: list[bytes]) -> bool:
    return any(mean_abs_difference(frame, item) < 4 for item in seen_frames)


def mean_abs_difference(left: bytes, right: bytes) -> float:
    if len(left) != len(right) or not left:
        return 255
    step = max(3, len(left) // 1000)
    total = 0
    count = 0
    for index in range(0, len(left), step):
        total += abs(left[index] - right[index])
        count += 1
    return total / max(1, count)
