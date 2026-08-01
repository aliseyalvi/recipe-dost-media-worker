from __future__ import annotations

from app.config import Settings
from app.media_resolvers.media_fetch import sweep_stale_job_directories


def cleanup_stale_media(settings: Settings) -> int:
    return sweep_stale_job_directories(
        settings.temp_media_dir,
        older_than_seconds=settings.max_job_runtime_seconds + 120,
    )
