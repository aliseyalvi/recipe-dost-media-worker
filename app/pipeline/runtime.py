from __future__ import annotations

import asyncio
import logging

from app.config import Settings, get_settings
from app.pipeline.resources import (
    ResourceAdmissionController,
    configure_ffmpeg_slots,
    configure_gemini_upload_slots,
    queue_depth_bucket,
)
from app.pipeline.analyzer import analyzer_loop
from app.pipeline.dispatch import dispatcher_loop
from app.pipeline.finalizer import finalizer_loop
from app.pipeline.ingest import ingest_loop
from app.pipeline.transport import InProcessStageTransport
from app.pipeline.telemetry import PipelineTransitionRecorder

logger = logging.getLogger("recipe_dost.worker")


class CombinedPipeline:
    def __init__(self, settings: Settings) -> None:
        if settings.worker_stage_transport != "in_process":
            raise RuntimeError(
                "Combined worker currently supports only in_process stage transport."
            )
        self.settings = settings
        self.transport = InProcessStageTransport(
            ingest_size=settings.worker_ingest_queue_size,
            analysis_size=settings.worker_analysis_queue_size,
            finalize_size=settings.worker_finalize_queue_size,
        )
        self.stop_event = asyncio.Event()
        self.active_counts = {"ingest": 0, "analysis": 0, "finalize": 0}
        self.transitions = PipelineTransitionRecorder()
        self.tasks: list[asyncio.Task[None]] = []
        self._shutting_down = False

    async def run(self) -> None:
        configure_ffmpeg_slots(self.settings.worker_ffmpeg_concurrency)
        configure_gemini_upload_slots(self.settings.worker_gemini_upload_concurrency)
        logger.info(
            "worker_pipeline_started",
            extra={
                "stage": "pipeline",
                "pipeline_mode": self.settings.worker_pipeline_mode,
                "configured_concurrency": (
                    self.settings.worker_gemini_analysis_concurrency
                ),
                "queue_depth_bucket": "empty",
                "active_stage_count": 0,
                "lease_state_category": "claim_without_lease",
            },
        )
        self.tasks = [
            asyncio.create_task(
                dispatcher_loop(
                    settings=self.settings,
                    transport=self.transport,
                    stop_event=self.stop_event,
                    admission=ResourceAdmissionController(self.settings),
                    active_counts=self.active_counts,
                    transitions=self.transitions,
                ),
                name="worker-dispatcher",
            ),
            *[
                asyncio.create_task(
                    ingest_loop(
                        settings=self.settings,
                        transport=self.transport,
                        active_counts=self.active_counts,
                        transitions=self.transitions,
                    ),
                    name=f"worker-ingest-{index + 1}",
                )
                for index in range(self.settings.worker_ingest_concurrency)
            ],
            *[
                asyncio.create_task(
                    analyzer_loop(
                        settings=self.settings,
                        transport=self.transport,
                        active_counts=self.active_counts,
                        transitions=self.transitions,
                        worker_slot=index + 1,
                    ),
                    name=f"worker-analysis-{index + 1}",
                )
                for index in range(
                    self.settings.worker_gemini_analysis_concurrency
                )
            ],
            *[
                asyncio.create_task(
                    finalizer_loop(
                        settings=self.settings,
                        transport=self.transport,
                        active_counts=self.active_counts,
                        transitions=self.transitions,
                    ),
                    name=f"worker-finalize-{index + 1}",
                )
                for index in range(self.settings.worker_finalize_concurrency)
            ],
            asyncio.create_task(self._watchdog(), name="worker-pipeline-watchdog"),
        ]
        gathered = asyncio.gather(*self.tasks)
        try:
            await asyncio.shield(gathered)
        except asyncio.CancelledError:
            await self.shutdown()
            raise
        except Exception:
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.stop_event.set()
        dispatchers = [
            task for task in self.tasks if task.get_name() == "worker-dispatcher"
        ]
        if dispatchers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*dispatchers, return_exceptions=True),
                    timeout=10,
                )
            except TimeoutError:
                for task in dispatchers:
                    task.cancel()
        try:
            await asyncio.wait_for(
                self.transport.join(),
                timeout=min(30, max(5, self.settings.max_job_runtime_seconds)),
            )
        except TimeoutError:
            logger.warning(
                "worker_pipeline_shutdown_timeout",
                extra={"stage": "pipeline"},
            )
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        logger.info(
            "worker_pipeline_shutdown",
            extra={
                "stage": "pipeline",
                "pipeline_mode": self.settings.worker_pipeline_mode,
                "configured_concurrency": (
                    self.settings.worker_gemini_analysis_concurrency
                ),
                "queue_depth_bucket": queue_depth_bucket(
                    sum(self.transport.depths().values())
                ),
                "active_stage_count": sum(self.active_counts.values()),
                "lease_state_category": "claim_without_lease",
            },
        )

    async def _watchdog(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(5)
            failed = [
                task
                for task in self.tasks
                if task is not asyncio.current_task()
                and task.done()
                and not task.cancelled()
            ]
            if failed:
                exception = failed[0].exception()
                raise RuntimeError("pipeline stage task stopped unexpectedly") from exception


async def run_combined_pipeline() -> None:
    settings = get_settings()
    pipeline = CombinedPipeline(settings)
    await pipeline.run()
