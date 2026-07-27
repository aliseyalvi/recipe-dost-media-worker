from __future__ import annotations

import hashlib
import json

from app.qa.replay import OfflineRepository, run_case, settings_for_case
from app.queue_worker import build_gemini_generation_config


def run_pipeline_parity(case_names: list[str]) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for index, case_name in enumerate(case_names, start=1):
        sequential_repository = OfflineRepository()
        staged_repository = OfflineRepository()
        sequential = run_case(
            case_name,
            repository=sequential_repository,
            index=index,
        )
        staged = run_case(
            case_name,
            repository=staged_repository,
            index=index,
        )
        sequential_events = _analysis_event_shape(sequential_repository)
        staged_events = _analysis_event_shape(staged_repository)
        request_hash = request_shape_parity_hash()
        cases.append(
            {
                "case_name": case_name,
                "sequential_result": sequential["terminal_status"],
                "staged_result": staged["terminal_status"],
                "event_parity": sequential_events == staged_events,
                "request_shape_parity_hash": request_hash,
                "call_count": _total_calls(staged),
                "cleanup": bool(
                    sequential.get("cleanup_success")
                    and staged.get("cleanup_success")
                ),
                "terminal_status": staged["terminal_status"],
            }
        )
    return {
        "mode": "offline_pipeline_parity",
        "cases": cases,
        "success_ratio": (
            round(
                sum(case["terminal_status"] == "completed" for case in cases)
                / len(cases),
                4,
            )
            if cases
            else 0.0
        ),
        "event_parity": all(bool(case["event_parity"]) for case in cases),
        "request_shape_parity": (
            len({case["request_shape_parity_hash"] for case in cases}) <= 1
        ),
        "cleanup": all(bool(case["cleanup"]) for case in cases),
        "terminal": all(
            case["terminal_status"] in {"completed", "failed"} for case in cases
        ),
    }


def request_shape_parity_hash() -> str:
    settings = settings_for_case()
    shape = {
        model: {
            "config_keys": sorted(
                build_gemini_generation_config(
                    settings,
                    model=model,
                    structured_schema=True,
                )
            ),
            "content_shape": ["file", "text"],
        }
        for model in (
            settings.gemini_primary_model,
            settings.gemini_fallback_model,
        )
    }
    return hashlib.sha256(
        json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def _analysis_event_shape(repository: OfflineRepository) -> list[str]:
    return [
        str(event["event_type"])
        for event in repository.events
        if str(event["event_type"]).startswith(
            ("gemini_", "recipe_card_", "recipe_quality_")
        )
    ]


def _total_calls(case: dict[str, object]) -> int:
    values = case.get("call_counts")
    if not isinstance(values, list):
        return 0
    return sum(
        int(item.get("count") or 0)
        for item in values
        if isinstance(item, dict)
    )
