from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

MediaMode = Literal["progressive", "split_av", "youtube_url"]
MediaType = Literal["video", "audio"]
AudioState = Literal["present", "absent", "unknown"]


@dataclass(frozen=True)
class MediaResolutionRequest:
    platform: str
    canonical_source_url: str = field(repr=False, compare=False)
    correlation_id: str
    capability: str
    byte_limit: int
    preferred_byte_target: int
    timeout_seconds: int

    def safe_to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "correlation_id": self.correlation_id,
            "capability": self.capability,
            "byte_limit": self.byte_limit,
            "preferred_byte_target": self.preferred_byte_target,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class MediaCandidate:
    media_type: MediaType
    _urls: tuple[str, ...] = field(repr=False, compare=False)
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    codec: str | None = None
    container: str | None = None
    bitrate: int | None = None
    quality_label: str | None = None
    audio_state: AudioState = "unknown"
    candidate_id: str | None = None

    def urls(self) -> tuple[str, ...]:
        return self._urls

    def safe_to_dict(self) -> dict[str, object]:
        return {
            "media_type": self.media_type,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "codec": self.codec,
            "container": self.container,
            "bitrate": self.bitrate,
            "quality_label": self.quality_label,
            "audio_state": self.audio_state,
            "candidate_id": self.candidate_id,
            "url_count": len(self._urls),
        }


@dataclass(frozen=True)
class PublicMetadata:
    title: str | None = field(default=None, repr=False, compare=False)
    author: str | None = field(default=None, repr=False, compare=False)
    thumbnail_url: str | None = field(default=None, repr=False, compare=False)
    duration_seconds: int | None = None

    def safe_to_dict(self) -> dict[str, object]:
        return {
            "title_present": bool(self.title),
            "author_present": bool(self.author),
            "thumbnail_present": bool(self.thumbnail_url),
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True)
class SourceContext:
    caption: str | None = field(default=None, repr=False, compare=False)
    creator_display_name: str | None = field(default=None, repr=False, compare=False)
    creator_handle: str | None = field(default=None, repr=False, compare=False)
    duration_seconds: int | None = None
    text_language: str | None = None
    hashtags: tuple[str, ...] = field(default_factory=tuple, repr=False, compare=False)
    on_screen_text: tuple[str, ...] = field(default_factory=tuple, repr=False, compare=False)
    music_title: str | None = field(default=None, repr=False, compare=False)
    music_author: str | None = field(default=None, repr=False, compare=False)

    def to_prompt_dict(self) -> dict[str, object]:
        values: dict[str, object] = {
            "caption": self.caption or "",
            "creator_display_name": self.creator_display_name or "",
            "creator_handle": self.creator_handle or "",
            "duration_seconds": self.duration_seconds or 0,
            "text_language": self.text_language or "",
            "hashtags": list(self.hashtags),
            "on_screen_text": list(self.on_screen_text),
            "music_title": self.music_title or "",
            "music_author": self.music_author or "",
        }
        return {key: value for key, value in values.items() if value}

    def safe_to_dict(self) -> dict[str, object]:
        caption_length = len(self.caption or "")
        return {
            "caption_present": bool(self.caption),
            "caption_length": caption_length,
            "creator_name_present": bool(self.creator_display_name),
            "creator_handle_present": bool(self.creator_handle),
            "hashtag_count": len(self.hashtags),
            "on_screen_text_present": bool(self.on_screen_text),
            "text_language_present": bool(self.text_language),
            "music_context_present": bool(self.music_title or self.music_author),
            "source_context_attached": bool(self.to_prompt_dict()),
        }


@dataclass(frozen=True)
class MediaResolution:
    adapter_id: str
    media_mode: MediaMode
    candidates: tuple[MediaCandidate, ...]
    metadata: PublicMetadata = field(default_factory=PublicMetadata, repr=False)
    source_context: SourceContext = field(default_factory=SourceContext, repr=False)
    raw_video_candidate_count: int = 0
    raw_audio_candidate_count: int = 0
    normalization_rejection_counts: dict[str, int] = field(
        default_factory=dict, repr=False, compare=False
    )
    contract_diagnostic: dict[str, object] = field(
        default_factory=dict, repr=False, compare=False
    )
    provider_request_id: str | None = None
    rate_limit_remaining: int | None = None
    retry_after_seconds: int | None = None

    def safe_to_dict(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "media_mode": self.media_mode,
            "candidate_count": len(self.candidates),
            "video_candidate_count": sum(
                1 for candidate in self.candidates if candidate.media_type == "video"
            ),
            "audio_candidate_count": sum(
                1 for candidate in self.candidates if candidate.media_type == "audio"
            ),
            "metadata": self.metadata.safe_to_dict(),
            "source_context": self.source_context.safe_to_dict(),
            "raw_video_candidate_count": self.raw_video_candidate_count,
            "raw_audio_candidate_count": self.raw_audio_candidate_count,
            "normalization_rejection_counts": dict(self.normalization_rejection_counts),
            "provider_request_id_present": bool(self.provider_request_id),
            "rate_limit_remaining": self.rate_limit_remaining,
            "retry_after_seconds": self.retry_after_seconds,
        }


class ResolverError(Exception):
    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        retryable: bool = False,
        fallback_allowed: bool = False,
        http_status_class: str | None = None,
        technical: bool = False,
        contract_diagnostic: dict[str, object] | None = None,
        raw_video_candidate_count: int = 0,
        raw_audio_candidate_count: int = 0,
        normalization_rejection_counts: dict[str, int] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.fallback_allowed = fallback_allowed
        self.http_status_class = http_status_class
        self.technical = technical
        self.contract_diagnostic = dict(contract_diagnostic or {})
        self.raw_video_candidate_count = max(0, raw_video_candidate_count)
        self.raw_audio_candidate_count = max(0, raw_audio_candidate_count)
        self.normalization_rejection_counts = dict(
            normalization_rejection_counts or {}
        )


class MediaResolver(Protocol):
    adapter_id: str

    def resolve(self, request: MediaResolutionRequest) -> MediaResolution:
        ...
