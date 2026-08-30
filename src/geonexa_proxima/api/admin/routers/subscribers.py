"""Подписчики, их профили и интересы."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from geonexa_proxima.api.admin.deps import (
    Admin,
    Engine,
    Paging,
    Subscribers,
    audit,
    conflict_from,
    execute,
    fetch_all,
    fetch_one,
    page_response,
    require,
    returning,
)
from geonexa_proxima.db.subscriber_repository import (
    SubscriberNotFoundError,
    kind_from_chat_type,
)
from geonexa_proxima.domain import ALL_KINDS, SubscriberKind, UserStatus

router = APIRouter(tags=["admin:subscribers"])


class SubscriberPatch(BaseModel):
    status: UserStatus | None = None
    notes: str | None = None
    timezone: str | None = None
    is_owner: bool | None = None
    title: str | None = None


class SubscriberCreate(BaseModel):
    telegram_chat_id: int
    kind: SubscriberKind = SubscriberKind.USER
    title: str | None = None
    chat_type: str | None = None
    notes: str | None = None


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.get("/subscribers")
async def list_subscribers(
    admin: Admin,
    repository: Subscribers,
    paging: Paging,
    kind: Annotated[list[str] | None, Query()] = None,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    q: str | None = None,
    has_active_subscription: bool | None = None,
) -> dict[str, Any]:
    """Список подписчиков нужных видов с постраничностью."""

    kwargs: dict[str, Any] = {
        "kinds": kind or None,
        "statuses": status_filter or None,
        "search": q,
        "with_active_subscription": has_active_subscription,
    }
    try:
        rows = await repository.list_subscribers(**kwargs, limit=paging.limit, offset=paging.offset)
        total = await repository.count_subscribers(**kwargs)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return page_response([r.model_dump() for r in rows], total, paging)


@router.get("/subscribers/breakdown")
async def breakdown(admin: Admin, repository: Subscribers) -> dict[str, Any]:
    """Разрез «вид × статус» для верхней строки дашборда."""

    rows = await repository.breakdown()
    return {
        "kinds": ALL_KINDS,
        "rows": [{"kind": r.kind, "status": r.status, "count": r.count} for r in rows],
    }


@router.post("/subscribers", status_code=status.HTTP_201_CREATED)
async def create_subscriber(
    payload: SubscriberCreate, admin: Admin, db: Engine, request: Request
) -> dict[str, Any]:
    """Завести подписчика вручную — например, канал, куда бота ещё не добавили."""

    kind = payload.kind.value
    if payload.chat_type:
        kind = kind_from_chat_type(payload.chat_type)
    if kind == SubscriberKind.USER.value and payload.telegram_chat_id <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="chat_id личного чата положителен")
    if kind != SubscriberKind.USER.value and payload.telegram_chat_id >= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="chat_id группы или канала отрицателен"
        )
    try:
        row = await returning(
            db,
            text(
                "INSERT INTO subscribers (id, kind, telegram_chat_id, title, notes, status)"
                " VALUES (gen_random_uuid(), :kind, :chat_id, :title, :notes, 'active')"
                " RETURNING id, kind, telegram_chat_id, title, status"
            ),
            {
                "kind": kind,
                "chat_id": payload.telegram_chat_id,
                "title": payload.title,
                "notes": payload.notes,
            },
        )
    except Exception as error:
        from sqlalchemy.exc import IntegrityError

        if isinstance(error, IntegrityError):
            raise conflict_from(error) from error
        raise
    await audit(
        db,
        admin,
        request,
        action="subscriber.create",
        entity_type="subscriber",
        entity_id=str(row["id"]),
        payload={"kind": kind},
    )
    return row


@router.get("/subscribers/{subscriber_id}")
async def get_subscriber(subscriber_id: UUID, admin: Admin, db: Engine) -> dict[str, Any]:
    """Карточка подписчика: профили, подписка, членство в чате, активность."""

    row = require(
        await fetch_one(
            db,
            text(
                "SELECT s.*, cm.bot_status, cm.can_post_messages, cm.member_count,"
                " cm.chat_type, cm.added_at, cm.removed_at"
                " FROM subscribers s"
                " LEFT JOIN chat_memberships cm ON cm.subscriber_id = s.id"
                " WHERE s.id = :id"
            ),
            {"id": str(subscriber_id)},
        ),
        "Подписчик",
    )
    profiles = await fetch_all(
        db,
        text(
            "SELECT id, name, is_active, digest_enabled, delivery_format, max_items,"
            " min_personal_score, last_digest_at, next_digest_at, version"
            " FROM subscriber_profiles WHERE subscriber_id = :id"
            " ORDER BY is_active DESC, created_at"
        ),
        {"id": str(subscriber_id)},
    )
    subscription = await fetch_one(
        db,
        text(
            "SELECT sub.id, sub.status, sub.starts_at, sub.ends_at, sub.grace_until,"
            " p.key AS plan_key, p.name AS plan_name"
            " FROM subscriptions sub JOIN subscription_plans p ON p.id = sub.plan_id"
            " WHERE sub.subscriber_id = :id AND sub.status IN ('active', 'trial')"
            " AND sub.starts_at <= now()"
            " AND (sub.ends_at IS NULL OR coalesce(sub.grace_until, sub.ends_at) >= now())"
        ),
        {"id": str(subscriber_id)},
    )
    return {"subscriber": row, "profiles": profiles, "subscription": subscription}


@router.patch("/subscribers/{subscriber_id}")
async def patch_subscriber(
    subscriber_id: UUID, payload: SubscriberPatch, admin: Admin, db: Engine, request: Request
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Нечего менять")
    if "status" in changes:
        changes["status"] = changes["status"].value
    assignments = ", ".join(f"{key} = :{key}" for key in changes)
    updated = await execute(
        db,
        text(f"UPDATE subscribers SET {assignments}, updated_at = now() WHERE id = :id"),
        {**changes, "id": str(subscriber_id)},
    )
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Подписчик не найден")
    # Заблокированному подписчику дайджесты не строятся: иначе очередь копит
    # задания, которые Telegram всё равно отвергнет.
    if changes.get("status") in {UserStatus.BLOCKED.value, UserStatus.LEFT.value}:
        await execute(
            db,
            text(
                "UPDATE subscriber_profiles SET digest_enabled = false, updated_at = now()"
                " WHERE subscriber_id = :id"
            ),
            {"id": str(subscriber_id)},
        )
    await audit(
        db,
        admin,
        request,
        action="subscriber.update",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        payload=changes,
    )
    return {"updated": True, "changes": changes}


@router.post("/subscribers/{subscriber_id}/approve")
async def approve(
    subscriber_id: UUID, admin: Admin, db: Engine, repository: Subscribers, request: Request
) -> dict[str, Any]:
    try:
        user = await repository.set_status(subscriber_id, UserStatus.ACTIVE)
    except SubscriberNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    await audit(
        db,
        admin,
        request,
        action="subscriber.approve",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
    )
    return user.model_dump()


@router.post("/subscribers/{subscriber_id}/block")
async def block(
    subscriber_id: UUID, admin: Admin, db: Engine, repository: Subscribers, request: Request
) -> dict[str, Any]:
    try:
        user = await repository.set_status(subscriber_id, UserStatus.BLOCKED)
    except SubscriberNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    await audit(
        db,
        admin,
        request,
        action="subscriber.block",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
    )
    return user.model_dump()


@router.delete("/subscribers/{subscriber_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subscriber(
    subscriber_id: UUID, admin: Admin, db: Engine, repository: Subscribers, request: Request
) -> None:
    await repository.forget(subscriber_id)
    await audit(
        db,
        admin,
        request,
        action="subscriber.delete",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
    )


@router.get("/subscribers/{subscriber_id}/activity")
async def activity(subscriber_id: UUID, admin: Admin, db: Engine, paging: Paging) -> dict[str, Any]:
    """Лента событий подписчика: команды, фидбек, дайджесты, доставки."""

    rows = await fetch_all(
        db,
        text(
            "SELECT kind, payload, occurred_at, item_id, digest_id"
            " FROM subscriber_activity WHERE subscriber_id = :id"
            " ORDER BY occurred_at DESC LIMIT :limit OFFSET :offset"
        ),
        {"id": str(subscriber_id), "limit": paging.limit, "offset": paging.offset},
    )
    total = await fetch_one(
        db,
        text("SELECT count(*) AS n FROM subscriber_activity WHERE subscriber_id = :id"),
        {"id": str(subscriber_id)},
    )
    return page_response(rows, int((total or {}).get("n", 0)), paging)


@router.post("/subscribers/{subscriber_id}/message")
async def send_message(
    subscriber_id: UUID,
    payload: MessageRequest,
    admin: Admin,
    db: Engine,
    request: Request,
) -> dict[str, Any]:
    """Отправить подписчику произвольное сообщение — минуя очередь дайджестов."""

    row = require(
        await fetch_one(
            db,
            text("SELECT telegram_chat_id, status FROM subscribers WHERE id = :id"),
            {"id": str(subscriber_id)},
        ),
        "Подписчик",
    )
    container = getattr(request.app.state, "container", None)
    if container is None or not hasattr(container, "telegram_bot"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Бот не сконфигурирован")
    try:
        message = await container.telegram_bot().send_message(row["telegram_chat_id"], payload.text)
    except Exception as error:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail=f"Telegram отказал: {error}"
        ) from error
    await audit(
        db,
        admin,
        request,
        action="subscriber.message",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        payload={"length": len(payload.text)},
    )
    return {"sent": True, "message_id": getattr(message, "message_id", None)}


@router.get("/subscribers/{subscriber_id}/profiles")
async def list_profiles(subscriber_id: UUID, admin: Admin, db: Engine) -> list[dict[str, Any]]:
    return await fetch_all(
        db,
        text(
            "SELECT * FROM subscriber_profiles WHERE subscriber_id = :id"
            " ORDER BY is_active DESC, created_at"
        ),
        {"id": str(subscriber_id)},
    )


class ProfilePatch(BaseModel):
    description: str | None = None
    digest_enabled: bool | None = None
    delivery_format: str | None = None
    max_items: int | None = Field(default=None, ge=1, le=100)
    min_personal_score: float | None = Field(default=None, ge=0, le=1)
    min_global_score: float | None = Field(default=None, ge=0, le=10)
    timezone: str | None = None
    paused_until: str | None = None


@router.patch("/profiles/{profile_id}")
async def patch_profile(
    profile_id: UUID, payload: ProfilePatch, admin: Admin, db: Engine, request: Request
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Нечего менять")
    if "delivery_format" in changes and changes["delivery_format"] not in {
        "cards",
        "compact",
        "single_message",
        "digest_post",
    }:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Неизвестный формат доставки")
    assignments = ", ".join(f"{key} = :{key}" for key in changes)
    updated = await execute(
        db,
        text(f"UPDATE subscriber_profiles SET {assignments}, updated_at = now() WHERE id = :id"),
        {**changes, "id": str(profile_id)},
    )
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Профиль не найден")
    await audit(
        db,
        admin,
        request,
        action="profile.update",
        entity_type="profile",
        entity_id=str(profile_id),
        payload=changes,
    )
    return {"updated": True, "changes": changes}


@router.get("/profiles/{profile_id}/interests")
async def interests(profile_id: UUID, admin: Admin, db: Engine) -> dict[str, Any]:
    explicit = await fetch_all(
        db,
        text(
            "SELECT i.id, i.query, i.polarity, i.weight, t.name AS topic"
            " FROM profile_interests i LEFT JOIN topics t ON t.id = i.topic_id"
            " WHERE i.profile_id = :id ORDER BY i.weight DESC"
        ),
        {"id": str(profile_id)},
    )
    learned = await fetch_all(
        db,
        text(
            "SELECT s.id, s.query, s.polarity, s.weight, s.evidence_count, s.source"
            " FROM profile_interest_signals s WHERE s.profile_id = :id"
            " ORDER BY s.weight DESC LIMIT 100"
        ),
        {"id": str(profile_id)},
    )
    return {"explicit": explicit, "learned": learned}
