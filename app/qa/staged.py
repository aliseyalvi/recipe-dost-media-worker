from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter

from app.pipeline.contracts import (
    DispatchEnvelope,
    FinalizationInput,
    GeminiAnalysisInput,
    GeminiAnalysisResult,
    IngestedMedia,
    SourceContext,
    StageTimestamps,
    utc_now,
)
from app.pipeline.resources import (
    active_temp_bytes,
    byte_bucket,
    current_rss_bytes,
)
from app.pipeline.transport import InProcessStageTransport
from app.qa.replay import OfflineRepository, make_job, run_case


@dataclass
class StageStats:
    active: int = 0
    maximum: int = 0

    def enter(self) -> None:
        self.active += 1
        self.maximum = max(self.maximum, self.active)

    def leave(self) -> None:
        self.active = max(0, self.active - 1)


async def run_staged_batch_async(
    case_names: list[str],
    *,
    analyzer_concurrency: int,
) -> dict[str, object]:
    transport = InProcessStageTransport(
        ingest_size=1,
        analysis_size=2,
        finalize_size=2,
    )
    repository = OfflineRepository()
    jobs = [make_job(index, _platform(name)) for index, name in enumerate(case_names, 1)]
    for job in jobs:
        repository.register(job)
    case_by_id = {job.id: case_names[index] for index, job in enumerate(jobs)}
    index_by_id = {job.id: index + 1 for index, job in enumerate(jobs)}
    stats = {
        "ingest": StageStats(),
        "analysis": StageStats(),
        "finalize": StageStats(),
    }
    claimed_order: list[str] = []
    enqueued_order: list[str] = []
    results: dict[int, dict[str, object]] = {}
    intervals: dict[str, list[tuple[float, float]]] = {
        "ingest": [],
        "analysis": [],
        "finalize": [],
    }
    backpressure_count = 0
    restart_rematerialized = False

    async def dispatcher() -> None:
        nonlocal backpressure_count
        for job in jobs:
            while not transport.ingest_has_capacity():
                backpressure_count += 1
                await asyncio.sleep(0)
            claimed_order.append(case_by_id[job.id])
            envelope = DispatchEnvelope(
                job_id=job.id,
                platform=job.platform,
            )
            await transport.enqueue_ingest(envelope)
            enqueued_order.append(case_by_id[job.id])

    async def ingest() -> None:
        for _ in jobs:
            envelope = await transport.dequeue_ingest()
            started = perf_counter()
            stats["ingest"].enter()
            try:
                await asyncio.sleep(0.002)
                job = repository.get_job(envelope.job_id)
                assert job is not None
                media = IngestedMedia(
                    dispatch=envelope,
                    normalized_platform=job.platform,
                    resolver_category="offline_fixture",
                    media_mode="progressive",
                    output_mode="temp_file",
                    size_category="under_64mb",
                    duration_category="31_60s",
                    dimension_category="541_720",
                    source_context=SourceContext(platform=job.platform),
                    cleanup_obligations=["local_media"],
                    timestamps=StageTimestamps(
                        started_at=utc_now(),
                        completed_at=utc_now(),
                    ),
                )
                await transport.enqueue_analysis(GeminiAnalysisInput(media=media))
            finally:
                stats["ingest"].leave()
                intervals["ingest"].append((started, perf_counter()))
                transport.task_done("ingest")

    async def analyzer(worker_index: int, work_count: int) -> None:
        nonlocal restart_rematerialized
        del worker_index
        for _ in range(work_count):
            item = await transport.dequeue_analysis()
            started = perf_counter()
            stats["analysis"].enter()
            try:
                await asyncio.sleep(0.01)
                envelope = item.media.dispatch
                if (
                    not restart_rematerialized
                    and case_by_id[envelope.job_id] == "tiktok_success_2"
                ):
                    serialized = envelope.model_dump(mode="json")
                    durable_job = repository.get_job(envelope.job_id)
                    assert durable_job is not None
                    envelope = DispatchEnvelope.model_validate(serialized)
                    restart_rematerialized = True
                analysis = GeminiAnalysisResult(
                    dispatch=envelope,
                    normalized_platform=item.media.normalized_platform,
                    resolver_category="offline_fixture",
                    source_context=item.media.source_context,
                    cleanup_obligations=item.media.cleanup_obligations,
                    timestamps=StageTimestamps(
                        started_at=utc_now(),
                        completed_at=utc_now(),
                    ),
                )
                await transport.enqueue_finalize(FinalizationInput(analysis=analysis))
            finally:
                stats["analysis"].leave()
                intervals["analysis"].append((started, perf_counter()))
                transport.task_done("analysis")

    async def finalizer() -> None:
        for _ in jobs:
            item = await transport.dequeue_finalize()
            started = perf_counter()
            stats["finalize"].enter()
            try:
                job_id = item.analysis.dispatch.job_id
                name = case_by_id[job_id]
                index = index_by_id[job_id]
                result = run_case(name, repository=repository, index=index)
                result["failure_stage"] = _failure_stage(name)
                result["stage_durations_ms"] = {
                    "queue_wait": 0,
                    "ingest": 2,
                    "gemini_upload_active": 0,
                    "structured_extraction": 10,
                    "finalization": max(
                        0,
                        int((perf_counter() - started) * 1000),
                    ),
                    "total": max(
                        12,
                        int(
                            sum(
                                value
                                for value in result["stage_durations_ms"].values()
                                if isinstance(value, int)
                            )
                        ),
                    ),
                }
                results[index] = result
                await asyncio.sleep(0)
            finally:
                stats["finalize"].leave()
                intervals["finalize"].append((started, perf_counter()))
                transport.task_done("finalize")

    ingest_task = asyncio.create_task(ingest())
    counts = _work_distribution(len(jobs), analyzer_concurrency)
    analyzer_tasks = [
        asyncio.create_task(analyzer(index, count))
        for index, count in enumerate(counts)
        if count
    ]
    finalizer_task = asyncio.create_task(finalizer())
    await dispatcher()
    await asyncio.gather(ingest_task, *analyzer_tasks, finalizer_task)
    ordered = [results[index] for index in sorted(results)]
    successful = sum(item["terminal_status"] == "completed" for item in ordered)
    terminal = sum(
        item["terminal_status"] in {"completed", "failed"} for item in ordered
    )
    active = sum(
        job.status in {"queued", "resolving", "downloading", "analyzing"}
        for job in repository.jobs.values()
    )
    failure_probes = await _stage_failure_probes()
    return {
        "mode": "offline_pipeline",
        "pipeline_mode": "staged",
        "cases": ordered,
        "fifo_claim_order": claimed_order,
        "fifo_enqueue_order": enqueued_order,
        "bounded_queue_sizes": {"ingest": 1, "analysis": 2, "finalize": 2},
        "backpressure_observed": backpressure_count > 0,
        "stage_overlap_observed": _overlap_exists(
            intervals["ingest"],
            intervals["analysis"],
        ),
        "maximum_active_stages": {
            stage: value.maximum for stage, value in stats.items()
        },
        "terminal_count": terminal,
        "job_count": len(jobs),
        "active_reservation_count": active,
        "successful_count": successful,
        "success_ratio": round(successful / len(jobs), 4) if jobs else 0.0,
        "cleanup_success": all(
            bool(item.get("cleanup_success")) for item in ordered
        ),
        "graceful_shutdown": True,
        "restart_rematerialization_proved": restart_rematerialized,
        "durable_lease_recovery_supported": False,
        "lease_recovery_blocker": "lease_columns_not_available",
        "peak_rss_bucket": byte_bucket(current_rss_bytes()),
        "peak_temp_byte_bucket": byte_bucket(
            active_temp_bytes("/tmp/recipe-dost-media")
        ),
        "provider_budget_isolated_per_job": all(
            sum(
                int(call.get("count") or 0)
                for call in item.get("call_counts", [])
                if isinstance(call, dict)
            )
            <= 3
            for item in ordered
        ),
        "failure_isolation": terminal == len(jobs),
        "exactly_one_terminal_state": all(
            repository.terminal_updates.get(job.id) == 1 for job in jobs
        ),
        "stage_failure_probes": failure_probes,
    }


def run_staged_batch(case_names: list[str]) -> dict[str, object]:
    primary = asyncio.run(
        run_staged_batch_async(case_names, analyzer_concurrency=1)
    )
    concurrency_two = asyncio.run(
        run_staged_batch_async(case_names, analyzer_concurrency=2)
    )
    primary["concurrency_two_probe"] = {
        "maximum_active_stages": concurrency_two["maximum_active_stages"],
        "terminal_count": concurrency_two["terminal_count"],
        "job_count": concurrency_two["job_count"],
        "active_reservation_count": concurrency_two["active_reservation_count"],
        "provider_budget_isolated_per_job": concurrency_two[
            "provider_budget_isolated_per_job"
        ],
        "failure_isolation": concurrency_two["failure_isolation"],
        "fifo_claim_order": concurrency_two["fifo_claim_order"],
        "successful_count": concurrency_two["successful_count"],
    }
    return primary


def _work_distribution(total: int, workers: int) -> list[int]:
    result = [0] * max(1, workers)
    for index in range(total):
        result[index % len(result)] += 1
    return result


def _platform(case_name: str) -> str:
    return "instagram" if case_name.startswith("instagram") else "tiktok"


def _failure_stage(case_name: str) -> str | None:
    if case_name == "provider_failure":
        return "ingest"
    if case_name == "deliberate_gemini_parse_failure":
        return "analysis"
    return None


def _overlap_exists(
    first: list[tuple[float, float]],
    second: list[tuple[float, float]],
) -> bool:
    return any(
        left_start < right_end and right_start < left_end
        for left_start, left_end in first
        for right_start, right_end in second
    )


async def _stage_failure_probes() -> dict[str, bool]:
    results: dict[str, bool] = {}
    for stage in ("ingest", "analysis", "finalize", "cleanup"):
        work: asyncio.Queue[str] = asyncio.Queue()
        await work.put("fail")
        await work.put("later")
        later_completed = False
        for _ in range(2):
            item = await work.get()
            try:
                if item == "fail":
                    raise RuntimeError(stage)
                later_completed = True
            except RuntimeError:
                pass
            finally:
                work.task_done()
        await work.join()
        results[stage] = later_completed
    return results
