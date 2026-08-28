"""Per-profile candidate retrieval and transparent score fusion."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from geonexa_proxima.config import Settings
from geonexa_proxima.domain import ItemKind, StoredItem
from geonexa_proxima.ports import (
    Embedder,
    ItemRepository,
    ProfileExplainer,
    ProfileVectorStore,
    Reranker,
    VectorStore,
)


class ProfileLike(Protocol):
    id: UUID
    user_id: UUID
    version: int
    compiled_text: str


@dataclass(slots=True)
class PersonalizedItem:
    item: StoredItem
    profile_score_id: UUID
    personal_score: float
    semantic_score: float
    reranker_score: float
    global_score: float
    interest_score: float
    explanation: str = ""


class PersonalizationService:
    """Rank global items for one profile without mutating global scientific scores."""

    def __init__(
        self,
        *,
        settings: Settings,
        item_repository: ItemRepository,
        profile_repository: Any,
        embedder: Embedder,
        item_vectors: VectorStore,
        profile_vectors: ProfileVectorStore,
        reranker: Reranker | None = None,
        explainer: ProfileExplainer | None = None,
    ) -> None:
        self.settings = settings
        self.item_repository = item_repository
        self.profile_repository = profile_repository
        self.embedder = embedder
        self.item_vectors = item_vectors
        self.profile_vectors = profile_vectors
        self.reranker = reranker
        self.explainer = explainer

    async def rank(
        self,
        profile: ProfileLike,
        *,
        limit: int = 20,
        since: datetime | None = None,
        kinds: set[ItemKind] | None = None,
        explain_top: int = 10,
        minimum_global_score: float = 0,
    ) -> list[PersonalizedItem]:
        profile_vector = await self._get_profile_vector(profile)
        candidate_limit = max(limit, self.settings.personalization_candidate_limit)
        global_items, hits = await asyncio.gather(
            self.item_repository.list_digest_candidates(
                minimum_global_score,
                candidate_limit,
                since,
            ),
            self.item_vectors.search(profile_vector, limit=candidate_limit),
        )

        items = {item.id: item for item in global_items}
        missing_ids = [hit.item_id for hit in hits if hit.item_id not in items]
        missing = await asyncio.gather(
            *(self.item_repository.get(item_id) for item_id in missing_ids)
        )
        items.update({item.id: item for item in missing if item is not None})
        if minimum_global_score > 0:
            items = {
                item_id: item
                for item_id, item in items.items()
                if item.rank and item.rank.total_score >= minimum_global_score
            }
        if kinds is not None:
            items = {item_id: item for item_id, item in items.items() if item.kind in kinds}
        if not items:
            return []

        semantic = {hit.item_id: _cosine_to_unit(hit.score) for hit in hits if hit.item_id in items}
        ordered = list(items.values())
        documents = [_item_text(item) for item in ordered]
        reranker_scores = (
            await self.reranker.score(profile.compiled_text, documents)
            if self.reranker
            else [semantic.get(item.id, _baseline_semantic(item)) for item in ordered]
        )
        if len(reranker_scores) != len(ordered):
            raise ValueError("Reranker returned a different number of scores than candidates")

        interests = await self.profile_repository.list_interests(profile.user_id, profile.id)
        signals = await self.profile_repository.list_profile_signals(profile.user_id, profile.id)
        ranked: list[tuple[StoredItem, float, float, float, float, float]] = []
        for item, reranker_score in zip(ordered, reranker_scores, strict=True):
            semantic_score = semantic.get(item.id, _baseline_semantic(item))
            reranker_unit = _clamp(float(reranker_score))
            global_score = _clamp(item.rank.total_score / 10 if item.rank else 0)
            interest_score = _interest_score(item, interests, signals)
            personal_score = (
                self.settings.personal_semantic_weight * semantic_score
                + self.settings.personal_reranker_weight * reranker_unit
                + self.settings.personal_global_weight * global_score
                + self.settings.personal_interest_weight * interest_score
            )
            ranked.append(
                (
                    item,
                    personal_score,
                    semantic_score,
                    reranker_unit,
                    global_score,
                    interest_score,
                )
            )
        ranked.sort(key=lambda row: row[1], reverse=True)

        results: list[PersonalizedItem] = []
        for index, row in enumerate(ranked[:limit]):
            item, personal, semantic_score, reranker_score, global_score, interest_score = row
            explanation = ""
            if self.explainer is not None and index < explain_top:
                try:
                    explanation = await self.explainer.explain(
                        item,
                        profile_text=profile.compiled_text,
                        personal_score=personal,
                    )
                except Exception:
                    explanation = ""
            score = await self.profile_repository.upsert_profile_item_score(
                user_id=profile.user_id,
                profile_id=profile.id,
                item_id=item.id,
                profile_version=profile.version,
                semantic_score=semantic_score,
                reranker_score=reranker_score,
                global_score=global_score,
                interest_score=interest_score,
                personal_score=personal,
                explanation=explanation,
            )
            results.append(
                PersonalizedItem(
                    item=item,
                    profile_score_id=score.id,
                    personal_score=personal,
                    semantic_score=semantic_score,
                    reranker_score=reranker_score,
                    global_score=global_score,
                    interest_score=interest_score,
                    explanation=explanation,
                )
            )
        return results

    async def _get_profile_vector(self, profile: ProfileLike) -> list[float]:
        await self.profile_vectors.ensure_collection(self.embedder.dimensions)
        cached = await self.profile_vectors.get(profile.id, profile.version)
        if cached is not None:
            return cached
        vector = await self.embedder.embed_query(profile.compiled_text)
        await self.profile_vectors.upsert(profile.id, profile.version, vector)
        return vector


def _item_text(item: StoredItem) -> str:
    parts = [item.title]
    if item.abstract:
        parts.append(item.abstract)
    if item.rank and item.rank.categories:
        parts.append("Categories: " + ", ".join(item.rank.categories))
    return "\n\n".join(parts)


def _baseline_semantic(item: StoredItem) -> float:
    return _cosine_to_unit(item.semantic_score) if item.semantic_score is not None else 0


def _cosine_to_unit(score: float) -> float:
    return _clamp((float(score) + 1) / 2)


def _interest_score(
    item: StoredItem,
    interests: Sequence[object],
    signals: Sequence[object],
) -> float:
    haystack = _item_text(item).casefold()
    weighted = 0.0
    maximum = 0.0
    for preference in [*interests, *signals]:
        term = (
            getattr(preference, "query", None)
            or getattr(preference, "topic_name", None)
            or getattr(preference, "term", None)
        )
        if not term:
            continue
        weight = abs(float(getattr(preference, "weight", 1.0)))
        polarity = str(getattr(preference, "polarity", "positive"))
        maximum += weight
        if str(term).casefold() in haystack:
            weighted += -weight if polarity.endswith("negative") else weight
    if maximum == 0:
        return 0.5
    return _clamp(0.5 + 0.5 * weighted / maximum)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
