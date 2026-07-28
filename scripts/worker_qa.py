#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from time import perf_counter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings  # noqa: E402
from app.qa.replay import (  # noqa: E402
    OfflineRepository,
    load_manifest,
    make_job,
    run_batch,
    run_case,
)
from app.qa.staged import run_staged_batch  # noqa: E402
from app.queue_worker import process_queue_job  # noqa: E402

DEFAULT_REPORT = PROJECT_ROOT / ".worker-qa" / "latest" / "summary.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic worker QA harness.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay = subparsers.add_parser("replay")
    replay.add_argument("--case", required=True)
    replay.add_argument("--report", type=Path, default=DEFAULT_REPORT)

    batch = subparsers.add_parser("batch")
    batch.add_argument("--manifest", type=Path, required=True)
    batch.add_argument("--report", type=Path, default=DEFAULT_REPORT)

    pipeline = subparsers.add_parser("pipeline")
    pipeline.add_argument("--manifest", type=Path, required=True)
    pipeline.add_argument(
        "--pipeline-mode",
        choices=("staged",),
        required=True,
    )
    pipeline.add_argument("--report", type=Path, default=DEFAULT_REPORT)

    live = subparsers.add_parser("live")
    live.add_argument("--manifest", type=Path, required=True)
    live.add_argument("--max-total-calls", type=int, required=True)
    live.add_argument("--report", type=Path, default=DEFAULT_REPORT)

    args = parser.parse_args()
    if args.command == "replay":
        report = {
            "mode": "offline_replay",
            "cases": [run_case(args.case)],
        }
        report["success_ratio"] = _success_ratio(report["cases"])
    elif args.command == "batch":
        report = {"mode": "offline_batch", **run_batch(load_manifest(args.manifest))}
    elif args.command == "pipeline":
        report = run_staged_batch(load_manifest(args.manifest))
    else:
        report = run_live(args.manifest, args.max_total_calls)
    write_safe_report(args.report, report)
    print(
        json.dumps(
            {
                "mode": report["mode"],
                "case_count": len(report.get("cases", [])),
                "success_ratio": report.get("success_ratio", 0),
                "report": ".worker-qa/latest/summary.json",
            },
            separators=(",", ":"),
        )
    )
    return 0


def run_live(manifest_path: Path, max_total_calls: int) -> dict[str, object]:
    _validate_live_gate(manifest_path, max_total_calls)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        raise SystemExit("Live manifest must contain at least one case.")
    if not all(
        isinstance(case, dict)
        and case.get("platform") in {"tiktok", "instagram"}
        and isinstance(case.get("source_url"), str)
        and case.get("source_url")
        for case in cases
    ):
        raise SystemExit("Live manifest contains an invalid private case.")

    settings = Settings(_env_file=None)
    settings.gemini_max_calls_per_job = min(
        settings.gemini_max_calls_per_job,
        max_total_calls,
    )
    predicted_calls = len(cases) * (1 + settings.gemini_max_calls_per_job)
    if predicted_calls > max_total_calls:
        raise SystemExit("Explicit live call cap is below the bounded manifest maximum.")

    repository = OfflineRepository()
    results: list[dict[str, object]] = []
    for index, case in enumerate(cases, start=1):
        job = make_job(index, str(case["platform"]))
        job = job.model_copy(update={"source_url": str(case["source_url"])})
        repository.register(job)
        started = perf_counter()
        process_queue_job(
            repository,  # type: ignore[arg-type]
            settings,
            job,
        )
        final = repository.jobs[job.id]
        calls = [
            event
            for event in repository.events
            if event["media_job_id"] == job.id
            and event["event_type"] == "gemini_api_call_started"
        ]
        results.append(
            {
                "case_name": str(case.get("case_name") or f"live_case_{index}"),
                "stage_durations_ms": {
                    "total": max(0, int((perf_counter() - started) * 1000))
                },
                "terminal_status": final.status,
                "normalized_error_category": final.error_code,
                "call_counts": _live_call_counts(calls),
                "queue_wait_bucket": "under_1s",
                "cleanup_success": any(
                    event["media_job_id"] == job.id
                    and event["event_type"] == "temp_video_deleted"
                    for event in repository.events
                ),
                "reservation_created_count": _event_count(
                    repository, job.id, "ai_quota_reservation_created"
                ),
                "reservation_released_count": _event_count(
                    repository, job.id, "ai_quota_reservation_released"
                ),
                "success_committed_count": _event_count(
                    repository, job.id, "ai_quota_success_committed"
                ),
                "finalization_replayed_count": _event_count(
                    repository, job.id, "ai_quota_finalization_replayed"
                ),
            }
        )
    return {
        "mode": "owner_live_development",
        "cases": results,
        "success_ratio": _success_ratio(results),
        "quota_charge": False,
        "supabase_writes": False,
    }


def _validate_live_gate(manifest_path: Path, max_total_calls: int) -> None:
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if branch != "develop":
        raise SystemExit("Live worker QA requires branch develop.")
    if os.getenv("APP_ENV") != "development":
        raise SystemExit("Live worker QA requires APP_ENV=development.")
    if os.getenv("ALLOW_LIVE_WORKER_QA", "").lower() != "true":
        raise SystemExit("Live worker QA requires ALLOW_LIVE_WORKER_QA=true.")
    if max_total_calls <= 0:
        raise SystemExit("Live worker QA requires a positive explicit call cap.")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(manifest_path.resolve())],
        cwd=PROJECT_ROOT,
        check=False,
    ).returncode == 0
    if not ignored:
        raise SystemExit("Live worker QA manifest must be gitignored.")
    if not os.getenv("GEMINI_API_KEY"):
        raise SystemExit("Live worker QA requires owner-provided development credentials.")


def write_safe_report(path: Path, report: dict[str, object]) -> None:
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    lowered = serialized.lower()
    forbidden = (
        "source_url",
        "cdn_url",
        "prompt",
        "raw_response",
        "access_token",
        "api_key",
        "https://",
        "http://",
    )
    if any(marker in lowered for marker in forbidden):
        raise ValueError("Unsafe field detected in worker QA report.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(serialized + "\n", encoding="utf-8")
    temporary.replace(path)


def _event_count(repository: OfflineRepository, job_id: object, event_type: str) -> int:
    return sum(
        event["media_job_id"] == job_id and event["event_type"] == event_type
        for event in repository.events
    )


def _live_call_counts(events: list[dict[str, object]]) -> list[dict[str, object]]:
    counts: dict[tuple[str, str], int] = {}
    for event in events:
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            continue
        key = (
            str(metadata.get("call_purpose") or "unknown"),
            str(metadata.get("model") or "unknown"),
        )
        counts[key] = counts.get(key, 0) + 1
    return [
        {"purpose": purpose, "model": model, "count": count}
        for (purpose, model), count in sorted(counts.items())
    ]


def _success_ratio(cases: list[dict[str, object]]) -> float:
    if not cases:
        return 0.0
    return round(
        sum(case.get("terminal_status") == "completed" for case in cases)
        / len(cases),
        4,
    )


if __name__ == "__main__":
    raise SystemExit(main())
