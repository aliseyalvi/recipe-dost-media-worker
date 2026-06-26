from __future__ import annotations

from uuid import UUID

import requests

from app.config import Settings
from app.supabase_client import MediaJobRepository

ALLOWED = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def upload_source_thumbnail(
    thumbnail_url: str | None,
    *,
    user_id: UUID,
    media_job_id: UUID,
    repository: MediaJobRepository,
    settings: Settings,
) -> str | None:
    if not thumbnail_url or not settings.use_source_thumbnail_first:
        return None
    response = requests.get(thumbnail_url, timeout=(5, 20), headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in ALLOWED:
        raise ValueError("Unsupported thumbnail type.")
    data = response.content
    if len(data) > 1024 * 1024:
        raise ValueError("Thumbnail too large.")
    path = f"{user_id}/{media_job_id}/thumbnail.{ALLOWED[content_type]}"
    return repository.upload_public_object(settings.thumbnail_bucket, path, data, content_type)
