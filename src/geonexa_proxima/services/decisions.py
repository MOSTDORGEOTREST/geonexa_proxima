"""Журнал решений гейта.

Без него настройка порогов превращается в гадание: видно, сколько материалов
отсеяно, но не видно каких и почему. С ним в админке открывается список
отклонённых с указанием сработавшей группы — и термины правят по фактам.

Запись идёт пачками: на прогон приходятся тысячи решений, и вставлять их по
одному значило бы утроить время сбора ради журнала.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class DecisionSink(Protocol):
    async def record(
        self,
        *,
        source: str,
        external_id: str,
        item_id: UUID | None,
        stage: str,
        decision: str,
        keyword_score: float | None,
        semantic_score: float | None,
        matched_terms: dict[str, list[str]],
        blocked_by: str | None,
        title: str | None,
        reason: str | None,
    ) -> None: ...

    async def flush(self) -> None: ...


@dataclass
class NullDecisionSink:
    """Ничего не пишет: для тестов и для HARVEST_STORE_REJECTED=false."""

    recorded: list[dict[str, Any]] = field(default_factory=list)

    async def record(self, **payload: Any) -> None:
        self.recorded.append(payload)

    async def flush(self) -> None:
        return None


class PostgresDecisionSink:
    """Пишет решения в harvest_decisions пачками."""

    INSERT = text(
        """
        INSERT INTO harvest_decisions (
            harvest_run_id, source, external_id, item_id, stage, decision,
            keyword_score, semantic_score, matched_terms, blocked_by, title, reason)
        VALUES (
            :run_id, :source, :external_id, :item_id, :stage, :decision,
            :keyword_score, :semantic_score, CAST(:matched_terms AS jsonb),
            :blocked_by, :title, :reason)
        """
    )

    def __init__(
        self,
        engine: AsyncEngine,
        run_id: UUID,
        *,
        batch_size: int = 200,
        store_rejected: bool = True,
    ) -> None:
        self.engine = engine
        self.run_id = run_id
        self.batch_size = batch_size
        self.store_rejected = store_rejected
        self._buffer: list[dict[str, Any]] = []

    async def record(
        self,
        *,
        source: str,
        external_id: str,
        item_id: UUID | None,
        stage: str,
        decision: str,
        keyword_score: float | None,
        semantic_score: float | None,
        matched_terms: dict[str, list[str]],
        blocked_by: str | None,
        title: str | None,
        reason: str | None,
    ) -> None:
        if decision == "rejected" and not self.store_rejected:
            return
        self._buffer.append(
            {
                "run_id": str(self.run_id),
                "source": source,
                "external_id": external_id[:512],
                "item_id": str(item_id) if item_id else None,
                "stage": stage,
                "decision": decision,
                "keyword_score": keyword_score,
                "semantic_score": semantic_score,
                "matched_terms": json.dumps(matched_terms, ensure_ascii=False),
                "blocked_by": blocked_by,
                "title": (title or "")[:1000] or None,
                "reason": (reason or "")[:1000] or None,
            }
        )
        if len(self._buffer) >= self.batch_size:
            await self.flush()

    async def flush(self) -> None:
        if not self._buffer:
            return
        rows, self._buffer = self._buffer, []
        async with self.engine.begin() as connection:
            await connection.execute(self.INSERT, rows)


class TermHitCounter:
    """Счётчики срабатываний терминов.

    Через месяц работы по ним видно, какие термины не сработали ни разу
    (мёртвый груз) и какие тянут шум. Без счётчиков список терминов чистят
    на ощупь.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._hits: dict[str, int] = {}

    def observe(self, matched_terms: dict[str, list[str]]) -> None:
        for terms in matched_terms.values():
            for term in terms:
                self._hits[term] = self._hits.get(term, 0) + 1

    async def flush(self) -> None:
        if not self._hits:
            return
        rows = [{"term": term, "hits": hits} for term, hits in self._hits.items()]
        self._hits = {}
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE harvest_terms SET hit_count = hit_count + :hits, "
                    "last_hit_at = now() WHERE term = :term"
                ),
                rows,
            )
