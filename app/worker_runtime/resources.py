from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

_ffmpeg_slots = threading.BoundedSemaphore(1)
_gemini_upload_slots = threading.BoundedSemaphore(1)
_ffmpeg_active = 0
_gemini_upload_active = 0
_ffmpeg_lock = threading.Lock()
_gemini_upload_lock = threading.Lock()


def _configure_ffmpeg_slots(limit: int) -> None:
    global _ffmpeg_slots
    _ffmpeg_slots = threading.BoundedSemaphore(max(1, limit))


def _configure_gemini_upload_slots(limit: int) -> None:
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


def _active_ffmpeg_processes() -> int:
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


def _active_gemini_uploads() -> int:
    with _gemini_upload_lock:
        return _gemini_upload_active
