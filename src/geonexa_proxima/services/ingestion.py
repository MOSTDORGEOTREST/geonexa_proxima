"""End-to-end ingestion orchestration."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from geonexa_proxima.domain import CollectedItem, StoredItem
from geonexa_proxima.harvest import Decision, HarvestMatcher, MatchResult
from geonexa_proxima.ports import (
    Analyzer,
    Collector,
    Embedder,
    ItemRepository,
    Ranker,
    Reranker,
    VectorStore,
)
from geonexa_proxima.services.cursors import SourceCursors
from geonexa_proxima.services.decisions import DecisionSink
from geonexa_proxima.services.deduplication import cosine_similarity, deduplicate_items
from geonexa_proxima.services.normalization import normalize_item


@dataclass(slots=True)
class IngestionStats:
    collected: int = 0
    normalized: int = 0
    deduplicated: int = 0
    # Воронка гейта: accepted идут дальше сразу, borderline ждут проверки
    # эмбеддингом, rejected не доходят ни до одной дорогой стадии.
    gate_accepted: int = 0
    gate_borderline: int = 0
    gate_rejected: int = 0
    gate_rescued: int = 0
    blocked_by: dict[str, int] = field(default_factory=dict)
    # Сколько дал каждый источник — видно, какой перестал отвечать.
    by_source: dict[str, int] = field(default_factory=dict)
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
    gate: MatchResult | None = None


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
        matcher: HarvestMatcher | None = None,
        decision_sink: DecisionSink | None = None,
        semantic_threshold: float = 0.45,
        deep_analysis_threshold: float = 8.0,
        embedding_batch_size: int = 16,
        keyword_threshold: float | None = None,
        store_rejected: bool = True,
        cursors: SourceCursors | None = None,
        term_counter: Any | None = None,
    ) -> None:
        self.collectors = tuple(collectors)
        self.repository = repository
        self.embedder = embedder
        self.vector_store = vector_store
        self.ranker = ranker
        self.analyzer = analyzer
        self.profile_text = profile_text.strip()
        self.reranker = reranker
        self.matcher = matcher
        self.decision_sink = decision_sink
        self.semantic_threshold = semantic_threshold
        self.borderline_semantic_threshold = (
            matcher.profile.borderline_semantic_threshold if matcher else semantic_threshold
        )
        self.deep_analysis_threshold = deep_analysis_threshold
        self.embedding_batch_size = embedding_batch_size
        # None означает «доверять порогу из YAML-профиля»; значение из .env
        # перекрывает его, не трогая файл — так порог калибруется без релиза.
        self.keyword_threshold = keyword_threshold
        # Отклонённые материалы — единственный материал для калибровки порогов
        # и чистки терминов. Без них через месяц не на чем будет считать.
        self.store_rejected = store_rejected
        # Курсоры необязательны: без них сбор работает по фиксированному окну,
        # как раньше, и это нормальный режим для тестов и разовых прогонов.
        self.cursors = cursors
        # Счётчик попаданий терминов. Без него hit_count навсегда остаётся
        # нулём, и экран «мёртвые термины» объявляет мёртвыми все 323.
        self.term_counter = term_counter

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

        # Ключевой гейт стоит здесь намеренно: до эмбеддингов и до LLM.
        # Отсечь мусор арифметикой по строкам стоит нисколько, а прогнать его
        # через модель — стоит времени и денег на каждом прогоне.
        normalized, gates = await self._apply_gate(normalized, stats)
        if not normalized:
            return stats

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
            gate = gates.get(_key(item))
            if gate is not None:
                await self._record_decision(item, gate, item_id=stored.id)

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

        # Спасение borderline: гейт по словам их не пропустил, но семантика
        # может вытащить работу, написанную непривычными словами.
        matched: list[_PendingItem] = []
        for item in pending:
            gate = gates.get(_key(item.collected))
            threshold = self.semantic_threshold
            if gate is not None and gate.decision is Decision.BORDERLINE:
                if item.semantic_score < self.borderline_semantic_threshold:
                    await self._record_decision(
                        item.collected,
                        gate,
                        item_id=item.stored.id,
                        stage="semantic",
                        decision=Decision.REJECTED,
                        semantic_score=item.semantic_score,
                    )
                    continue
                stats.gate_rescued += 1
                await self._record_decision(
                    item.collected,
                    gate,
                    item_id=item.stored.id,
                    stage="semantic",
                    decision=Decision.ACCEPTED,
                    semantic_score=item.semantic_score,
                )
            if item.semantic_score >= threshold:
                matched.append(item)
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

    async def _apply_gate(
        self, items: Sequence[CollectedItem], stats: IngestionStats
    ) -> tuple[list[CollectedItem], dict[tuple[str, str], MatchResult]]:
        """Прогнать материалы через профиль сбора. Без матчера пропускаем всё.

        Отклонённые записываются здесь и только здесь: до репозитория они не
        доходят, а значит и `item_id` у них не будет никогда. Раньше их решения
        терялись целиком, и таблица `harvest_decisions` знала только про то,
        что прошло, — калибровать пороги было не на чем.
        """

        if self.matcher is None:
            return list(items), {}
        kept: list[CollectedItem] = []
        gates: dict[tuple[str, str], MatchResult] = {}
        rejected: list[tuple[CollectedItem, MatchResult]] = []
        for item in items:
            result = self.matcher.match(
                item.title,
                item.abstract,
                item.keywords,
                venue=item.venue,
                threshold=self.keyword_threshold,
            )
            gates[_key(item)] = result
            if self.term_counter is not None and result.matched_terms:
                self.term_counter.observe(result.matched_terms)
            if result.decision is Decision.ACCEPTED:
                stats.gate_accepted += 1
                kept.append(item)
            elif result.decision is Decision.BORDERLINE:
                stats.gate_borderline += 1
                kept.append(item)
            else:
                stats.gate_rejected += 1
                rejected.append((item, result))
                if result.blocked_by:
                    stats.blocked_by[result.blocked_by] = (
                        stats.blocked_by.get(result.blocked_by, 0) + 1
                    )
        if self.store_rejected:
            for item, result in rejected:
                await self._record_decision(item, result)
        return kept, gates

    async def flush_journals(self) -> None:
        """Дописать журнал решений и счётчики терминов.

        Вызывается в конце прогона: обе структуры копят записи пачками, и без
        явного сброса последняя, неполная пачка потерялась бы.
        """

        for sink in (self.decision_sink, self.term_counter):
            flush = getattr(sink, "flush", None)
            if flush is None:
                continue
            try:
                await flush()
            except Exception:
                continue

    async def _record_decision(
        self,
        item: CollectedItem,
        gate: MatchResult,
        *,
        item_id: UUID | None = None,
        stage: str = "keyword",
        decision: Decision | None = None,
        semantic_score: float | None = None,
    ) -> None:
        if self.decision_sink is None:
            return
        await self.decision_sink.record(
            source=item.source.value,
            external_id=item.external_id,
            item_id=item_id,
            stage=stage,
            decision=(decision or gate.decision).value,
            keyword_score=gate.keyword_score,
            semantic_score=semantic_score,
            matched_terms=gate.matched_terms,
            blocked_by=gate.blocked_by,
            title=item.title,
            reason=gate.reason,
        )

    async def _collect(
        self, since: datetime, limit: int, stats: IngestionStats
    ) -> list[CollectedItem]:
        """Опросить источники, каждый — со своей точки остановки.

        Общее окно `since` остаётся запасным вариантом: оно применяется к
        источнику, по которому курсора ещё нет. У остальных старт берётся из
        курсора с нахлёстом — так пропущенный прогон не создаёт дыру в корпусе,
        а штатный не тащит заново то, что уже собрано.
        """

        plan = await self._collection_plan(since)
        results = await asyncio.gather(
            *(collector.collect(start, limit) for collector, _, start in plan),
            return_exceptions=True,
        )
        items: list[CollectedItem] = []
        for (collector, key, start), result in zip(plan, results, strict=True):
            name = type(collector).__name__
            if isinstance(result, BaseException):
                stats.failures[f"collector:{name}"] = str(result)
                continue
            items.extend(result)
            stats.by_source[key] = stats.by_source.get(key, 0) + len(result)
            await self._advance_cursor(collector, key, result, start)
        return items

    async def _collection_plan(self, since: datetime) -> list[tuple[Collector, str, datetime]]:
        plan: list[tuple[Collector, str, datetime]] = []
        for collector in self.collectors:
            key = _source_key(collector)
            start = since
            if self.cursors is not None:
                try:
                    start = await self.cursors.resume_from(key, key, fallback=since)
                except Exception:
                    start = since
            plan.append((collector, key, start))
        return plan

    async def _advance_cursor(
        self,
        collector: Collector,
        key: str,
        items: Sequence[CollectedItem],
        started_from: datetime,
    ) -> None:
        """Продвинуть курсор до самой свежей собранной публикации."""

        if self.cursors is None:
            return
        from geonexa_proxima.services.cursors import newest

        watermark = newest(list(items))
        if watermark is None and not items:
            # Пустой ответ — не повод двигать курсор: источник мог быть
            # недоступен и вернуть ноль вместо ошибки.
            return
        try:
            query_id = await self.cursors.ensure_query(key, key, getattr(collector, "query", key))
            await self.cursors.advance(
                query_id,
                watermark=watermark,
                last_external_id=items[-1].external_id if items else None,
                stats={"collected": len(items), "since": started_from.isoformat()},
            )
        except Exception:
            return

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


def _key(item: CollectedItem) -> tuple[str, str]:
    return item.source.value, item.external_id


def _unit_score(score: float) -> float:
    if 0 <= score <= 1:
        return score
    return 1 / (1 + math.exp(-max(-60.0, min(60.0, score))))


def _source_key(collector: object) -> str:
    """Имя источника для курсора. Берём из самого коллектора, а не из класса."""

    source = getattr(collector, "source", None)
    if source is not None:
        return str(getattr(source, "value", source))
    name = type(collector).__name__.removesuffix("Collector").lower()
    return name or "unknown"
