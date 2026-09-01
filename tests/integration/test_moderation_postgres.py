"""Модерация против настоящего PostgreSQL: статусы, дайджест и история.

Три инварианта здесь нельзя проверить фейками, потому что они живут в SQL и в
транзакции: подтверждение обязано включить дайджест, блокировка обязана
пережить выход бота, а история чата обязана помнить и отказ.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from geonexa_proxima.config import get_settings
from geonexa_proxima.db import create_engine, create_session_factory
from geonexa_proxima.db.models import (
    ChatEventModel,
    SubscriberModel,
    SubscriberProfileModel,
)
from geonexa_proxima.db.subscriber_repository import ChatIdentity, SubscriberRepository
from geonexa_proxima.domain import UserStatus

pytestmark = pytest.mark.skipif(
    os.getenv("GEONEXA_RUN_INTEGRATION") != "1",
    reason="set GEONEXA_RUN_INTEGRATION=1 with PostgreSQL migrated to head",
)


def _chat_id() -> int:
    return -(700_000_000_000 + uuid4().int % 99_999_999)


@pytest.fixture
async def repository():
    settings = get_settings()
    # Режим TLS берём из настроек, а не из умолчания `create_engine`: локальная
    # база под тестом обычно без TLS, а умолчание `prefer` для asyncpg означает
    # обязательное шифрование и отказ соединения.
    engine = create_engine(
        settings.database_url,
        ssl_mode=settings.database_ssl_mode,
        ssl_root_cert=settings.database_ssl_root_cert or None,
    )
    sessions = create_session_factory(engine)
    created: list[int] = []

    def track(chat_id: int) -> int:
        created.append(chat_id)
        return chat_id

    yield SubscriberRepository(sessions), track

    async with sessions() as session, session.begin():
        await session.execute(
            delete(SubscriberModel).where(SubscriberModel.telegram_chat_id.in_(created))
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_approving_a_chat_turns_the_digest_on(repository) -> None:
    """Иначе подтверждение не меняет ничего.

    Диспетчер отбирает по `digest_enabled`, а профиль заводится выключенным.
    Подтверждённый чат молча не получал бы ничего, и по статусу это неотличимо
    от работающего.
    """

    subscribers, track = repository
    chat_id = track(_chat_id())
    user, _ = await subscribers.register_chat(
        ChatIdentity(telegram_chat_id=chat_id, chat_type="supergroup", title="Отдел изысканий"),
        bot_status="member",
    )
    assert user.status is UserStatus.PENDING

    async with subscribers._session_factory() as session, session.begin():
        session.add(
            SubscriberProfileModel(
                subscriber_id=user.id,
                name="Основной",
                normalized_name="основной",
                compiled_text="геотехника",
                version=1,
                is_active=True,
                digest_enabled=False,
            )
        )

    approved = await subscribers.approve(user.id, actor="admin")
    assert approved.status is UserStatus.ACTIVE

    async with subscribers._session_factory() as session:
        enabled = await session.scalar(
            select(SubscriberProfileModel.digest_enabled).where(
                SubscriberProfileModel.subscriber_id == user.id
            )
        )
    assert enabled is True


@pytest.mark.asyncio
async def test_blocking_survives_the_bot_being_kicked_and_re_added(repository) -> None:
    """Иначе заблокированному чату достаточно выгнать бота и вернуть обратно.

    Выход бота ставил `left` поверх `blocked`, а возврат видел только `left` и
    открывал доступ: блокировка снималась сама, без администратора.
    """

    subscribers, track = repository
    chat_id = track(_chat_id())
    user, _ = await subscribers.register_chat(
        ChatIdentity(telegram_chat_id=chat_id, chat_type="supergroup", title="Чат"),
        bot_status="member",
    )
    await subscribers.approve(user.id, actor="admin")
    await subscribers.reject(user.id, actor="admin", reason="не наш")

    await subscribers.update_bot_status(chat_id, "kicked")
    record = await subscribers.get(user.id)
    assert record is not None
    assert record.status is UserStatus.BLOCKED

    await subscribers.update_bot_status(chat_id, "member")
    record = await subscribers.get(user.id)
    assert record is not None
    assert record.status is UserStatus.BLOCKED, "блокировку снимает только администратор"


@pytest.mark.asyncio
async def test_an_approved_chat_returns_to_work_without_a_second_approval(repository) -> None:
    """Переезд группы в супергруппу — не повод требовать подтверждения заново."""

    subscribers, track = repository
    chat_id = track(_chat_id())
    user, _ = await subscribers.register_chat(
        ChatIdentity(telegram_chat_id=chat_id, chat_type="supergroup", title="Чат"),
        bot_status="member",
    )
    await subscribers.approve(user.id, actor="admin")

    await subscribers.update_bot_status(chat_id, "left")
    await subscribers.update_bot_status(chat_id, "member")

    record = await subscribers.get(user.id)
    assert record is not None
    assert record.status is UserStatus.ACTIVE


@pytest.mark.asyncio
async def test_rejection_is_visible_in_the_chat_history(repository) -> None:
    """Симметрично подтверждению: иначе отказ выглядит как «ничего не было»."""

    subscribers, track = repository
    chat_id = track(_chat_id())
    user, _ = await subscribers.register_chat(
        ChatIdentity(telegram_chat_id=chat_id, chat_type="supergroup", title="Чат"),
        bot_status="member",
    )
    await subscribers.reject(user.id, actor="admin", reason="дубль")

    async with subscribers._session_factory() as session:
        kinds = (
            await session.scalars(
                select(ChatEventModel.event_type).where(ChatEventModel.subscriber_id == user.id)
            )
        ).all()
    assert "rejected" in kinds


@pytest.mark.asyncio
async def test_pending_chat_is_invisible_to_the_dispatcher(repository) -> None:
    """Заявка не должна попадать в рассылку ни при каких настройках профиля."""

    from geonexa_proxima.services.dispatch_queries import DUE_PROFILES

    subscribers, track = repository
    chat_id = track(_chat_id())
    user, _ = await subscribers.register_chat(
        ChatIdentity(telegram_chat_id=chat_id, chat_type="supergroup", title="Чат"),
        bot_status="member",
    )
    async with subscribers._session_factory() as session, session.begin():
        session.add(
            SubscriberProfileModel(
                subscriber_id=user.id,
                name="Основной",
                normalized_name="основной",
                compiled_text="геотехника",
                version=1,
                is_active=True,
                digest_enabled=True,
            )
        )
        await session.execute(text("SELECT 1"))

    async with subscribers._session_factory() as session:
        rows = (
            await session.execute(DUE_PROFILES, {"kinds": ["group", "channel"], "limit": 50})
        ).all()
    assert all(row.subscriber_id != user.id for row in rows)
