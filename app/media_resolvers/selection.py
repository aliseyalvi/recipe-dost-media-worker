from __future__ import annotations

from collections import Counter
from urllib.parse import urlparse

from app.media_resolvers.contracts import MediaCandidate, ResolverError


def select_video_candidate(
    candidates: tuple[MediaCandidate, ...],
    *,
    preferred_bytes: int,
    hard_limit_bytes: int,
    max_short_edge: int = 720,
    allowed_host_suffixes: tuple[str, ...] = (),
) -> MediaCandidate:
    supported, _rejections = evaluate_video_candidates(
        candidates,
        hard_limit_bytes=hard_limit_bytes,
        max_short_edge=max_short_edge,
        allowed_host_suffixes=allowed_host_suffixes,
    )
    if not supported:
        raise ResolverError(
            "unsupported_media",
            "The resolver did not return a compatible H.264 MP4 video.",
            fallback_allowed=True,
        )

    def rank(candidate: MediaCandidate) -> tuple[int, int, int, int, int]:
        size = candidate.size_bytes
        within_preferred = size is not None and size <= preferred_bytes
        short_edge = _short_edge(candidate) or 0
        known_size = size is not None
        return (
            0 if within_preferred else 1,
            0 if (candidate.codec or "").lower() in {"h264", "avc", "avc1"} else 1,
            -short_edge if within_preferred else (size or hard_limit_bytes + 1),
            0 if known_size else 1,
            size or hard_limit_bytes + 1,
        )

    return min(supported, key=rank)


def evaluate_video_candidates(
    candidates: tuple[MediaCandidate, ...],
    *,
    hard_limit_bytes: int,
    max_short_edge: int,
    allowed_host_suffixes: tuple[str, ...] = (),
) -> tuple[list[MediaCandidate], dict[str, int]]:
    supported: list[MediaCandidate] = []
    rejection_counts: Counter[str] = Counter()
    for candidate in candidates:
        if candidate.media_type != "video":
            continue
        short_edge = _short_edge(candidate)
        if short_edge is not None and short_edge > max_short_edge:
            rejection_counts["exceeds_resolution_limit"] += 1
            continue
        if candidate.size_bytes is not None and candidate.size_bytes <= 0:
            rejection_counts["invalid_size"] += 1
            continue
        if candidate.size_bytes is not None and candidate.size_bytes > hard_limit_bytes:
            rejection_counts["exceeds_hard_limit"] += 1
            continue
        codec = (candidate.codec or "").lower()
        container = (candidate.container or "").lower()
        if codec not in {"", "h264", "avc", "avc1"}:
            rejection_counts["unsupported_codec"] += 1
            continue
        if container != "mp4":
            rejection_counts["unsupported_format"] += 1
            continue
        if allowed_host_suffixes and not any(
            _candidate_url_allowed(url, allowed_host_suffixes) for url in candidate.urls()
        ):
            rejection_counts["disallowed_initial_host"] += 1
            continue
        supported.append(candidate)
    return supported, dict(rejection_counts)


def select_audio_candidate(
    candidates: tuple[MediaCandidate, ...],
    *,
    remaining_bytes: int,
) -> MediaCandidate | None:
    supported = [
        candidate
        for candidate in candidates
        if candidate.media_type == "audio"
        and (candidate.size_bytes is None or candidate.size_bytes <= remaining_bytes)
    ]
    if not supported:
        return None
    return max(
        supported,
        key=lambda candidate: (
            candidate.bitrate or 0,
            -(candidate.size_bytes or remaining_bytes),
        ),
    )


def _short_edge(candidate: MediaCandidate) -> int | None:
    if candidate.width and candidate.height:
        return min(candidate.width, candidate.height)
    return candidate.width or candidate.height


def _candidate_url_allowed(url: str, allowed_host_suffixes: tuple[str, ...]) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme == "https" and any(
        host == suffix.lower().lstrip(".")
        or host.endswith("." + suffix.lower().lstrip("."))
        for suffix in allowed_host_suffixes
        if suffix
    )
