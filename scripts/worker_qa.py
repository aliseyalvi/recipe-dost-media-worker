#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from time import perf_counter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_REPORT = PROJECT_ROOT / ".worker-qa" / "latest" / "summary.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic worker QA harness.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("gemini-config")
    gemini_request = subparsers.add_parser("gemini-request")
    gemini_request.add_argument("--mode", choices=("text", "file"), required=True)
    gemini_request.add_argument(
        "--variant",
        choices=("response-schema", "response-json-schema", "both"),
        required=True,
    )

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

    parity = subparsers.add_parser("pipeline-parity")
    parity.add_argument("--manifest", type=Path, required=True)
    parity.add_argument("--report", type=Path, default=DEFAULT_REPORT)

    live = subparsers.add_parser("live")
    live.add_argument("--manifest", type=Path, required=True)
    live.add_argument("--max-total-calls", type=int, required=True)
    live.add_argument("--report", type=Path, default=DEFAULT_REPORT)

    args = parser.parse_args()
    if args.command == "gemini-config":
        return run_gemini_config_diagnostic()
    if args.command == "gemini-request":
        return run_gemini_request_diagnostic(args.mode, args.variant)

    _load_qa_runtime()
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
    elif args.command == "pipeline-parity":
        report = run_pipeline_parity(load_manifest(args.manifest))
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


def run_gemini_config_diagnostic() -> int:
    # Match the production entry point: import the combined worker first, then
    # let the ASGI application import its normal startup dependency graph.
    import app.workers.combined_worker  # noqa: F401
    import app.main  # noqa: F401
    from app.config import Settings
    from app.queue_worker import validate_production_gemini_configs

    settings = Settings(
        _env_file=None,
        worker_api_secret="offline-diagnostic",
        supabase_url="https://offline.invalid",
        supabase_service_role_key="offline-diagnostic",
    )
    result = validate_production_gemini_configs(settings)
    values = {
        "render_git_commit_suffix": _safe_render_commit_suffix(),
        "python_version": platform.python_version(),
        "google_genai_version": result["google_genai_version"],
        "pydantic_version": result["pydantic_version"],
        "pydantic_core_version": result["pydantic_core_version"],
        "primary_ready": _safe_bool(result["primary_ready"]),
        "fallback_ready": _safe_bool(result["fallback_ready"]),
        "gemini_config_ready": _safe_bool(result["gemini_config_ready"]),
        "gemini_request_shape_ready": _safe_bool(
            result["gemini_request_shape_ready"]
        ),
        "gemini_live_canary_verified": result[
            "gemini_live_canary_verified"
        ],
        "schema_mutation_detected": _safe_bool(
            result["schema_mutation_detected"]
        ),
        "primary_configuration_category": result[
            "primary_configuration_category"
        ],
        "fallback_configuration_category": result[
            "fallback_configuration_category"
        ],
        "normalized_error_locations": _safe_list(
            result["normalized_error_locations"]
        ),
        "normalized_error_types": _safe_list(
            result["normalized_error_types"]
        ),
        "provider_call_attempted": "false",
    }
    for key, value in values.items():
        print(f"{key}={value}")
    return 0 if bool(result["ready"]) else 1


def run_gemini_request_diagnostic(mode: str, variant: str) -> int:
    _validate_gemini_request_live_gate()
    from app.ai.gemini_reliability import (
        sdk_failure_evidence,
        validate_generation_config,
    )
    from app.config import Settings
    from app.queue_worker import (
        CANONICAL_RECIPE_PROMPT,
        build_gemini_generation_config,
        build_prepared_gemini_request,
        create_gemini_client,
        extract_gemini_text,
        parse_structured_response,
        upload_gemini_video_file,
        validate_and_normalize_canonical_recipe,
        wait_for_gemini_file_active,
    )

    settings = Settings(
        _env_file=None,
        app_env="development",
        worker_api_secret="owner-live-diagnostic",
        supabase_url="https://offline.invalid",
        supabase_service_role_key="owner-live-diagnostic",
    )
    if settings.gemini_api_key is None:
        raise SystemExit("Gemini request diagnostic requires owner credentials.")
    client = create_gemini_client(
        settings.gemini_api_key.get_secret_value(),
        settings.gemini_file_upload_timeout_seconds,
    )
    variants = (
        ("response-schema", "response_schema"),
        ("response-json-schema", "response_json_schema"),
    )
    selected = (
        variants
        if variant == "both"
        else tuple(item for item in variants if item[0] == variant)
    )
    results: list[dict[str, object]] = [
        {
            "variant": display_variant,
            "config_ready": False,
            "request_shape_ready": False,
            "sdk_invocation_started": False,
            "http_status_observed": False,
            "provider_status_class": "none",
            "response_received": False,
            "structured_result_valid": False,
            "normalized_failure_stage": "none",
            "cleanup_success": False,
        }
        for display_variant, _schema_mode in selected
    ]
    uploaded_file: object | None = None
    cleanup_success = mode == "text"
    input_part: object | None = None
    input_mode = "text"
    try:
        if mode == "file":
            entered_path = getpass.getpass("Local media path: ")
            media_path = Path(entered_path).expanduser()
            if not media_path.is_file() or media_path.stat().st_size <= 0:
                raise ValueError("Local media input is unavailable.")
            uploaded_file = upload_gemini_video_file(
                client=client,
                temp_file_path=str(media_path),
                mime_type="video/mp4",
            )
            uploaded_file = wait_for_gemini_file_active(
                client=client,
                uploaded_file=uploaded_file,
                settings=settings,
            )
            input_part = uploaded_file
            input_mode = "file_api"
            prompt = CANONICAL_RECIPE_PROMPT
        else:
            prompt = (
                CANONICAL_RECIPE_PROMPT
                + "\n\nRecipe evidence: Whisk two eggs with salt. Cook diced "
                "tomato briefly, add the eggs, fold, and serve."
            )
        for result, (_display_variant, schema_mode) in zip(results, selected):
            try:
                validate_generation_config(
                    build_gemini_generation_config(
                        settings,
                        model=settings.gemini_primary_model,
                        structured_schema=True,
                        primary_schema_mode=schema_mode,
                    )
                )
                result["config_ready"] = True
                prepared = build_prepared_gemini_request(
                    settings,
                    model_id=settings.gemini_primary_model,
                    input_part=input_part,
                    prompt=prompt,
                    input_mode=input_mode,
                    primary_schema_mode=schema_mode,
                )
                result["request_shape_ready"] = True
                result["sdk_invocation_started"] = True
                response = client.models.generate_content(
                    model=prepared.model_id,
                    contents=prepared.contents,
                    config=prepared.config,
                )
                result["response_received"] = True
                parsed = parse_structured_response(extract_gemini_text(response))
                _recipe, errors = validate_and_normalize_canonical_recipe(
                    parsed,
                    {"platform": "diagnostic", "title": "Owner diagnostic"},
                    settings,
                )
                result["structured_result_valid"] = not errors
                if errors:
                    result["normalized_failure_stage"] = "response_parse"
            except Exception as exc:
                evidence = sdk_failure_evidence(
                    exc,
                    sdk_invocation_attempted=bool(
                        result["sdk_invocation_started"]
                    ),
                )
                result["http_status_observed"] = evidence[
                    "http_status_observed"
                ]
                result["provider_status_class"] = evidence[
                    "provider_status_class"
                ]
                result["normalized_failure_stage"] = evidence[
                    "failure_stage"
                ]
    except Exception as exc:
        evidence = sdk_failure_evidence(
            exc,
            sdk_invocation_attempted=False,
        )
        for result in results:
            result["http_status_observed"] = evidence[
                "http_status_observed"
            ]
            result["provider_status_class"] = evidence[
                "provider_status_class"
            ]
            result["normalized_failure_stage"] = evidence["failure_stage"]
    finally:
        if uploaded_file is not None:
            try:
                files_api = getattr(client, "files")
                name = getattr(uploaded_file, "name", None)
                if name:
                    try:
                        files_api.delete(name=name)
                    except TypeError:
                        files_api.delete(name)
                    cleanup_success = True
            except Exception:
                cleanup_success = False
        close = getattr(client, "close", None)
        if callable(close):
            close()
    for result in results:
        result["cleanup_success"] = cleanup_success
        for key in (
            "variant",
            "config_ready",
            "request_shape_ready",
            "sdk_invocation_started",
            "http_status_observed",
            "provider_status_class",
            "response_received",
            "structured_result_valid",
            "normalized_failure_stage",
            "cleanup_success",
        ):
            value = result[key]
            print(f"{key}={_safe_bool(value) if isinstance(value, bool) else value}")
    return (
        0
        if all(
            bool(result["structured_result_valid"])
            and bool(result["cleanup_success"])
            for result in results
        )
        else 1
    )


def _validate_gemini_request_live_gate() -> None:
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if branch != "develop":
        raise SystemExit("Gemini request diagnostic requires branch develop.")
    if os.getenv("APP_ENV") != "development":
        raise SystemExit("Gemini request diagnostic requires APP_ENV=development.")
    if os.getenv("ALLOW_LIVE_GEMINI_DIAGNOSTIC", "").lower() != "true":
        raise SystemExit(
            "Gemini request diagnostic requires ALLOW_LIVE_GEMINI_DIAGNOSTIC=true."
        )
    if str(os.getenv("RENDER_GIT_BRANCH") or "").strip().lower() == "main":
        raise SystemExit("Gemini request diagnostic refuses main.")
    if not os.getenv("GEMINI_API_KEY"):
        raise SystemExit("Gemini request diagnostic requires owner credentials.")


def _load_qa_runtime() -> None:
    global Settings
    global OfflineRepository
    global load_manifest
    global make_job
    global run_batch
    global run_case
    global run_staged_batch
    global run_pipeline_parity
    global process_queue_job

    from app.config import Settings as SettingsType
    from app.qa.parity import run_pipeline_parity as parity_runner
    from app.qa.replay import (
        OfflineRepository as Repository,
        load_manifest as manifest_loader,
        make_job as job_factory,
        run_batch as batch_runner,
        run_case as case_runner,
    )
    from app.qa.staged import run_staged_batch as staged_runner
    from app.queue_worker import process_queue_job as queue_processor

    Settings = SettingsType
    OfflineRepository = Repository
    load_manifest = manifest_loader
    make_job = job_factory
    run_batch = batch_runner
    run_case = case_runner
    run_staged_batch = staged_runner
    run_pipeline_parity = parity_runner
    process_queue_job = queue_processor


def _safe_render_commit_suffix() -> str:
    value = str(os.getenv("RENDER_GIT_COMMIT") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8,64}", value):
        return "unavailable"
    return value[-8:].lower()


def _safe_bool(value: object) -> str:
    return "true" if value is True else "false"


def _safe_list(value: object) -> str:
    if not isinstance(value, list):
        return "none"
    safe = [str(item) for item in value if isinstance(item, str) and item]
    return ",".join(safe) if safe else "none"


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
