from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProcessJobRequest(BaseModel):
    media_job_id: UUID


class HealthResponse(BaseModel):
    ok: Literal[True] = True
    service: Literal["recipe-dost-media-worker"] = "recipe-dost-media-worker"
    mode: Literal["skeleton"] = "skeleton"


class MediaJobData(BaseModel):
    media_job_id: UUID
    status: str
    progress: int = Field(ge=0, le=100)


class ProcessJobResponse(BaseModel):
    ok: Literal[True] = True
    data: MediaJobData


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    ok: Literal[False] = False
    error: ErrorDetail


class MediaJobRecord(BaseModel):
    id: UUID
    status: str
    progress: int = Field(ge=0, le=100)

    model_config = ConfigDict(extra="ignore")
