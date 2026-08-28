"""Exact, fuzzy, and embedding-based duplicate detection helpers."""

from __future__ import annotations

import math
from collections.abc import Sequence

from geonexa_proxima.domain import CollectedItem
from geonexa_proxima.services.normalization import identity_keys, title_key


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Vectors must have equal dimensions")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def exact_duplicate(left: CollectedItem, right: CollectedItem) -> bool:
    return bool(set(identity_keys(left)) & set(identity_keys(right)))


def fuzzy_title_score(left: str, right: str) -> float:
    """Return a 0..1 title similarity, importing rapidfuzz only when used."""

    from rapidfuzz.fuzz import token_set_ratio

    return float(token_set_ratio(title_key(left), title_key(right))) / 100.0


def likely_duplicate(
    left: CollectedItem,
    right: CollectedItem,
    *,
    fuzzy_threshold: float = 0.94,
    left_embedding: Sequence[float] | None = None,
    right_embedding: Sequence[float] | None = None,
    embedding_threshold: float = 0.985,
) -> bool:
    if exact_duplicate(left, right):
        return True
    if fuzzy_title_score(left.title, right.title) >= fuzzy_threshold:
        return True
    return (
        left_embedding is not None
        and right_embedding is not None
        and cosine_similarity(left_embedding, right_embedding) >= embedding_threshold
    )


def deduplicate_items(
    items: Sequence[CollectedItem], *, fuzzy_threshold: float = 0.94
) -> list[CollectedItem]:
    """Deduplicate a small batch, preserving the first provider result."""

    unique: list[CollectedItem] = []
    seen_keys: set[str] = set()
    for item in items:
        keys = set(identity_keys(item))
        if keys & seen_keys:
            continue
        if any(
            fuzzy_title_score(item.title, candidate.title) >= fuzzy_threshold
            for candidate in unique
        ):
            continue
        unique.append(item)
        seen_keys.update(keys)
    return unique
