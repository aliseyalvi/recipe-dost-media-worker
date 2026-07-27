from __future__ import annotations

import os


def standalone_main(stage: str) -> None:
    transport = os.getenv("WORKER_STAGE_TRANSPORT", "in_process").strip().lower()
    if transport != "supabase":
        raise SystemExit(
            f"{stage} worker refused standalone startup: configure an approved "
            "durable cross-process transport first."
        )
    raise SystemExit(
        f"{stage} worker refused standalone startup: SupabaseStageTransport "
        "requires a separately approved additive lease/stage migration."
    )

