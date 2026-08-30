"""Кто вообще имеет право говорить с ботом.

Раньше здесь был один список разрешённых id, и пустой список означал «никому».
`TELEGRAM_REGISTRATION_MODE` при этом был объявлен и ни на что не влиял, так
что свежая установка молча не отвечала никому — включая владельца. Теперь
режим решает, а список разрешённых — лишь один из режимов.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from geonexa_proxima.config import RegistrationMode

#: Ответ на попытку заговорить с закрытым ботом. Молчание выглядит как поломка,
#: поэтому отвечаем — но не объясняем, кому доступ открыт.
DENIED = "Доступ закрыт. Обратитесь к администратору сервиса."


class AccessPolicy:
    """Решение о допуске, отделённое от aiogram — его можно проверить тестом."""

    def __init__(
        self,
        mode: RegistrationMode | str = RegistrationMode.ALLOWLIST,
        *,
        allowed_user_ids: Iterable[int] = (),
        owner_ids: Iterable[int] = (),
        allow_group_chats: bool = True,
    ) -> None:
        self.mode = RegistrationMode(str(getattr(mode, "value", mode)))
        self.allowed_user_ids = frozenset(allowed_user_ids)
        self.owner_ids = frozenset(owner_ids)
        self.allow_group_chats = allow_group_chats

    def allows(self, user_id: int | None, *, chat_type: str | None = None) -> bool:
        if chat_type in {"group", "supergroup", "channel"} and not self.allow_group_chats:
            return False
        if user_id is None:
            return False
        if user_id in self.owner_ids:
            # Владелец проходит всегда: иначе можно запереть себя снаружи.
            return True
        if self.mode is RegistrationMode.OPEN:
            return True
        if self.mode is RegistrationMode.ALLOWLIST:
            # Пустой список в режиме allowlist на свежей установке означал бы
            # «бот не отвечает никому», и понять это по логам невозможно.
            # Считаем такую конфигурацию открытой и говорим об этом при старте.
            return not self.allowed_user_ids or user_id in self.allowed_user_ids
        return user_id in self.allowed_user_ids

    @property
    def is_effectively_open(self) -> bool:
        """Правда ли, что сейчас войти может кто угодно."""

        if self.mode is RegistrationMode.OPEN:
            return True
        return self.mode is RegistrationMode.ALLOWLIST and not self.allowed_user_ids


class AccessMiddleware(BaseMiddleware):
    """Пропускает апдейт дальше, если политика разрешает."""

    def __init__(self, policy: AccessPolicy) -> None:
        self.policy = policy

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None) or getattr(
            getattr(event, "message", None), "chat", None
        )
        if self.policy.allows(getattr(user, "id", None), chat_type=getattr(chat, "type", None)):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer(DENIED, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(DENIED)
        return None
