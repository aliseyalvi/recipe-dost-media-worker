from __future__ import annotations

from urllib.parse import urlparse


def detect_social_platform(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return "unknown"
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return "unknown"
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in {"youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com"}:
        return "youtube"
    if "instagram.com" in host:
        return "instagram"
    if "tiktok.com" in host:
        return "tiktok"
    if "facebook.com" in host or host in {"fb.watch", "www.fb.watch"}:
        return "facebook"
    if parsed.path.lower().endswith((".mp4", ".mov", ".webm", ".m4v")):
        return "direct_media"
    return "unknown"
