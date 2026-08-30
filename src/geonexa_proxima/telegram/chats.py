"""Бота добавили в группу или канал — и мы об этом узнаём.

До этого модуля вся групповая половина продукта была недостижима: репозиторий
умел заводить чаты, диспетчер умел их выбирать, воркер умел в них слать, но
никто не создавал ни одной записи. Telegram сообщает о добавлении и удалении
бота апдейтом `my_chat_member`, ровно один раз на событие.

Пропущенный апдейт не катастрофа: раз в шесть часов `chat-monitor` сверяет
состояние опросом. Но опрос знает только про чаты, которые уже есть в базе, —
первое появление чата может прийти только отсюда.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import ChatMemberUpdated

from geonexa_proxima.db.subscriber_repository import (
    ChatIdentity,
    SubscriberRepository,
    kind_from_chat_type,
)
from geonexa_proxima.domain import PRESENT_BOT_STATUSES
from geonexa_proxima.services.container import Container

log = logging.getLogger(__name__)

#: Типы чатов, которые нас интересуют. Личку заводит /start, а не этот роутер.
CHAT_TYPES = frozenset({"group", "supergroup", "channel"})


def register_chat_router(container: Container) -> Router:
    """Собрать роутер, следящий за присутствием бота в чатах."""

    router = Router(name="geonexa-chats")

    @router.my_chat_member()
    async def on_membership_changed(event: ChatMemberUpdated) -> None:
        chat = event.chat
        if chat.type not in CHAT_TYPES:
            return
        settings = container.settings
        status = str(getattr(event.new_chat_member.status, "value", event.new_chat_member.status))
        present = status in PRESENT_BOT_STATUSES

        if not settings.telegram_allow_group_chats:
            log.info("Групповые чаты выключены — %s пропущен", chat.id)
            return
        if container.session_factory is None:
            log.warning("Нет фабрики сессий: чат %s не записан", chat.id)
            return

        repository = SubscriberRepository(container.session_factory)
        identity = ChatIdentity(
            telegram_chat_id=chat.id,
            chat_type=chat.type,
            title=chat.title,
            username=chat.username,
            added_by_user_id=event.from_user.id if event.from_user else None,
            invite_link=getattr(event.invite_link, "invite_link", None),
        )
        can_post = getattr(event.new_chat_member, "can_post_messages", None)
        if kind_from_chat_type(chat.type) != "channel":
            can_post = True

        known = await repository.get_by_chat_id(chat.id)
        if known is None:
            if not present or not settings.telegram_auto_register_chats:
                # Автозаведение выключено — фиксируем факт, но не создаём
                # подписчика: иначе админ обнаружит в базе чаты, которых не заводил.
                log.info(
                    "Чат %s (%s) не заведён: авторегистрация=%s, статус=%s",
                    chat.id,
                    chat.title,
                    settings.telegram_auto_register_chats,
                    status,
                )
                return
            _, created = await repository.register_chat(
                identity,
                bot_status=status,
                can_post_messages=can_post,
                raw_update=event.model_dump(mode="json", exclude_none=True),
            )
            log.info(
                "Чат %s (%s) %s",
                chat.id,
                chat.title,
                "заведён" if created else "обновлён",
            )
            return

        await repository.update_bot_status(
            chat.id,
            status,
            can_post_messages=can_post,
            raw_update=event.model_dump(mode="json", exclude_none=True),
        )
        log.info("Чат %s: статус бота -> %s", chat.id, status)

    return router
