"""Отчёт по источникам: что видно в логе прогона, не заглядывая в код.

Смысл этих проверок не в счётчиках, а в том, что после прогона можно ответить
на три вопроса по каждому источнику: какое окно он получил, сколько принёс и
чем закончил. Раньше ответ был только в коде.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from geonexa_proxima.domain import CollectedItem, RankResult, SourceName, StoredItem
from geonexa_proxima.services.ingestion import IngestionService

WINDOW_START = datetime(2026, 8, 30, 0, 0, tzinfo=UTC)
WINDOW_END = WINDOW_START + timedelta(days=1)


class _Repo:
    async def save_collected(self, item: CollectedItem) -> tuple[StoredItem, bool]:
        return StoredItem(id=uuid4(), kind=item.kind, title=item.title), True

    async def set_semantic_score(self, item_id, score) -> None: ...
    async def set_rank(self, item_id, rank) -> None: ...
    async def set_analysis(self, item_id, analysis) -> None: ...


class _Embedder:
    @property
    def dimensions(self) -> int:
        return 4

    async def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    async def embed_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]


class _VectorStore:
    async def ensure_collection(self, dimensions: int) -> None: ...
    async def upsert(self, item_ids, vectors, payloads) -> None: ...

    async def search(self, vector, limit: int = 20):
        return []


class _Ranker:
    async def rank(self, item, semantic_score) -> RankResult:
        return RankResult(
            relevance=7,
            novelty=6,
            scientific_quality=7,
            practical_value=7,
            importance_for_geotechnics=7,
            importance_for_ai=6,
            reason="проба",
        )


class _Analyzer:
    async def analyze(self, item, rank):
        raise AssertionError("глубокий разбор в этом тесте не нужен")


class _Source:
    """Источник, который запоминает окно и отдаёт заданное число материалов."""

    def __init__(self, name: SourceName, count: int) -> None:
        self.source = name
        self.count = count
        self.calls: list[tuple[datetime, datetime | None, int]] = []

    async def collect(self, since, limit, until=None) -> list[CollectedItem]:
        self.calls.append((since, until, limit))
        return [
            CollectedItem(
                source=self.source,
                external_id=f"{self.source.value}-{index}",
                title=f"Работа {index} из {self.source.value}",
            )
            for index in range(self.count)
        ]


class _BrokenSource:
    def __init__(self, name: SourceName) -> None:
        self.source = name

    async def collect(self, since, limit, until=None) -> list[CollectedItem]:
        raise TimeoutError("источник не ответил за 30 с")


def _service(collectors, **kwargs) -> IngestionService:
    return IngestionService(
        collectors=collectors,
        repository=_Repo(),
        embedder=_Embedder(),
        vector_store=_VectorStore(),
        ranker=_Ranker(),
        analyzer=_Analyzer(),
        profile_text="",
        semantic_threshold=0.0,
        deep_analysis_threshold=99.0,
        **kwargs,
    )


async def test_window_reaches_every_source() -> None:
    arxiv = _Source(SourceName.ARXIV, 2)
    crossref = _Source(SourceName.CROSSREF, 1)

    await _service([arxiv, crossref]).ingest(
        since=WINDOW_START, until=WINDOW_END, limit_per_source=50
    )

    assert arxiv.calls == [(WINDOW_START, WINDOW_END, 50)]
    assert crossref.calls == [(WINDOW_START, WINDOW_END, 50)]


async def test_report_names_every_source() -> None:
    stats = await _service([_Source(SourceName.ARXIV, 3), _Source(SourceName.GITHUB, 0)]).ingest(
        since=WINDOW_START, until=WINDOW_END, limit_per_source=50, label="сутки 30.08.2026 UTC"
    )

    assert stats.sources["arxiv"]["collected"] == 3
    assert stats.sources["arxiv"]["window"] == "сутки 30.08.2026 UTC"
    assert "seconds" in stats.sources["arxiv"]
    assert stats.sources["github"]["collected"] == 0
    assert stats.by_source == {"arxiv": 3, "github": 0}


async def test_broken_source_does_not_take_the_others_down() -> None:
    stats = await _service(
        [_BrokenSource(SourceName.OPENALEX), _Source(SourceName.ARXIV, 2)]
    ).ingest(since=WINDOW_START, until=WINDOW_END, limit_per_source=50)

    # Живой источник отработал целиком.
    assert stats.sources["arxiv"]["collected"] == 2
    # А упавший назван поимённо, вместе с типом ошибки и её текстом.
    assert "TimeoutError" in stats.sources["openalex"]["error"]
    assert "не ответил" in stats.failures["collector:openalex"]


async def test_failure_is_written_to_the_log(caplog: pytest.LogCaptureFixture) -> None:
    """Ошибку видно в хвосте прогона, а не только в итоговом JSON."""

    logger = logging.getLogger("test.harvest")
    with caplog.at_level(logging.INFO, logger="test.harvest"):
        await _service([_BrokenSource(SourceName.CROSSREF)], logger=logger).ingest(
            since=WINDOW_START, until=WINDOW_END, limit_per_source=50, label="сутки 30.08.2026 UTC"
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("crossref: старт, сутки 30.08.2026 UTC" in message for message in messages)
    assert any("crossref: ОШИБКА" in message and "TimeoutError" in message for message in messages)


async def test_hitting_the_limit_is_reported() -> None:
    """Упёрлись в лимит — значит часть суток осталась за бортом, и это видно."""

    stats = await _service([_Source(SourceName.ARXIV, 5)]).ingest(
        since=WINDOW_START, until=WINDOW_END, limit_per_source=5
    )

    assert stats.sources["arxiv"]["truncated"] is True


async def test_merge_sums_days_without_losing_errors() -> None:
    first = await _service([_Source(SourceName.ARXIV, 2), _BrokenSource(SourceName.GITHUB)]).ingest(
        since=WINDOW_START, until=WINDOW_END, limit_per_source=50
    )
    second = await _service([_Source(SourceName.ARXIV, 3)]).ingest(
        since=WINDOW_END, until=WINDOW_END + timedelta(days=1), limit_per_source=50
    )

    first.merge(second)

    assert first.collected == 5
    assert first.sources["arxiv"]["collected"] == 5
    assert first.sources["arxiv"]["windows"] == 2
    assert "TimeoutError" in first.sources["github"]["error"]
