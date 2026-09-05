"""Профиль сбора: термины, пороги и — главное — проба гейта на живом тексте."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from geonexa_proxima.api.admin.deps import (
    Admin,
    AppSettings,
    Engine,
    Paging,
    audit,
    execute,
    fetch_all,
    fetch_one,
    page_response,
    require,
    returning,
)

router = APIRouter(prefix="/harvest", tags=["admin:harvest"])


class GateProbe(BaseModel):
    title: str = Field(min_length=1, max_length=1000)
    abstract: str | None = None
    keywords: list[str] = Field(default_factory=list)
    venue: str | None = None
    threshold: float | None = Field(default=None, ge=0, le=1)


class TermIn(BaseModel):
    term: str = Field(min_length=1, max_length=300)
    match_type: str = "phrase"
    weight: float = Field(default=1.0, ge=0, le=10)
    lang: str | None = None
    enabled: bool = True


def _matcher(request: Request, settings: Any) -> Any:
    """Матчер держим на приложении: разбор YAML на каждый запрос не нужен."""

    existing = getattr(request.app.state, "harvest_matcher", None)
    if existing is not None:
        return existing
    path = settings.harvest_config_path
    if not path.is_file():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Профиль сбора не найден: {path}",
        )
    from geonexa_proxima.harvest import HarvestMatcher, load_harvest_profile

    matcher = HarvestMatcher(load_harvest_profile(path))
    request.app.state.harvest_matcher = matcher
    return matcher


@router.post("/test")
async def test_gate(
    payload: GateProbe, admin: Admin, settings: AppSettings, request: Request
) -> dict[str, Any]:
    """Прогнать текст через гейт и показать, что и почему сработало.

    Самый полезный экран при настройке: вставляешь заголовок реальной статьи и
    сразу видишь, какая группа не выполнилась и какой термин её вытащил.
    """

    matcher = _matcher(request, settings)
    threshold = payload.threshold
    if threshold is None:
        threshold = settings.harvest_keyword_threshold
    result = matcher.match(
        payload.title,
        payload.abstract,
        payload.keywords,
        venue=payload.venue,
        threshold=threshold,
    )
    return {
        "decision": result.decision.value,
        "keyword_score": result.keyword_score,
        "satisfied": result.satisfied,
        "blocked_by": result.blocked_by,
        "reason": result.reason,
        "threshold": threshold,
        "matched_terms": result.matched_terms,
    }


@router.get("/profile")
async def profile(admin: Admin, db: Engine, settings: AppSettings) -> dict[str, Any]:
    """Активный профиль сбора вместе с группами и числом терминов."""

    row = require(
        await fetch_one(
            db,
            text("SELECT * FROM harvest_profiles WHERE key = :key"),
            {"key": settings.harvest_profile_key},
        ),
        "Профиль сбора",
    )
    groups = await fetch_all(
        db,
        text(
            "SELECT g.*, (SELECT count(*) FROM harvest_terms t WHERE t.group_id = g.id)"
            " AS terms FROM harvest_term_groups g WHERE g.harvest_profile_id = :id"
            " ORDER BY g.key"
        ),
        {"id": str(row["id"])},
    )
    return {"profile": row, "groups": groups}


@router.post("/profile/resync")
async def resync_profile(
    admin: Admin, db: Engine, settings: AppSettings, request: Request
) -> dict[str, Any]:
    """Перечитать `config/harvest.yaml` в базу и сбросить кэш матчера.

    Матчер собирается из файла, а экран профиля читает базу: после правки
    файла они расходятся, и по экрану нельзя понять, что реально режет
    гейт. Кнопка приводит базу к файлу; правки терминов, сделанные в
    админке поверх файла, при этом теряются — файл здесь источник истины.
    """

    from geonexa_proxima.bootstrap.seed import sync_harvest_profile

    if not settings.harvest_config_path.is_file():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Профиль сбора не найден: {settings.harvest_config_path}",
        )
    try:
        report = await sync_harvest_profile(db, settings)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    request.app.state.harvest_matcher = None
    await audit(db, admin, request, action="harvest.profile.resync", payload=report)
    return report


@router.get("/terms")
async def terms(
    admin: Admin,
    db: Engine,
    paging: Paging,
    group: str | None = None,
    q: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    conditions = ["true"]
    params: dict[str, Any] = {"limit": paging.limit, "offset": paging.offset}
    if group:
        conditions.append("g.key = :group")
        params["group"] = group
    if q:
        conditions.append("t.term ILIKE :q")
        params["q"] = f"%{q}%"
    if enabled is not None:
        conditions.append("t.enabled = :enabled")
        params["enabled"] = enabled
    where = " AND ".join(conditions)
    rows = await fetch_all(
        db,
        text(
            f"SELECT t.id, t.term, t.match_type, t.weight, t.lang, t.enabled, t.hit_count,"
            f" g.key AS group_key FROM harvest_terms t"
            f" JOIN harvest_term_groups g ON g.id = t.group_id"
            f" WHERE {where} ORDER BY t.hit_count DESC, t.term LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    total = await fetch_one(
        db,
        text(
            f"SELECT count(*) AS n FROM harvest_terms t"
            f" JOIN harvest_term_groups g ON g.id = t.group_id WHERE {where}"
        ),
        {k: v for k, v in params.items() if k not in {"limit", "offset"}},
    )
    return page_response(rows, int((total or {}).get("n", 0)), paging)


@router.get("/terms/stats")
async def term_stats(admin: Admin, db: Engine) -> dict[str, Any]:
    """Мёртвые и самые продуктивные термины — материал для чистки профиля."""

    dead = await fetch_all(
        db,
        text(
            "SELECT t.term, g.key AS group_key, t.hit_count FROM harvest_terms t"
            " JOIN harvest_term_groups g ON g.id = t.group_id"
            " WHERE t.enabled AND t.hit_count = 0 ORDER BY g.key, t.term LIMIT 200"
        ),
    )
    top = await fetch_all(
        db,
        text(
            "SELECT t.term, g.key AS group_key, t.hit_count FROM harvest_terms t"
            " JOIN harvest_term_groups g ON g.id = t.group_id"
            " WHERE t.hit_count > 0 ORDER BY t.hit_count DESC LIMIT 50"
        ),
    )
    totals = await fetch_one(
        db,
        text(
            "SELECT count(*) AS total, count(*) FILTER (WHERE enabled) AS enabled,"
            " count(*) FILTER (WHERE hit_count = 0 AND enabled) AS dead FROM harvest_terms"
        ),
    )
    return {"totals": totals, "dead": dead, "top": top}


@router.patch("/terms/{term_id}")
async def patch_term(
    term_id: UUID, payload: TermIn, admin: Admin, db: Engine, request: Request
) -> dict[str, Any]:
    changes = payload.model_dump()
    assignments = ", ".join(f"{key} = :{key}" for key in changes)
    updated = await execute(
        db,
        text(f"UPDATE harvest_terms SET {assignments}, updated_at = now() WHERE id = :id"),
        {**changes, "id": str(term_id)},
    )
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Термин не найден")
    # Матчер держит профиль в памяти — после правки его надо пересобрать.
    request.app.state.harvest_matcher = None
    await audit(
        db,
        admin,
        request,
        action="harvest.term_update",
        entity_type="term",
        entity_id=str(term_id),
        payload=changes,
    )
    return {"updated": True}


@router.delete("/terms/{term_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_term(term_id: UUID, admin: Admin, db: Engine, request: Request) -> None:
    await execute(db, text("DELETE FROM harvest_terms WHERE id = :id"), {"id": str(term_id)})
    request.app.state.harvest_matcher = None
    await audit(
        db,
        admin,
        request,
        action="harvest.term_delete",
        entity_type="term",
        entity_id=str(term_id),
    )


@router.get("/cursors")
async def cursors(admin: Admin, db: Engine, settings: AppSettings) -> list[dict[str, Any]]:
    """Где остановился каждый источник.

    Пустой водяной знак означает, что источник ещё ни разу не отдал материал с
    датой публикации, и следующий прогон пойдёт по окну по умолчанию.
    """

    from geonexa_proxima.services.cursors import SourceCursors

    return await SourceCursors(db, profile_key=settings.harvest_profile_key).overview()


@router.get("/runs")
async def runs(
    admin: Admin,
    db: Engine,
    paging: Paging,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> dict[str, Any]:
    conditions = ["true"]
    params: dict[str, Any] = {"limit": paging.limit, "offset": paging.offset}
    if status_filter:
        conditions.append("status = :status")
        params["status"] = status_filter
    where = " AND ".join(conditions)
    rows = await fetch_all(
        db,
        text(
            f"SELECT id, status, trigger, started_at, finished_at, since, until, "
            f"error, stats, triggered_by"
            f" FROM harvest_runs WHERE {where}"
            f" ORDER BY started_at DESC LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    total = await fetch_one(
        db,
        text(f"SELECT count(*) AS n FROM harvest_runs WHERE {where}"),
        {k: v for k, v in params.items() if k not in {"limit", "offset"}},
    )
    return page_response(rows, int((total or {}).get("n", 0)), paging)


@router.post("/runs/abort")
async def abort_running(admin: Admin, db: Engine, request: Request) -> dict[str, int]:
    """Снять зависший прогон.

    Одновременно идёт только один сбор — это держит частичный уникальный
    индекс. Если процесс умер, не закрыв запись, она блокирует все следующие
    сборы. Сбор подбирает такие сам по таймауту, но ждать его незачем: кнопка
    делает то же самое немедленно, не требуя доступа к базе.
    """

    rows = await returning(
        db,
        text(
            "UPDATE harvest_runs SET status = 'failed', finished_at = now(), "
            "error = coalesce(error, 'Снято администратором: прогон висел в статусе running.') "
            "WHERE status = 'running' RETURNING id"
        ),
        {},
    )
    await audit(
        db,
        admin,
        request,
        action="harvest.abort",
        entity_type="harvest_run",
        payload={"aborted": len(rows)},
    )
    return {"aborted": len(rows)}


@router.get("/decisions")
async def decisions(
    admin: Admin,
    db: Engine,
    paging: Paging,
    decision: str | None = None,
    source: str | None = None,
    blocked_by: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    """Журнал решений гейта — на чём калибруются пороги."""

    conditions = ["true"]
    params: dict[str, Any] = {"limit": paging.limit, "offset": paging.offset}
    for name, value in (
        ("decision", decision),
        ("source", source),
        ("blocked_by", blocked_by),
    ):
        if value:
            conditions.append(f"{name} = :{name}")
            params[name] = value
    if q:
        conditions.append("title ILIKE :q")
        params["q"] = f"%{q}%"
    where = " AND ".join(conditions)
    rows = await fetch_all(
        db,
        text(
            f"SELECT id, source, external_id, stage, decision, keyword_score, semantic_score,"
            f" matched_terms, blocked_by, title, reason, created_at FROM harvest_decisions"
            f" WHERE {where} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    total = await fetch_one(
        db,
        text(f"SELECT count(*) AS n FROM harvest_decisions WHERE {where}"),
        {k: v for k, v in params.items() if k not in {"limit", "offset"}},
    )
    return page_response(rows, int((total or {}).get("n", 0)), paging)


@router.get("/blocked-reasons")
async def blocked_reasons(
    admin: Admin, db: Engine, days: Annotated[int, Query(ge=1, le=365)] = 30
) -> list[dict[str, Any]]:
    """Топ причин отклонения: если профиль ловит не то, видно здесь."""

    return await fetch_all(
        db,
        text(
            "SELECT coalesce(blocked_by, 'не прошёл satisfy') AS reason, count(*) AS n"
            " FROM harvest_decisions"
            " WHERE decision = 'rejected' AND created_at >= now() - make_interval(days => :days)"
            " GROUP BY 1 ORDER BY n DESC LIMIT 30"
        ),
        {"days": days},
    )
