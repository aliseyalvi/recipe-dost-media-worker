from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import GeminiRecipeResult

PipelineStage = Literal["dispatch", "ingest", "analysis", "finalize", "cleanup"]
MediaMode = Literal[
    "youtube_url",
    "progressive",
    "split_av",
    "metadata_only",
    "unknown",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StageTimestamps(BaseModel):
    queued_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class SourceContext(BaseModel):
    """Bounded source context. It is transportable but never event/log metadata."""

    platform: str
    title: str | None = None
    caption: str | None = None
    author: str | None = None
    creator_display_name: str | None = None
    creator_handle: str | None = None
    text_language: str | None = None
    music_title: str | None = None
    music_author: str | None = None
    duration_seconds: int | None = None
    hashtags: list[str] = Field(default_factory=list)
    on_screen_text: list[str] = Field(default_factory=list)


class DispatchEnvelope(BaseModel):
    job_id: UUID
    attempt_ref: str = Field(default_factory=lambda: uuid4().hex)
    platform: str
    lease_state: Literal["claim_without_lease"] = "claim_without_lease"
    timestamps: StageTimestamps = Field(
        default_factory=lambda: StageTimestamps(
            queued_at=utc_now(),
            started_at=utc_now(),
        )
    )


class IngestedMedia(BaseModel):
    dispatch: DispatchEnvelope
    normalized_platform: str
    resolver_category: str
    media_mode: MediaMode = "unknown"
    output_mode: str
    video_bytes_verified: bool = False
    size_bytes: int | None = None
    duration_seconds: int | None = None
    width: int | None = None
    height: int | None = None
    size_category: str
    duration_category: str
    dimension_category: str
    source_context: SourceContext
    cleanup_obligations: list[str] = Field(default_factory=list)
    timestamps: StageTimestamps
    local_media_path: str | None = Field(default=None, exclude=True)
    thumbnail_url: str | None = Field(default=None, exclude=True)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class GeminiAnalysisInput(BaseModel):
    media: IngestedMedia
    gemini_file_reference: str | None = Field(default=None, exclude=True)
    timestamps: StageTimestamps = Field(
        default_factory=lambda: StageTimestamps(queued_at=utc_now())
    )


class GeminiAnalysisResult(BaseModel):
    dispatch: DispatchEnvelope
    normalized_platform: str
    resolver_category: str
    recipe: GeminiRecipeResult | None = None
    model: str | None = None
    used_fallback: bool = False
    brief_text: str | None = None
    source_context: SourceContext
    cleanup_obligations: list[str] = Field(default_factory=list)
    timestamps: StageTimestamps
    local_media_path: str | None = Field(default=None, exclude=True)
    thumbnail_url: str | None = Field(default=None, exclude=True)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class FinalizationInput(BaseModel):
    analysis: GeminiAnalysisResult
    timestamps: StageTimestamps = Field(
        default_factory=lambda: StageTimestamps(queued_at=utc_now())
    )
