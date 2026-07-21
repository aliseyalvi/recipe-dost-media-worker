from __future__ import annotations

from app.media_resolvers.contracts import MediaCandidate, ResolverError


def select_video_candidate(
    candidates: tuple[MediaCandidate, ...],
    *,
    preferred_bytes: int,
    hard_limit_bytes: int,
    max_short_edge: int = 720,
) -> MediaCandidate:
    supported: list[MediaCandidate] = []
    for candidate in candidates:
        if candidate.media_type != "video":
            continue
        short_edge = _short_edge(candidate)
        if short_edge is not None and short_edge > max_short_edge:
            continue
        if candidate.size_bytes is not None and candidate.size_bytes > hard_limit_bytes:
            continue
        codec = (candidate.codec or "").lower()
        container = (candidate.container or "").lower()
        # Some Instagram contracts identify MP4 but omit codec metadata. That
        # candidate remains probe-gated. Explicit H.265/DASH remains excluded.
        if codec not in {"", "h264", "avc", "avc1"} or container != "mp4":
            continue
        supported.append(candidate)
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
