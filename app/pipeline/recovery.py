from __future__ import annotations

from dataclasses import dataclass

# Temporary compatibility import; active startup uses app.worker_runtime.cleanup.
from app.worker_runtime.cleanup import cleanup_stale_media


@dataclass(frozen=True)
class RecoveryCapabilities:
    restart_rematerialization: bool = True
    stale_temp_cleanup: bool = True
    expired_lease_reclaim: bool = False
    blocker: str = "lease_owner_expiry_and_attempt_fields_missing"


def recovery_capabilities() -> RecoveryCapabilities:
    return RecoveryCapabilities()
