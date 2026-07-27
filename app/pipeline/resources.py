from __future__ import annotations

import os
import resource
import shutil
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from app.config import Settings

_ffmpeg_slots = threading.BoundedSemaphore(1)
_gemini_upload_slots = threading.BoundedSemaphore(1)
_ffmpeg_active = 0
_gemini_upload_active = 0
_ffmpeg_lock = threading.Lock()
_gemini_upload_lock = threading.Lock()


def configure_ffmpeg_slots(limit: int) -> None:
    global _ffmpeg_slots
    _ffmpeg_slots = threading.BoundedSemaphore(max(1, limit))


def configure_gemini_upload_slots(limit: int) -> None:
    global _gemini_upload_slots
    _gemini_upload_slots = threading.BoundedSemaphore(max(1, limit))


@contextmanager
def ffmpeg_process_slot() -> Iterator[None]:
    global _ffmpeg_active
    _ffmpeg_slots.acquire()
    with _ffmpeg_lock:
        _ffmpeg_active += 1
    try:
        yield
    finally:
        with _ffmpeg_lock:
            _ffmpeg_active = max(0, _ffmpeg_active - 1)
        _ffmpeg_slots.release()


def active_ffmpeg_processes() -> int:
    with _ffmpeg_lock:
        return _ffmpeg_active


@contextmanager
def gemini_upload_slot() -> Iterator[None]:
    global _gemini_upload_active
    _gemini_upload_slots.acquire()
    with _gemini_upload_lock:
        _gemini_upload_active += 1
    try:
        yield
    finally:
        with _gemini_upload_lock:
            _gemini_upload_active = max(0, _gemini_upload_active - 1)
        _gemini_upload_slots.release()


def active_gemini_uploads() -> int:
    with _gemini_upload_lock:
        return _gemini_upload_active


@dataclass(frozen=True)
class AdmissionSnapshot:
    admitted: bool
    reason: str
    rss_bytes: int
    temp_bytes: int
    free_temp_bytes: int
    estimated_temp_bytes: int


class ResourceAdmissionController:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def inspect(self) -> AdmissionSnapshot:
        rss = current_rss_bytes()
        temp = active_temp_bytes(self._settings.temp_media_dir)
        free = free_temp_bytes(self._settings.temp_media_dir)
        # Worst case is split A/V plus mux output. This intentionally
        # over-reserves rather than claiming jobs the process cannot hold.
        estimated = self._settings.max_video_size_bytes * 3
        max_rss = self._settings.worker_max_rss_mb * 1024 * 1024
        max_temp = self._settings.worker_max_active_temp_mb * 1024 * 1024
        min_free = self._settings.worker_min_free_temp_mb * 1024 * 1024
        if rss >= max_rss:
            reason = "rss_budget"
        elif temp + estimated > max_temp:
            reason = "temp_budget"
        elif free - estimated < min_free:
            reason = "temp_free_space"
        elif active_ffmpeg_processes() >= self._settings.worker_ffmpeg_concurrency:
            reason = "ffmpeg_budget"
        elif active_gemini_uploads() >= self._settings.worker_gemini_upload_concurrency:
            reason = "gemini_upload_budget"
        else:
            reason = "admitted"
        return AdmissionSnapshot(
            admitted=reason == "admitted",
            reason=reason,
            rss_bytes=rss,
            temp_bytes=temp,
            free_temp_bytes=free,
            estimated_temp_bytes=estimated,
        )


def current_rss_bytes() -> int:
    statm = Path("/proc/self/statm")
    if statm.exists():
        try:
            resident_pages = int(statm.read_text(encoding="ascii").split()[1])
            return resident_pages * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            pass
    if os.uname().sysname == "Darwin":
        try:
            completed = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(os.getpid())],
                capture_output=True,
                text=True,
                check=True,
                timeout=2,
            )
            return int(completed.stdout.strip()) * 1024
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if os.uname().sysname == "Darwin" else value * 1024


def active_temp_bytes(temp_root: str) -> int:
    root = Path(temp_root)
    if not root.exists():
        return 0
    total = 0
    try:
        for path in root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def free_temp_bytes(temp_root: str) -> int:
    root = Path(temp_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return shutil.disk_usage(root).free


def byte_bucket(value: int) -> str:
    mib = max(0, value) / (1024 * 1024)
    if mib < 64:
        return "under_64mb"
    if mib < 128:
        return "64_127mb"
    if mib < 256:
        return "128_255mb"
    if mib < 384:
        return "256_383mb"
    return "384mb_plus"


def queue_depth_bucket(depth: int) -> str:
    if depth <= 0:
        return "empty"
    if depth == 1:
        return "one"
    if depth <= 3:
        return "two_to_three"
    return "four_plus"


def latency_bucket(milliseconds: int) -> str:
    if milliseconds < 1_000:
        return "under_1s"
    if milliseconds < 5_000:
        return "1_4s"
    if milliseconds < 15_000:
        return "5_14s"
    if milliseconds < 60_000:
        return "15_59s"
    return "60s_plus"
