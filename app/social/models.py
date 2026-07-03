from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class SocialResolverError(Exception):
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


@dataclass(frozen=True)
class SocialVideoInfo:
    platform: str
    resolver: str
    title: str | None
    video_id: str | None
    duration_seconds: float | None
    thumbnail_url: str | None
    webpage_url: str | None
    stream_url: str
    stream_headers: dict[str, str]
    ext: str | None
    mime_type: str | None
    extractor: str | None
    filename: str | None
    uploader: str | None
    filesize_approx: int | None
    raw_metadata: dict[str, Any] | None = None
    description: str | None = None
    width: int | None = None
    height: int | None = None
    thumbnail_source: str | None = None
    thumbnail_provider: str | None = None
    thumbnail_strategy: str | None = None
    thumbnail_candidate_count: int | None = None
    thumbnail_selected: bool | None = None
