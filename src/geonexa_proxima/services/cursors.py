"""Курсоры источников: где мы остановились по каждому запросу.

Без курсоров каждый прогон начинается с фиксированного окна назад
(`COLLECTION_LOOKBACK_HOURS`). Это плохо в обе стороны. Если сервис лежал сутки,
окно короче простоя — и материалы за пропущенный период не будут собраны
никогда. Если сервис работает штатно, окно длиннее нужного — и каждый прогон
заново тащит то, что уже лежит в базе, тратя лимиты источников и время.

Курсор хранит водяной знак: время самого свежего материала, который дошёл до
корпуса. Следующий прогон стартует от него с небольшим перекрытием — источники
публикуют задним числом, и стык окон без нахлёста создаёт дыру.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

#: Нахлёст окон. Источники индексируют публикации с задержкой, и старт ровно с
#: водяного знака гарантированно теряет то, что появилось «задним числом».
DEFAULT_OVERLAP = timedelta(hours=6)

#: Дальше этого назад не отступаем даже после долгого простоя: первый прогон
#: после месячной паузы не должен пытаться скачать месяц целиком.
MAX_CATCHUP = timedelta(days=14)


@dataclass(frozen=True, slots=True)
class Cursor:
    """Позиция по одному запросу источника."""

    query_id: UUID
    source: str
    key: str
    watermark: datetime | None
    last_external_id: str | None
    last_success_at: datetime | None

    def resume_from(
        self,
        *,
        fallback: datetime,
        overlap: timedelta = DEFAULT_OVERLAP,
        max_catchup: timedelta = MAX_CATCHUP,
        now: datetime | None = None,
    ) -> datetime:
        """С какого момента собирать в этот раз."""

        moment = now or datetime.now(UTC)
        if self.watermark is None:
            return fallback
        candidate = self.watermark - overlap
        floor = moment - max_catchup
        # Курсор из будущего означает сбитые часы или ручную правку — доверять
        # ему нельзя, но и падать незачем.
        return min(max(candidate, floor), moment)


class SourceCursors:
    """Чтение и продвижение курсоров. Хранилище — `source_cursors`."""

    def __init__(self, engine: AsyncEngine, *, profile_key: str) -> None:
        self.engine = engine
        self.profile_key = profile_key

    async def ensure_query(self, source: str, key: str, query: str) -> UUID:
        """Найти или завести строку запроса, к которой привязывается курсор."""

        async with self.engine.begin() as connection:
            profile_id = await connection.scalar(
                text("SELECT id FROM harvest_profiles WHERE key = :key"),
                {"key": self.profile_key},
            )
            if profile_id is None:
                raise LookupError(f"Профиль сбора {self.profile_key!r} не найден")
            row = await connection.execute(
                text(
                    "INSERT INTO harvest_queries (id, harvest_profile_id, source, key, query)"
                    " VALUES (gen_random_uuid(), :profile_id, :source, :key, :query)"
                    " ON CONFLICT (harvest_profile_id, source, key) DO UPDATE"
                    " SET query = EXCLUDED.query, updated_at = now()"
                    " RETURNING id"
                ),
                {
                    "profile_id": str(profile_id),
                    "source": source,
                    "key": key,
                    "query": query,
                },
            )
            return row.scalar_one()

    async def load(self, source: str, key: str) -> Cursor | None:
        async with self.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT q.id AS query_id, q.source, q.key, c.cursor,"
                            " c.last_external_id, c.last_success_at"
                            " FROM harvest_queries q"
                            " JOIN harvest_profiles p ON p.id = q.harvest_profile_id"
                            " LEFT JOIN source_cursors c ON c.harvest_query_id = q.id"
                            " WHERE p.key = :profile AND q.source = :source AND q.key = :key"
                        ),
                        {"profile": self.profile_key, "source": source, "key": key},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return Cursor(
            query_id=row["query_id"],
            source=row["source"],
            key=row["key"],
            watermark=_watermark(row["cursor"]),
            last_external_id=row["last_external_id"],
            last_success_at=row["last_success_at"],
        )

    async def resume_from(
        self,
        source: str,
        key: str,
        *,
        fallback: datetime,
        overlap: timedelta = DEFAULT_OVERLAP,
    ) -> datetime:
        """Момент, с которого собирать. Без курсора — окно по умолчанию."""

        cursor = await self.load(source, key)
        if cursor is None:
            return fallback
        return cursor.resume_from(fallback=fallback, overlap=overlap)

    async def advance(
        self,
        query_id: UUID,
        *,
        watermark: datetime | None,
        last_external_id: str | None = None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        """Записать новую позицию после успешного прохода.

        Водяной знак только растёт: неудачный прогон, вернувший меньше данных,
        не должен отбрасывать курсор назад и заставлять собирать заново.
        """

        payload = json.dumps(
            {"watermark": watermark.isoformat() if watermark else None},
            ensure_ascii=False,
        )
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO source_cursors (id, harvest_query_id, cursor,"
                    " last_external_id, last_success_at)"
                    " VALUES (gen_random_uuid(), :query_id, CAST(:cursor AS jsonb),"
                    " :external_id, now())"
                    " ON CONFLICT (harvest_query_id) DO UPDATE SET"
                    " cursor = CASE"
                    "   WHEN EXCLUDED.cursor->>'watermark' IS NULL THEN source_cursors.cursor"
                    "   WHEN source_cursors.cursor->>'watermark' IS NULL THEN EXCLUDED.cursor"
                    "   WHEN (EXCLUDED.cursor->>'watermark')::timestamptz >"
                    "        (source_cursors.cursor->>'watermark')::timestamptz"
                    "     THEN EXCLUDED.cursor"
                    "   ELSE source_cursors.cursor END,"
                    " last_external_id = coalesce(EXCLUDED.last_external_id,"
                    "                             source_cursors.last_external_id),"
                    " last_success_at = now(), updated_at = now()"
                ),
                {
                    "query_id": str(query_id),
                    "cursor": payload,
                    "external_id": last_external_id,
                },
            )
            await connection.execute(
                text(
                    "UPDATE harvest_queries SET last_run_at = now(),"
                    " last_stats = CAST(:stats AS jsonb), updated_at = now() WHERE id = :id"
                ),
                {
                    "id": str(query_id),
                    "stats": json.dumps(stats or {}, ensure_ascii=False, default=str),
                },
            )

    async def overview(self) -> list[dict[str, Any]]:
        """Состояние курсоров для админки: где какой источник остановился."""

        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT q.source, q.key, q.enabled, q.last_run_at, q.last_stats,"
                            " c.cursor->>'watermark' AS watermark, c.last_external_id,"
                            " c.last_success_at FROM harvest_queries q"
                            " JOIN harvest_profiles p ON p.id = q.harvest_profile_id"
                            " LEFT JOIN source_cursors c ON c.harvest_query_id = q.id"
                            " WHERE p.key = :profile ORDER BY q.source, q.key"
                        ),
                        {"profile": self.profile_key},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]


def _watermark(cursor: dict[str, Any] | None) -> datetime | None:
    if not cursor:
        return None
    raw = cursor.get("watermark")
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def newest(items: list[Any]) -> datetime | None:
    """Самая свежая дата публикации в пачке — она и становится водяным знаком."""

    dates = []
    for item in items:
        published = getattr(item, "publication_date", None)
        if published is None:
            continue
        moment = (
            datetime.combine(published, datetime.min.time(), tzinfo=UTC)
            if not isinstance(published, datetime)
            else published
        )
        dates.append(moment if moment.tzinfo else moment.replace(tzinfo=UTC))
    return max(dates) if dates else None
