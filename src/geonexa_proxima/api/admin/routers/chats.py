"""Группы и каналы, куда добавили бота."""

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
    fetch_all,
    fetch_one,
    page_response,
    require,
)
from geonexa_proxima.db.subscriber_repository import SubscriberNotFoundError
from geonexa_proxima.domain import CHAT_KINDS

router = APIRouter(prefix="/chats", tags=["admin:chats"])


class TestMessage(BaseModel):
    text: str = Field(default="Проксима на связи.", min_length=1, max_length=2000)


@router.get("")
async def list_chats(
    admin: Admin,
    repository: Subscribers,
    paging: Paging,
    bot_status: Annotated[list[str] | None, Query()] = None,
    kind: Annotated[list[str] | None, Query()] = None,
    present_only: bool = False,
    q: str | None = None,
) -> dict[str, Any]:
    """Все чаты с текущими правами бота."""

    try:
        rows = await repository.list_chats(
            kinds=kind or None,
            bot_statuses=bot_status or None,
            present_only=present_only,
            search=q,
            limit=paging.limit,
            offset=paging.offset,
        )
        total = await repository.count_subscribers(kinds=kind or CHAT_KINDS, search=q)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return page_response(
        [
            {
                "subscriber_id": str(record.subscriber_id),
                "kind": record.kind,
                "telegram_chat_id": record.telegram_chat_id,
                "title": record.title,
                "username": record.username,
                "status": record.status,
                "bot_status": record.bot_status,
                "chat_type": record.chat_type,
                "member_count": record.member_count,
                "can_post_messages": record.can_post_messages,
                "can_deliver": record.can_deliver,
                "is_present": record.is_present,
                "profiles": record.profiles,
                "added_at": record.added_at,
                "removed_at": record.removed_at,
                "last_checked_at": record.last_checked_at,
                "error": record.error,
            }
            for record in rows
        ],
        total,
        paging,
    )


@router.get("/{subscriber_id}")
async def get_chat(subscriber_id: UUID, admin: Admin, db: Engine) -> dict[str, Any]:
    chat = require(
        await fetch_one(
            db,
            text(
                "SELECT s.*, cm.bot_status, cm.can_post_messages, cm.member_count, cm.chat_type,"
                " cm.invite_link, cm.added_by_user_id, cm.added_at, cm.removed_at,"
                " cm.last_checked_at, cm.error"
                " FROM subscribers s LEFT JOIN chat_memberships cm ON cm.subscriber_id = s.id"
                " WHERE s.id = :id AND s.kind <> 'user'"
            ),
            {"id": str(subscriber_id)},
        ),
        "Чат",
    )
    events = await fetch_all(
        db,
        text(
            "SELECT event_type, old_value, new_value, occurred_at FROM chat_events"
            " WHERE subscriber_id = :id ORDER BY occurred_at DESC LIMIT 50"
        ),
        {"id": str(subscriber_id)},
    )
    return {"chat": chat, "events": events}


@router.post("/{subscriber_id}/refresh")
async def refresh_chat(
    subscriber_id: UUID, admin: Admin, db: Engine, repository: Subscribers, request: Request
) -> dict[str, Any]:
    """Спросить Telegram о правах бота прямо сейчас, не дожидаясь мониторинга."""

    chat = require(
        await fetch_one(
            db,
            text("SELECT telegram_chat_id, kind FROM subscribers WHERE id = :id"),
            {"id": str(subscriber_id)},
        ),
        "Чат",
    )
    container = getattr(request.app.state, "container", None)
    if container is None or not hasattr(container, "telegram_bot"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Бот не сконфигурирован")
    bot = container.telegram_bot()
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat["telegram_chat_id"], me.id)
        info = await bot.get_chat(chat["telegram_chat_id"])
    except Exception as error:
        # Чат мог исчезнуть или бота выгнали — это и есть ответ, а не сбой.
        await repository.update_bot_status(
            chat["telegram_chat_id"], "kicked", error=str(error)[:500]
        )
        return {"bot_status": "kicked", "error": str(error)[:500]}
    member_status = str(getattr(member.status, "value", member.status))
    can_post = getattr(member, "can_post_messages", None)
    if chat["kind"] != "channel":
        can_post = True
    record = await repository.update_bot_status(
        chat["telegram_chat_id"],
        member_status,
        can_post_messages=can_post,
        member_count=getattr(info, "member_count", None),
    )
    await audit(
        db,
        admin,
        request,
        action="chat.refresh",
        entity_type="chat",
        entity_id=str(subscriber_id),
        payload={"bot_status": member_status},
    )
    return {
        "bot_status": record.bot_status,
        "can_deliver": record.can_deliver,
        "member_count": record.member_count,
    }


@router.post("/{subscriber_id}/leave")
async def leave_chat(
    subscriber_id: UUID, admin: Admin, db: Engine, repository: Subscribers, request: Request
) -> dict[str, Any]:
    """Вывести бота из чата и погасить подписчика."""

    chat = require(
        await fetch_one(
            db,
            text("SELECT telegram_chat_id FROM subscribers WHERE id = :id AND kind <> 'user'"),
            {"id": str(subscriber_id)},
        ),
        "Чат",
    )
    container = getattr(request.app.state, "container", None)
    if container is not None and hasattr(container, "telegram_bot"):
        try:
            await container.telegram_bot().leave_chat(chat["telegram_chat_id"])
        except Exception as error:
            await audit(
                db,
                admin,
                request,
                action="chat.leave_failed",
                entity_type="chat",
                entity_id=str(subscriber_id),
                payload={"error": str(error)[:300]},
            )
    try:
        record = await repository.update_bot_status(chat["telegram_chat_id"], "left")
    except SubscriberNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    await audit(
        db, admin, request, action="chat.leave", entity_type="chat", entity_id=str(subscriber_id)
    )
    return {"bot_status": record.bot_status, "status": record.status}


@router.post("/{subscriber_id}/test-message")
async def test_message(
    subscriber_id: UUID, payload: TestMessage, admin: Admin, db: Engine, request: Request
) -> dict[str, Any]:
    """Проверить, что в чат вообще проходит сообщение."""

    chat = require(
        await fetch_one(
            db,
            text("SELECT telegram_chat_id FROM subscribers WHERE id = :id"),
            {"id": str(subscriber_id)},
        ),
        "Чат",
    )
    container = getattr(request.app.state, "container", None)
    if container is None or not hasattr(container, "telegram_bot"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Бот не сконфигурирован")
    try:
        message = await container.telegram_bot().send_message(
            chat["telegram_chat_id"], payload.text
        )
    except Exception as error:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail=f"Telegram отказал: {error}"
        ) from error
    await audit(
        db,
        admin,
        request,
        action="chat.test_message",
        entity_type="chat",
        entity_id=str(subscriber_id),
    )
    return {"sent": True, "message_id": getattr(message, "message_id", None)}


@router.get("/{subscriber_id}/events")
async def chat_events(
    subscriber_id: UUID, admin: Admin, db: Engine, paging: Paging
) -> dict[str, Any]:
    rows = await fetch_all(
        db,
        text(
            "SELECT event_type, old_value, new_value, occurred_at FROM chat_events"
            " WHERE subscriber_id = :id ORDER BY occurred_at DESC LIMIT :limit OFFSET :offset"
        ),
        {"id": str(subscriber_id), "limit": paging.limit, "offset": paging.offset},
    )
    total = await fetch_one(
        db,
        text("SELECT count(*) AS n FROM chat_events WHERE subscriber_id = :id"),
        {"id": str(subscriber_id)},
    )
    return page_response(rows, int((total or {}).get("n", 0)), paging)
