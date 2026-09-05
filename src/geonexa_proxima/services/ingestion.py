"""End-to-end ingestion orchestration."""

from __future__ import annotations

import asyncio
import logging
import math
import time
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

#: Сколько записей семантической оценки пишем одновременно. Ограничение не по
#: скорости базы, а по размеру пула соединений: он маленький намеренно, потому
#: что процессов с пулами в проде несколько.
_SCORE_BATCH = 8


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
    #: Сколько раз отказ повторился в каждой области — чтобы «упало один раз»
    #: и «падало на каждом материале» различались в отчёте.
    failure_counts: dict[str, int] = field(default_factory=dict)
    #: Построчный отчёт по каждому источнику: окно, сколько собрал, за сколько
    #: секунд, что упало. Именно он избавляет от похода в код за ответом на
    #: вопрос «почему в этот раз пусто».
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Разбивка по суткам, когда прогон идёт за несколько дней.
    days: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def note_failure(self, scope: str, error: BaseException) -> None:
        """Записать отказ, не раздувая отчёт.

        Ключ — область, а не отдельный материал. Иначе упавший ранжировщик
        добавлял по строке на каждый материал, и в `harvest_runs.stats`
        уезжали тысячи ключей с текстами исключений в одном jsonb-поле,
        которое админка потом читает целиком.
        """

        seen = self.failure_counts.get(scope, 0) + 1
        self.failure_counts[scope] = seen
        text = f"{type(error).__name__}: {error}"
        self.failures[scope] = text if seen == 1 else f"{text} (и ещё {seen - 1})"

    def merge(self, other: IngestionStats) -> None:
        """Присоединить итог ещё одних суток.

        Прогон за несколько дней — это несколько независимых проходов по одному
        и тому же конвейеру. Складывать их вручную в трёх местах — верный
        способ однажды забыть поле, поэтому сложение живёт здесь.
        """

        for name in (
            "collected",
            "normalized",
            "deduplicated",
            "gate_accepted",
            "gate_borderline",
            "gate_rejected",
            "gate_rescued",
            "created",
            "existing",
            "embedded",
            "profile_matches",
            "ranked",
            "analyzed",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for key, value in other.blocked_by.items():
            self.blocked_by[key] = self.blocked_by.get(key, 0) + value
        for key, value in other.by_source.items():
            self.by_source[key] = self.by_source.get(key, 0) + value
        self.failures.update(other.failures)
        for key, count in other.failure_counts.items():
            self.failure_counts[key] = self.failure_counts.get(key, 0) + count
        for key, report in other.sources.items():
            current = self.sources.get(key)
            if current is None:
                self.sources[key] = dict(report)
                continue
            current["collected"] = current.get("collected", 0) + report.get("collected", 0)
            current["seconds"] = round(current.get("seconds", 0.0) + report.get("seconds", 0.0), 2)
            current["windows"] = current.get("windows", 1) + report.get("windows", 1)
            # Ошибка последних суток важнее успеха предыдущих: она объясняет,
            # почему в корпусе дыра именно здесь.
            if report.get("error"):
                current["error"] = report["error"]
        self.days.extend(other.days)


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
        logger: Any | None = None,
        ranking_concurrency: int = 4,
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
        # Логгер приходит снаружи: во флоу это логгер прогона Prefect, и только
        # тогда строки про источники видно в хвосте прогона, а не в stdout
        # контейнера. Свой запасной — чтобы сервис работал и вне Prefect.
        self.logger = logger or logging.getLogger("geonexa.harvest")
        self.ranking_concurrency = ranking_concurrency

    async def ingest(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        lookback_hours: int = 30,
        limit_per_source: int = 200,
        label: str | None = None,
    ) -> IngestionStats:
        stats = IngestionStats()
        since = since or datetime.now(UTC) - timedelta(hours=lookback_hours)
        raw_items = await self._collect(since, until, limit_per_source, stats, label)
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
                stats.note_failure(f"repository:{item.source.value}", exc)
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
        # Пачками, а не тысячей задач разом. Каждая берёт соединение из пула,
        # а пул в проде — два соединения без overflow с таймаутом ожидания в
        # пятнадцать секунд: на хорошем улове последние задачи не дожидались
        # очереди, `gather` без `return_exceptions` ронял весь проход, и сутки
        # закрывались неудачей уже после того, как материалы сохранены.
        for index in range(0, len(pending), _SCORE_BATCH):
            batch = pending[index : index + _SCORE_BATCH]
            await asyncio.gather(
                *(
                    self.repository.set_semantic_score(item.stored.id, item.semantic_score)
                    for item in batch
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

        # Оценка идёт параллельно, но с потолком: при широком гейте за сутки
        # доходит до тысячи материалов, и по одному (5-10 с на вызов) сутки
        # ранжировались бы часами. Потолок — `LIGHT_LLM_CONCURRENCY`; пул
        # соединений с базой при этом не страдает: запись после оценки короткая.
        semaphore = asyncio.Semaphore(max(1, self.ranking_concurrency))

        async def score(pending_item: _PendingItem) -> None:
            item = pending_item.collected
            item_id = pending_item.stored.id
            async with semaphore:
                try:
                    await self.repository.set_semantic_score(item_id, pending_item.semantic_score)
                    rank = await self.ranker.rank(item, pending_item.semantic_score)
                    await self.repository.set_rank(item_id, rank)
                    stats.ranked += 1
                    if (
                        rank.recommend_deep_analysis
                        or rank.total_score >= self.deep_analysis_threshold
                    ):
                        analysis = await self.analyzer.analyze(item, rank)
                        await self.repository.set_analysis(item_id, analysis)
                        stats.analyzed += 1
                except Exception as exc:
                    stats.note_failure("ranking", exc)

        await asyncio.gather(*(score(pending_item) for pending_item in matched))
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
            except Exception as error:
                # Молча терять журнал нельзя: по нему калибруют пороги и
                # находят мёртвые термины, а «пусто» и «не доехало» на экране
                # выглядят одинаково.
                self.logger.warning(
                    "Журнал %s не сброшен: %s: %s",
                    type(sink).__name__,
                    type(error).__name__,
                    error,
                )
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
        self,
        since: datetime,
        until: datetime | None,
        limit: int,
        stats: IngestionStats,
        label: str | None = None,
    ) -> list[CollectedItem]:
        """Опросить источники и рассказать в логе, что с каждым произошло.

        Источники опрашиваются параллельно, поэтому строки в логе идут не по
        порядку списка, а по порядку ответов. Каждая строка называет источник
        целиком — иначе в хвосте прогона они неразличимы.
        """

        plan = await self._collection_plan(since, until)
        # Подпись окна приходит сверху: границы хранятся в UTC, и московская
        # полночь выглядит в них как 21:00 предыдущего дня. Сервис про пояс
        # платформы не знает и знать не должен, а в логе должен стоять тот
        # день, который так называется в отчёте.
        window = label or _window_text(since, until)
        self.logger.info(
            "Сбор: %s, источников %s (%s), лимит %s на источник",
            window,
            len(plan),
            ", ".join(key for _, key, _ in plan) or "нет",
            limit,
        )
        results = await asyncio.gather(
            *(
                self._collect_one(collector, key, start, until, limit, stats, window)
                for collector, key, start in plan
            )
        )
        items: list[CollectedItem] = []
        for collected in results:
            items.extend(collected)
        self.logger.info("Сбор окончен: %s, всего материалов %s", window, len(items))
        return items

    async def _collect_one(
        self,
        collector: Collector,
        key: str,
        start: datetime,
        until: datetime | None,
        limit: int,
        stats: IngestionStats,
        label: str | None = None,
    ) -> list[CollectedItem]:
        """Один источник: свой замер времени, своя строка в логе, своя ошибка.

        Исключение здесь не выпускается наружу намеренно. Один упавший источник
        не должен уносить с собой остальные три: в корпусе будет дыра по нему
        одному, и она названа в отчёте прогона поимённо.
        """

        window = label or _window_text(start, until)
        report: dict[str, Any] = {"window": window, "collected": 0, "windows": 1}
        stats.sources[key] = report
        self.logger.info("%s: старт, %s", key, window)
        began = time.monotonic()
        try:
            items = list(await collector.collect(start, limit, until))
        except Exception as error:
            seconds = round(time.monotonic() - began, 2)
            # Тип ошибки в тексте не для красоты: «TimeoutError» и
            # «HTTPStatusError: 429» требуют разных действий, а по одному
            # сообщению они часто неотличимы.
            failure = f"{type(error).__name__}: {error}"
            report["seconds"] = seconds
            report["error"] = failure
            stats.failures[f"collector:{key}"] = failure
            self.logger.error("%s: ОШИБКА через %s с — %s", key, seconds, failure)
            return []
        seconds = round(time.monotonic() - began, 2)
        report["seconds"] = seconds
        report["collected"] = len(items)
        stats.by_source[key] = stats.by_source.get(key, 0) + len(items)
        if items:
            self.logger.info("%s: собрано %s за %s с", key, len(items), seconds)
        else:
            # Пусто — не обязательно поломка: за сутки источник мог ничего не
            # выдать. Но предупреждение в логе экономит вечер, когда пусто
            # окажется по всем четырём сразу.
            self.logger.warning("%s: пусто за %s (%s с)", key, window, seconds)
        # Потолок берётся у самого источника: у GitHub это 100 записей на
        # запрос, у OpenAlex 200, и постраничного обхода нет ни у того, ни у
        # другого. Сравнение с общим `MAX_ITEMS_PER_SOURCE` (300) не срабатывало
        # никогда — то есть срезанные сутки проходили молча, ровно тот отказ,
        # ради которого вводилась нарезка по суткам.
        ceiling = min(limit, getattr(collector, "page_limit", limit))
        if len(items) >= ceiling:
            report["truncated"] = True
            report["ceiling"] = ceiling
            self.logger.warning(
                "%s: упёрся в потолок выдачи (%s) за %s — материалов за эти "
                "сутки больше, чем забрано, и хвост потерян",
                key,
                ceiling,
                window,
            )
        await self._advance_cursor(collector, key, items, start)
        return items

    async def _collection_plan(
        self, since: datetime, until: datetime | None
    ) -> list[tuple[Collector, str, datetime]]:
        """С какой точки стартует каждый источник.

        Заданное окно сильнее курсора: прогон за конкретные сутки обязан
        собрать именно эти сутки, иначе догон пропущенных дней превращается в
        лотерею. Курсор остаётся источником старта только для открытого окна —
        разового прогона «от даты и до свежего».
        """

        plan: list[tuple[Collector, str, datetime]] = []
        for collector in self.collectors:
            key = _source_key(collector)
            start = since
            if self.cursors is not None and until is None:
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
        if watermark is not None and watermark > datetime.now(UTC):
            # У Crossref `published-print` регулярно указывает на будущий
            # выпуск. Водяной знак «только растёт», опустить его обратно нечем,
            # и источник после такой записи начинает отвечать пустотой в
            # режиме открытого окна. Будущее не бывает собранным.
            watermark = None
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


def _window_text(since: datetime, until: datetime | None) -> str:
    """Окно человеческими словами — это то, что читают в хвосте прогона."""

    if until is None:
        return f"с {since:%d.%m.%Y %H:%M} и до свежего"
    days = (until - since).total_seconds() / 86400
    if abs(days - 1) < 1e-6 and since.hour == 0 and since.minute == 0:
        return f"сутки {since:%d.%m.%Y}"
    return f"с {since:%d.%m.%Y %H:%M} по {until:%d.%m.%Y %H:%M}"


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
