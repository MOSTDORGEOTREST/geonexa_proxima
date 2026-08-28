"""End-to-end ingestion orchestration."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from geonexa_proxima.domain import CollectedItem, StoredItem
from geonexa_proxima.ports import (
    Analyzer,
    Collector,
    Embedder,
    ItemRepository,
    Ranker,
    Reranker,
    VectorStore,
)
from geonexa_proxima.services.deduplication import cosine_similarity, deduplicate_items
from geonexa_proxima.services.normalization import normalize_item


@dataclass(slots=True)
class IngestionStats:
    collected: int = 0
    normalized: int = 0
    deduplicated: int = 0
    created: int = 0
    existing: int = 0
    embedded: int = 0
    profile_matches: int = 0
    ranked: int = 0
    analyzed: int = 0
    failures: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class _PendingItem:
    collected: CollectedItem
    stored: StoredItem
    vector: list[float]
    semantic_score: float


class IngestionService:
    """Coordinates ports without depending on concrete infrastructure classes."""

    def __init__(
        self,
        *,
        collectors: Sequence[Collector],
        repository: ItemRepository,
        embedder: Embedder,
        vector_store: VectorStore,
        ranker: Ranker,
        analyzer: Analyzer,
        profile_text: str,
        reranker: Reranker | None = None,
        semantic_threshold: float = 0.45,
        deep_analysis_threshold: float = 8.0,
        embedding_batch_size: int = 16,
    ) -> None:
        self.collectors = tuple(collectors)
        self.repository = repository
        self.embedder = embedder
        self.vector_store = vector_store
        self.ranker = ranker
        self.analyzer = analyzer
        self.profile_text = profile_text.strip()
        self.reranker = reranker
        self.semantic_threshold = semantic_threshold
        self.deep_analysis_threshold = deep_analysis_threshold
        self.embedding_batch_size = embedding_batch_size

    async def ingest(
        self,
        *,
        since: datetime | None = None,
        lookback_hours: int = 30,
        limit_per_source: int = 200,
    ) -> IngestionStats:
        stats = IngestionStats()
        since = since or datetime.now(UTC) - timedelta(hours=lookback_hours)
        raw_items = await self._collect(since, limit_per_source, stats)
        stats.collected = len(raw_items)
        normalized = [normalize_item(item) for item in raw_items if item.title.strip()]
        stats.normalized = len(normalized)
        unique = deduplicate_items(normalized)
        stats.deduplicated = len(normalized) - len(unique)

        # Save every source record so duplicate provenance is not lost. Select one
        # richest representative per canonical item for the expensive ML stages.
        candidates: dict[UUID, tuple[CollectedItem, StoredItem]] = {}
        for item in normalized:
            try:
                stored, was_created = await self.repository.save_collected(item)
            except Exception as exc:
                stats.failures[f"repository:{item.source}:{item.external_id}"] = str(exc)
                continue
            if was_created:
                stats.created += 1
            else:
                stats.existing += 1
            if was_created or stored.rank is None:
                previous = candidates.get(stored.id)
                if previous is None or len(item.embedding_text) > len(previous[0].embedding_text):
                    candidates[stored.id] = (item, stored)

        if not candidates:
            return stats

        await self.vector_store.ensure_collection(self.embedder.dimensions)
        profile_vector = (
            await self.embedder.embed_query(self.profile_text) if self.profile_text else None
        )
        pending = await self._embed(list(candidates.values()), profile_vector, stats)
        await self._upsert_vectors(pending)
        await asyncio.gather(
            *(
                self.repository.set_semantic_score(item.stored.id, item.semantic_score)
                for item in pending
            )
        )

        matched = [item for item in pending if item.semantic_score >= self.semantic_threshold]
        stats.profile_matches = len(matched)
        if self.reranker and matched:
            rerank_scores = await self.reranker.score(
                self.profile_text,
                [item.collected.embedding_text for item in matched],
            )
            if len(rerank_scores) != len(matched):
                raise ValueError("Reranker returned a different number of scores than documents")
            for item, score in zip(matched, rerank_scores, strict=True):
                item.semantic_score = 0.7 * item.semantic_score + 0.3 * _unit_score(score)

        for pending_item in matched:
            item = pending_item.collected
            item_id = pending_item.stored.id
            try:
                await self.repository.set_semantic_score(item_id, pending_item.semantic_score)
                rank = await self.ranker.rank(item, pending_item.semantic_score)
                await self.repository.set_rank(item_id, rank)
                stats.ranked += 1
                if rank.recommend_deep_analysis or rank.total_score >= self.deep_analysis_threshold:
                    analysis = await self.analyzer.analyze(item, rank)
                    await self.repository.set_analysis(item_id, analysis)
                    stats.analyzed += 1
            except Exception as exc:
                stats.failures[f"ranking:{item_id}"] = str(exc)
        return stats

    async def _collect(
        self, since: datetime, limit: int, stats: IngestionStats
    ) -> list[CollectedItem]:
        results = await asyncio.gather(
            *(collector.collect(since, limit) for collector in self.collectors),
            return_exceptions=True,
        )
        items: list[CollectedItem] = []
        for collector, result in zip(self.collectors, results, strict=True):
            if isinstance(result, BaseException):
                stats.failures[f"collector:{type(collector).__name__}"] = str(result)
            else:
                items.extend(result)
        return items

    async def _embed(
        self,
        created: Sequence[tuple[CollectedItem, StoredItem]],
        profile_vector: Sequence[float] | None,
        stats: IngestionStats,
    ) -> list[_PendingItem]:
        pending: list[_PendingItem] = []
        for index in range(0, len(created), self.embedding_batch_size):
            batch = created[index : index + self.embedding_batch_size]
            vectors = await self.embedder.embed_documents(
                [item.embedding_text for item, _ in batch]
            )
            if len(vectors) != len(batch):
                raise ValueError("Embedder returned a different number of vectors than documents")
            for (item, stored), vector in zip(batch, vectors, strict=True):
                if len(vector) != self.embedder.dimensions:
                    raise ValueError(
                        f"Embedding dimensions mismatch: expected {self.embedder.dimensions}, "
                        f"received {len(vector)}"
                    )
                semantic_score = (
                    cosine_similarity(vector, profile_vector) if profile_vector else 1.0
                )
                pending.append(_PendingItem(item, stored, vector, semantic_score))
                stats.embedded += 1
        return pending

    async def _upsert_vectors(self, pending: Sequence[_PendingItem]) -> None:
        await self.vector_store.upsert(
            [item.stored.id for item in pending],
            [item.vector for item in pending],
            [
                {
                    "title": item.collected.title,
                    "kind": item.collected.kind.value,
                    "source": item.collected.source.value,
                    "external_id": item.collected.external_id,
                }
                for item in pending
            ],
        )


def _unit_score(score: float) -> float:
    if 0 <= score <= 1:
        return score
    return 1 / (1 + math.exp(-max(-60.0, min(60.0, score))))
