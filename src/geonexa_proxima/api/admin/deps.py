"""Общие зависимости админ-API: движок, репозитории, аудит, постраничность."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from geonexa_proxima.api.admin.security import AdminIdentity, current_admin, ip_or_none
from geonexa_proxima.config import Settings, get_settings
from geonexa_proxima.db.subscriber_repository import SubscriberRepository

Admin = Annotated[AdminIdentity, Depends(current_admin)]


def app_settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


AppSettings = Annotated[Settings, Depends(app_settings)]


def engine(request: Request) -> AsyncEngine:
    """Движок БД. Без него админка бесполезна, поэтому 503, а не пустой ответ."""

    container = getattr(request.app.state, "container", None)
    active = getattr(container, "engine", None) if container else None
    if active is None:
        from geonexa_proxima.db.session import get_engine

        try:
            active = get_engine(app_settings(request))
        except Exception as error:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Нет доступа к базе: {error}"
            ) from error
    return active


Engine = Annotated[AsyncEngine, Depends(engine)]


def subscribers(request: Request) -> SubscriberRepository:
    container = getattr(request.app.state, "container", None)
    factory = getattr(container, "session_factory", None) if container else None
    if factory is None:
        from geonexa_proxima.db.session import create_session_factory

        factory = create_session_factory(engine(request))
    return SubscriberRepository(factory)


Subscribers = Annotated[SubscriberRepository, Depends(subscribers)]


@dataclass(frozen=True, slots=True)
class PageParams:
    """Постраничность одна на всё API: смещение считается здесь, а не в роутерах."""

    page: int = 1
    per_page: int = 50

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    @property
    def limit(self) -> int:
        return self.per_page


def page_params(
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    per_page: Annotated[int, Query(ge=1, le=500)] = 50,
) -> PageParams:
    return PageParams(page=page, per_page=per_page)


Paging = Annotated[PageParams, Depends(page_params)]


def page_response(rows: Sequence[Any], total: int, params: PageParams) -> dict[str, Any]:
    return {
        "items": list(rows),
        "total": total,
        "page": params.page,
        "per_page": params.per_page,
        "pages": max(1, -(-total // params.per_page)),
    }


async def audit(
    db: AsyncEngine,
    admin: AdminIdentity,
    request: Request,
    *,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Записать действие администратора.

    Аудит не должен ронять операцию, которую записывает: если запись не
    удалась, действие уже произошло, и притворяться, что нет, — хуже.
    """

    try:
        async with db.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO admin_audit_log (actor, action, entity_type, entity_id,"
                    " after, ip, user_agent) VALUES (:actor,"
                    " :action, :entity_type, :entity_id, CAST(:payload AS jsonb),"
                    " CAST(:ip AS inet), :agent)"
                ),
                {
                    "actor": admin.username,
                    "action": action,
                    "entity_type": entity_type,
                    "entity_id": str(entity_id) if entity_id else None,
                    "payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
                    "ip": ip_or_none(request),
                    "agent": request.headers.get("user-agent", "")[:500] or None,
                },
            )
    except Exception:
        return


def conflict_from(error: IntegrityError) -> HTTPException:
    """Перевести нарушение инварианта БД в понятный администратору ответ."""

    text_error = str(getattr(error, "orig", error))
    if "ex_subscriptions_no_overlap" in text_error:
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Периоды подписок пересекаются: закройте текущую подписку или сдвиньте даты",
        )
    if "uq_subscribers_telegram_chat_id" in text_error or "telegram_chat_id" in text_error:
        return HTTPException(status.HTTP_409_CONFLICT, detail="Такой chat_id уже зарегистрирован")
    if "duplicate key" in text_error:
        return HTTPException(status.HTTP_409_CONFLICT, detail="Запись с таким ключом уже есть")
    return HTTPException(status.HTTP_409_CONFLICT, detail=text_error[:500])


async def fetch_all(
    db: AsyncEngine, statement: Any, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    async with db.connect() as connection:
        result = await connection.execute(
            statement if not isinstance(statement, str) else text(statement), params or {}
        )
        return [dict(row) for row in result.mappings().all()]


async def fetch_one(
    db: AsyncEngine, statement: Any, params: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    rows = await fetch_all(db, statement, params)
    return rows[0] if rows else None


async def returning(
    db: AsyncEngine, statement: Any, params: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Выполнить изменяющий запрос с RETURNING и зафиксировать транзакцию.

    Отдельно от `fetch_one`: тот открывает соединение без транзакции, и INSERT
    через него молча откатывался — запрос отвечал 201 с телом, а строки в базе
    не появлялось.
    """

    async with db.begin() as connection:
        result = await connection.execute(
            statement if not isinstance(statement, str) else text(statement), params or {}
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def scalar(db: AsyncEngine, statement: Any, params: dict[str, Any] | None = None) -> Any:
    async with db.connect() as connection:
        return await connection.scalar(
            statement if not isinstance(statement, str) else text(statement), params or {}
        )


async def execute(db: AsyncEngine, statement: Any, params: dict[str, Any] | None = None) -> int:
    async with db.begin() as connection:
        result = await connection.execute(
            statement if not isinstance(statement, str) else text(statement), params or {}
        )
        return result.rowcount or 0


def require(row: Any, what: str = "Запись") -> Any:
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{what} не найдена")
    return row
