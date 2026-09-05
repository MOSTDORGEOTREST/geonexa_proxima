"""Публикации: корпус собранных материалов одним списком.

До этого экрана корпус был виден только через воронку и дайджесты: сколько
собрано — видно, что именно — нет. Здесь всё, что дошло до базы, страницами по
пятьдесят, с поиском по заголовку и аннотации и фильтрами по источнику, виду,
дате и оценке.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text

from geonexa_proxima.api.admin.deps import (
    Admin,
    Engine,
    Paging,
    fetch_all,
    fetch_one,
    page_response,
    require,
    scalar,
)
from geonexa_proxima.domain import ItemKind, SourceName

router = APIRouter(prefix="/items", tags=["admin:items"])

#: Разрешённые сортировки. Строка приходит из адресной строки — подставлять
#: её в ORDER BY напрямую нельзя, поэтому карта, а не f-string.
SORTS: dict[str, str] = {
    "created": "i.created_at DESC",
    "published": "i.publication_date DESC NULLS LAST, i.created_at DESC",
    "score": "i.rank_total_score DESC NULLS LAST, i.created_at DESC",
    "semantic": "i.semantic_score DESC NULLS LAST, i.created_at DESC",
    "citations": "i.citation_count DESC NULLS LAST, i.created_at DESC",
    "title": "i.title ASC",
}

#: Что показывается в строке списка. Аннотация обрезается здесь, а не в
#: интерфейсе: полная уходит только в карточку — пятьдесят аннотаций по три
#: тысячи знаков на страницу списка ни к чему.
_LIST_COLUMNS = """
    i.id, i.kind, i.title, left(i.abstract, 400) AS abstract_short,
    (i.abstract IS NOT NULL AND length(i.abstract) > 400) AS abstract_truncated,
    i.doi, i.arxiv_id, i.canonical_url, i.publication_date, i.venue,
    i.citation_count, i.semantic_score, i.rank_total_score, i.keyword_score,
    i.gate_stage, i.language, i.is_preprint, i.created_at,
    i.ranking ->> 'reason' AS rank_reason,
    i.ranking -> 'categories' AS rank_categories,
    (i.deep_analysis IS NOT NULL) AS analyzed,
    coalesce(
        (SELECT array_agg(DISTINCT s.source ORDER BY s.source)
           FROM item_sources s WHERE s.item_id = i.id),
        ARRAY[]::text[]
    ) AS sources,
    coalesce(
        (SELECT string_agg(a.name, ', ' ORDER BY ia.position)
           FROM item_authors ia JOIN authors a ON a.id = ia.author_id
          WHERE ia.item_id = i.id),
        ''
    ) AS authors
"""


def _filters(
    *,
    q: str | None,
    source: str | None,
    kind: str | None,
    scored: bool | None,
    analyzed: bool | None,
    min_score: float | None,
    date_from: date | None,
    date_to: date | None,
    created_from: date | None,
    created_to: date | None,
    language: str | None = None,
) -> tuple[str, dict[str, Any]]:
    conditions = ["true"]
    params: dict[str, Any] = {}
    if q and q.strip():
        # Простой ILIKE по заголовку и аннотации. Полнотекстовый индекс здесь
        # был бы преждевременным: корпус растёт на сотни строк в месяц, а не
        # на миллионы, и последовательный проход по нему занимает миллисекунды.
        params["q"] = f"%{q.strip()}%"
        conditions.append(
            "(i.title ILIKE :q OR i.abstract ILIKE :q OR i.doi ILIKE :q OR i.venue ILIKE :q"
            " OR EXISTS (SELECT 1 FROM item_authors ia JOIN authors a ON a.id = ia.author_id"
            "            WHERE ia.item_id = i.id AND a.name ILIKE :q))"
        )
    if source:
        if source not in {value.value for value in SourceName}:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=f"Неизвестный источник {source}"
            )
        params["source"] = source
        conditions.append(
            "EXISTS (SELECT 1 FROM item_sources s WHERE s.item_id = i.id AND s.source = :source)"
        )
    if kind:
        if kind not in {value.value for value in ItemKind}:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Неизвестный вид {kind}")
        params["kind"] = kind
        conditions.append("i.kind = :kind")
    if language:
        if language == "unknown":
            conditions.append("i.language IS NULL")
        elif language == "other":
            conditions.append("i.language IS NOT NULL AND i.language NOT IN ('ru', 'en')")
        else:
            params["language"] = language[:8].lower()
            conditions.append("i.language = :language")
    if scored is not None:
        conditions.append(
            "i.rank_total_score IS NOT NULL" if scored else "i.rank_total_score IS NULL"
        )
    if analyzed is not None:
        conditions.append("i.deep_analysis IS NOT NULL" if analyzed else "i.deep_analysis IS NULL")
    if min_score is not None:
        params["min_score"] = min_score
        conditions.append("i.rank_total_score >= :min_score")
    if date_from is not None:
        params["date_from"] = date_from
        conditions.append("i.publication_date >= :date_from")
    if date_to is not None:
        params["date_to"] = date_to
        conditions.append("i.publication_date <= :date_to")
    if created_from is not None:
        params["created_from"] = created_from
        conditions.append("i.created_at >= :created_from")
    if created_to is not None:
        params["created_to"] = created_to
        # Верхняя граница по дате включительно — до полуночи следующего дня.
        conditions.append("i.created_at < CAST(:created_to AS date) + 1")
    return " AND ".join(conditions), params


@router.get("")
async def list_items(
    admin: Admin,
    db: Engine,
    paging: Paging,
    q: str | None = None,
    source: str | None = None,
    kind: str | None = None,
    scored: bool | None = None,
    analyzed: bool | None = None,
    min_score: Annotated[float | None, Query(ge=0, le=10)] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    language: str | None = None,
    sort: str = "created",
) -> dict[str, Any]:
    """Публикации страницами: поиск, фильтры, сортировка."""

    order = SORTS.get(sort)
    if order is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"Сортировка: одна из {', '.join(SORTS)}"
        )
    where, params = _filters(
        q=q,
        source=source,
        kind=kind,
        scored=scored,
        analyzed=analyzed,
        min_score=min_score,
        date_from=date_from,
        date_to=date_to,
        created_from=created_from,
        created_to=created_to,
        language=language,
    )
    total = int(await scalar(db, text(f"SELECT count(*) FROM items i WHERE {where}"), params) or 0)
    rows = await fetch_all(
        db,
        text(
            f"SELECT {_LIST_COLUMNS} FROM items i WHERE {where}"
            f" ORDER BY {order} LIMIT :limit OFFSET :offset"
        ),
        {**params, "limit": paging.limit, "offset": paging.offset},
    )
    return page_response(rows, total, paging)


@router.get("/facets")
async def facets(admin: Admin, db: Engine) -> dict[str, Any]:
    """Сколько чего в корпусе — для подписей фильтров и верхней строки."""

    by_source = await fetch_all(
        db,
        text(
            "SELECT s.source, count(DISTINCT s.item_id) AS n FROM item_sources s"
            " GROUP BY s.source ORDER BY n DESC"
        ),
    )
    by_kind = await fetch_all(
        db, text("SELECT kind, count(*) AS n FROM items GROUP BY kind ORDER BY n DESC")
    )
    by_language = await fetch_all(
        db,
        text(
            "SELECT coalesce(language, 'unknown') AS language, count(*) AS n FROM items"
            " GROUP BY 1 ORDER BY n DESC"
        ),
    )
    totals = await fetch_one(
        db,
        text(
            "SELECT count(*) AS total,"
            " count(*) FILTER (WHERE rank_total_score IS NOT NULL) AS scored,"
            " count(*) FILTER (WHERE deep_analysis IS NOT NULL) AS analyzed,"
            " count(*) FILTER (WHERE created_at >= now() - interval '7 days') AS last_week,"
            " min(publication_date) AS earliest, max(publication_date) AS latest"
            " FROM items"
        ),
    )
    return {
        "sources": by_source,
        "kinds": by_kind,
        "languages": by_language,
        "totals": totals or {},
    }


@router.get("/{item_id}")
async def item_detail(item_id: UUID, admin: Admin, db: Engine) -> dict[str, Any]:
    """Карточка публикации: полная аннотация, оценка, глубокий разбор, источники."""

    row = require(
        await fetch_one(
            db,
            text(
                "SELECT i.*, coalesce((SELECT string_agg(a.name, ', ' ORDER BY ia.position)"
                " FROM item_authors ia JOIN authors a ON a.id = ia.author_id"
                " WHERE ia.item_id = i.id), '') AS authors FROM items i WHERE i.id = :id"
            ),
            {"id": str(item_id)},
        ),
        "Публикация",
    )
    sources = await fetch_all(
        db,
        text(
            "SELECT source, external_id, first_seen_at, last_seen_at FROM item_sources"
            " WHERE item_id = :id ORDER BY first_seen_at"
        ),
        {"id": str(item_id)},
    )
    decisions = await fetch_all(
        db,
        text(
            "SELECT stage, decision, keyword_score, semantic_score, reason, blocked_by,"
            " created_at FROM harvest_decisions WHERE item_id = :id ORDER BY created_at"
        ),
        {"id": str(item_id)},
    )
    return {"item": row, "sources": sources, "decisions": decisions}
