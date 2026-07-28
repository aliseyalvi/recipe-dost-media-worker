from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import GeminiRecipeResult, MediaJobRecord

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
    # The durable job row is authoritative. This snapshot is an in-process
    # optimization and is deliberately omitted from serialization.
    runtime_job: MediaJobRecord | None = Field(default=None, exclude=True)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class IngestedMedia(BaseModel):
    dispatch: DispatchEnvelope
    normalized_platform: str
    resolver_category: str
    media_mode: MediaMode = "unknown"
    output_mode: str
    size_category: str
    duration_category: str
    dimension_category: str
    source_context: SourceContext
    cleanup_obligations: list[str] = Field(default_factory=list)
    timestamps: StageTimestamps
    local_media_path: str | None = Field(default=None, exclude=True)
    thumbnail_url: str | None = Field(default=None, exclude=True)
    runtime_extract_result: dict[str, object] | None = Field(
        default=None,
        exclude=True,
    )

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
    source_context: SourceContext
    cleanup_obligations: list[str] = Field(default_factory=list)
    timestamps: StageTimestamps
    local_media_path: str | None = Field(default=None, exclude=True)
    thumbnail_url: str | None = Field(default=None, exclude=True)
    runtime_source_metadata: dict[str, object] | None = Field(
        default=None,
        exclude=True,
    )
    runtime_client: Any = Field(default=None, exclude=True)
    terminal_job: MediaJobRecord | None = Field(default=None, exclude=True)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class FinalizationInput(BaseModel):
    analysis: GeminiAnalysisResult
    timestamps: StageTimestamps = Field(
        default_factory=lambda: StageTimestamps(queued_at=utc_now())
    )
