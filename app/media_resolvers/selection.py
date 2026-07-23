from __future__ import annotations

from collections import Counter
from urllib.parse import urlparse

from app.media_resolvers.contracts import (
    CompatibilityState,
    MediaCandidate,
    ResolverError,
)

H264_CODECS = frozenset({"h264", "avc", "avc1"})
MP4_CONTAINERS = frozenset({"mp4", "m4v"})
UNKNOWN_DECLARATIONS = frozenset({"", "unknown", "und", "none", "null", "n/a"})


def select_video_candidate(
    candidates: tuple[MediaCandidate, ...],
    *,
    preferred_bytes: int,
    hard_limit_bytes: int,
    max_short_edge: int = 720,
    allowed_host_suffixes: tuple[str, ...] = (),
    enforce_max_short_edge: bool = True,
) -> MediaCandidate:
    ranked = rank_video_candidates(
        candidates,
        preferred_bytes=preferred_bytes,
        hard_limit_bytes=hard_limit_bytes,
        max_short_edge=max_short_edge,
        allowed_host_suffixes=allowed_host_suffixes,
        enforce_max_short_edge=enforce_max_short_edge,
    )
    if not ranked:
        raise ResolverError(
            "unsupported_media",
            "The resolver did not return a compatible H.264 MP4 video.",
            fallback_allowed=True,
            failure_stage="candidate_selection",
        )
    return ranked[0]


def rank_video_candidates(
    candidates: tuple[MediaCandidate, ...],
    *,
    preferred_bytes: int,
    hard_limit_bytes: int,
    max_short_edge: int = 720,
    allowed_host_suffixes: tuple[str, ...] = (),
    prefer_smallest_compatible: bool = False,
    enforce_max_short_edge: bool = True,
) -> list[MediaCandidate]:
    supported, _rejections = evaluate_video_candidates(
        candidates,
        hard_limit_bytes=hard_limit_bytes,
        max_short_edge=max_short_edge,
        allowed_host_suffixes=allowed_host_suffixes,
        enforce_max_short_edge=enforce_max_short_edge,
    )

    def rank(candidate: MediaCandidate) -> tuple[int, int, int, int, int, int]:
        size = candidate.size_bytes
        within_preferred = size is not None and size <= preferred_bytes
        short_edge = _short_edge(candidate) or 0
        known_size = size is not None
        compatibility = declared_video_compatibility(candidate)
        quality_rank = -short_edge if within_preferred else (size or hard_limit_bytes + 1)
        size_or_unknown = size if size is not None else hard_limit_bytes + 1
        return (
            0 if compatibility == "compatible_declared" else 1,
            0 if within_preferred else 1,
            quality_rank,
            size_or_unknown if prefer_smallest_compatible else -(candidate.bitrate or 0),
            0 if known_size else 1,
            -(candidate.bitrate or 0) if prefer_smallest_compatible else -(size or 0),
        )

    return sorted(supported, key=rank)


def evaluate_video_candidates(
    candidates: tuple[MediaCandidate, ...],
    *,
    hard_limit_bytes: int,
    max_short_edge: int,
    allowed_host_suffixes: tuple[str, ...] = (),
    enforce_max_short_edge: bool = True,
) -> tuple[list[MediaCandidate], dict[str, int]]:
    supported: list[MediaCandidate] = []
    rejection_counts: Counter[str] = Counter()
    for candidate in candidates:
        if candidate.media_type != "video":
            rejection_counts["role_not_video"] += 1
            continue
        if (
            (candidate.width is not None and candidate.width <= 0)
            or (candidate.height is not None and candidate.height <= 0)
        ):
            rejection_counts["invalid_dimensions"] += 1
            continue
        short_edge = _short_edge(candidate)
        if (
            enforce_max_short_edge
            and short_edge is not None
            and short_edge > max_short_edge
        ):
            rejection_counts["exceeds_resolution_limit"] += 1
            continue
        if candidate.size_bytes is not None and candidate.size_bytes <= 0:
            rejection_counts["invalid_size"] += 1
            continue
        if candidate.size_bytes is not None and candidate.size_bytes > hard_limit_bytes:
            rejection_counts["exceeds_hard_limit"] += 1
            continue
        compatibility = declared_video_compatibility(candidate)
        codec = _declared_value(candidate.codec)
        container = _declared_value(candidate.container)
        if (
            compatibility == "incompatible_declared"
            and codec not in H264_CODECS | UNKNOWN_DECLARATIONS
        ):
            rejection_counts["unsupported_codec"] += 1
            continue
        if (
            compatibility == "incompatible_declared"
            and container not in MP4_CONTAINERS | UNKNOWN_DECLARATIONS
        ):
            rejection_counts["unsupported_format"] += 1
            continue
        if allowed_host_suffixes and not any(
            _candidate_url_allowed(url, allowed_host_suffixes) for url in candidate.urls()
        ):
            rejection_counts["disallowed_initial_host"] += 1
            continue
        supported.append(candidate)
    return supported, dict(rejection_counts)


def declared_video_compatibility(candidate: MediaCandidate) -> CompatibilityState:
    """Classify declarations only; ffprobe decides actual compatibility."""
    codec = _declared_value(candidate.codec)
    container = _declared_value(candidate.container)
    codec_known = codec not in UNKNOWN_DECLARATIONS
    container_known = container not in UNKNOWN_DECLARATIONS
    if codec_known and codec not in H264_CODECS:
        return "incompatible_declared"
    if container_known and container not in MP4_CONTAINERS:
        return "incompatible_declared"
    if codec_known and container_known:
        return "compatible_declared"
    return "probe_required"


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


def _declared_value(value: str | None) -> str:
    return str(value or "").strip().lower()
