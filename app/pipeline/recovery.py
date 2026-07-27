from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.media_resolvers.media_fetch import sweep_stale_job_directories


@dataclass(frozen=True)
class RecoveryCapabilities:
    restart_rematerialization: bool = True
    stale_temp_cleanup: bool = True
    expired_lease_reclaim: bool = False
    blocker: str = "lease_owner_expiry_and_attempt_fields_missing"


def cleanup_stale_media(settings: Settings) -> int:
    return sweep_stale_job_directories(
        settings.temp_media_dir,
        older_than_seconds=settings.max_job_runtime_seconds + 120,
    )


def recovery_capabilities() -> RecoveryCapabilities:
    return RecoveryCapabilities()

