from __future__ import annotations

import asyncio
from typing import Protocol

from app.pipeline.contracts import (
    DispatchEnvelope,
    FinalizationInput,
    GeminiAnalysisInput,
)


class StageTransport(Protocol):
    """Boundary a future durable transport must implement."""

    def ingest_has_capacity(self) -> bool: ...

    async def enqueue_ingest(self, item: DispatchEnvelope) -> None: ...

    async def dequeue_ingest(self) -> DispatchEnvelope: ...

    async def enqueue_analysis(self, item: GeminiAnalysisInput) -> None: ...

    async def dequeue_analysis(self) -> GeminiAnalysisInput: ...

    async def enqueue_finalize(self, item: FinalizationInput) -> None: ...

    async def dequeue_finalize(self) -> FinalizationInput: ...

    def depths(self) -> dict[str, int]: ...


class InProcessStageTransport:
    def __init__(
        self,
        *,
        ingest_size: int,
        analysis_size: int,
        finalize_size: int,
    ) -> None:
        self._ingest: asyncio.Queue[DispatchEnvelope] = asyncio.Queue(ingest_size)
        self._analysis: asyncio.Queue[GeminiAnalysisInput] = asyncio.Queue(analysis_size)
        self._finalize: asyncio.Queue[FinalizationInput] = asyncio.Queue(finalize_size)

    def ingest_has_capacity(self) -> bool:
        return not self._ingest.full()

    async def enqueue_ingest(self, item: DispatchEnvelope) -> None:
        await self._ingest.put(item)

    async def dequeue_ingest(self) -> DispatchEnvelope:
        return await self._ingest.get()

    async def enqueue_analysis(self, item: GeminiAnalysisInput) -> None:
        await self._analysis.put(item)

    async def dequeue_analysis(self) -> GeminiAnalysisInput:
        return await self._analysis.get()

    async def enqueue_finalize(self, item: FinalizationInput) -> None:
        await self._finalize.put(item)

    async def dequeue_finalize(self) -> FinalizationInput:
        return await self._finalize.get()

    def depths(self) -> dict[str, int]:
        return {
            "ingest": self._ingest.qsize(),
            "analysis": self._analysis.qsize(),
            "finalize": self._finalize.qsize(),
        }

    def task_done(self, stage: str) -> None:
        {
            "ingest": self._ingest,
            "analysis": self._analysis,
            "finalize": self._finalize,
        }[stage].task_done()

    async def join(self) -> None:
        await asyncio.gather(
            self._ingest.join(),
            self._analysis.join(),
            self._finalize.join(),
        )

    async def join_stage(self, stage: str) -> None:
        await {
            "ingest": self._ingest,
            "analysis": self._analysis,
            "finalize": self._finalize,
        }[stage].join()


class SupabaseStageTransport:
    """Reserved durable boundary; intentionally unavailable without a migration."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError(
            "SupabaseStageTransport is not implemented: durable stage/lease "
            "columns require a separately approved additive migration."
        )
