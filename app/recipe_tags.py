from __future__ import annotations

import re
from collections.abc import Iterable

MIN_RECIPE_TAGS = 5
MAX_RECIPE_TAGS = 8
MAX_RECIPE_TAG_LENGTH = 24

# These values do not help a person find or categorize a recipe. Source handles
# and URL-like values are also excluded so model output cannot turn into a raw
# source identifier in the public recipe card.
_DISALLOWED_TAGS = {"food", "recipe", "video"}
_URL_OR_HANDLE_PATTERN = re.compile(r"(?:https?://|www\.|@|/)", re.IGNORECASE)


def normalize_recipe_tags(values: Iterable[object] | object) -> list[str]:
    """Return safe, searchable recipe tags in stable input order.

    This helper deliberately does not invent tags: Gemini's existing structured
    response supplies the semantic categories, while the worker enforces the
    storage contract before a successful AI card is persisted.
    """
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        tag = re.sub(r"\s+", " ", value.strip().lstrip("#")).lower()
        if (
            not tag
            or len(tag) > MAX_RECIPE_TAG_LENGTH
            or tag in _DISALLOWED_TAGS
            or _URL_OR_HANDLE_PATTERN.search(tag)
        ):
            continue
        if tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
        if len(normalized) == MAX_RECIPE_TAGS:
            break
    return normalized


def has_required_recipe_tags(tags: list[str]) -> bool:
    return MIN_RECIPE_TAGS <= len(tags) <= MAX_RECIPE_TAGS
