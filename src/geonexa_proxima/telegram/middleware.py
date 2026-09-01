"""Кто вообще имеет право говорить с ботом.

Раньше здесь был один список разрешённых id, и пустой список означал «никому».
`TELEGRAM_REGISTRATION_MODE` при этом был объявлен и ни на что не влиял, так
что свежая установка молча не отвечала никому — включая владельца. Теперь
режим решает, а список разрешённых — лишь один из режимов.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from geonexa_proxima.config import RegistrationMode
from geonexa_proxima.domain import UserStatus
from geonexa_proxima.telegram.onboarding import (
    ACCESS_CLOSED,
    CHAT_NOT_APPROVED,
    PRIVATE_NOT_APPROVED,
    PRIVATE_ONLY_PROFILE,
    PRIVATE_UNKNOWN,
)

#: Ответ на попытку заговорить с закрытым ботом. Молчание выглядит как поломка,
#: поэтому отвечаем — но не объясняем, кому доступ открыт.
DENIED = ACCESS_CLOSED

#: Что можно делать в личке. Личный чат в групп-центричной схеме нужен для
#: одного — описать свои интересы; материалы приходят в группы и каналы.
PRIVATE_COMMANDS = frozenset(
    {
        "start",
        "cancel",
        "help",
        "howto",
        "profile",
        "profiles",
        "profile_new",
        "profile_use",
        "profile_edit",
        "profile_delete",
        "interests",
        "personalization",
    }
)

#: Что можно делать в группе и канале. Профиля здесь нет намеренно: профиль
#: чата ведёт администратор сервиса в админке, а не участники в переписке —
#: иначе любой из сотни участников молча переписывает выдачу всем остальным.
CHAT_COMMANDS = frozenset(
    {
        "start",
        "help",
        "daily",
        "week",
        "hot",
        "papers",
        "tools",
        "datasets",
        "search",
        "trends",
        "why",
    }
)

#: Ответ на команду профиля, отправленную в группу.
CHAT_PROFILE_IS_ADMIN_MANAGED = (
    "Профиль интересов этого чата ведёт администратор сервиса — из админки, а не командой в чате."
)

#: Только эти чаты вообще считаются групповыми.
_CHAT_TYPES = frozenset({"group", "supergroup", "channel"})


def command_of(text: str | None) -> str | None:
    """Имя команды из текста сообщения, без `@имя_бота` и регистра.

    Возвращает None для обычного текста: ответ на вопрос формы — не команда,
    и запрещать его нельзя, иначе диалог заполнения профиля обрывается на
    первом же сообщении.
    """

    if not text or not text.startswith("/"):
        return None
    return text.split(maxsplit=1)[0][1:].split("@", maxsplit=1)[0].casefold() or None


def command_in(message: Any) -> str | None:
    """Команда из сообщения: и из текста, и из подписи к файлу.

    aiogram матчит команду и в `caption`, поэтому «фото с подписью
    /profile_edit» доезжает до обработчика. Если смотреть только на `text`,
    такой апдейт выглядит для политики обычным сообщением и проходит мимо
    разграничения «личка занимается профилем, чат — нет».
    """

    if message is None:
        return None
    return command_of(getattr(message, "text", None)) or command_of(
        getattr(message, "caption", None)
    )


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
        elif isinstance(event, Message) and command_in(event) is not None:
            # Только на команду. С непустым списком допущенных бот иначе
            # отвечает «Доступ закрыт» каждому участнику группы на каждое его
            # сообщение — при том, что субъект разговора здесь чат, а не автор.
            await event.answer(DENIED)
        return None


@dataclass(slots=True, frozen=True)
class Verdict:
    """Что делать с апдейтом и что сказать, если не пропускаем."""

    allowed: bool
    note: str | None = None
    alert: bool = False


def decide(
    *,
    chat_type: str | None,
    command: str | None,
    status: UserStatus | None,
    interactive: bool = True,
) -> Verdict:
    """Пропустить апдейт или ответить отказом — без aiogram и без базы.

    Вынесено отдельной функцией, потому что здесь сходятся три правила
    групп-центричной схемы, и проверять их удобнее таблицей, а не сценарием в
    Telegram: неподтверждённый ждёт, личка занимается только профилем, профиль
    чата правится не из чата.

    ``status is None`` означает «подписчика ещё нет в базе»: так выглядит самый
    первый `/start` — его пропускаем, регистрация происходит в обработчике.
    """

    is_chat = (chat_type or "") in _CHAT_TYPES

    if status in {UserStatus.BLOCKED, UserStatus.LEFT}:
        return Verdict(False, ACCESS_CLOSED, alert=True)

    if not is_chat:
        # Личка. Незнакомца пускаем только на /start: любая другая команда от
        # человека, которого мы не видели, — это команда без профиля.
        if status is None:
            return Verdict(command == "start", PRIVATE_UNKNOWN)
        if status is not UserStatus.ACTIVE:
            return Verdict(command == "start", PRIVATE_NOT_APPROVED)
        if command is None or command in PRIVATE_COMMANDS:
            return Verdict(True)
        return Verdict(False, PRIVATE_ONLY_PROFILE)

    # Группа или канал.
    if status is not UserStatus.ACTIVE:
        # Молчим на обычные сообщения. Иначе бот, добавленный в рабочий чат на
        # сто человек, отвечает «чат ещё не подтверждён» на КАЖДУЮ реплику,
        # упирается в лимит группы и вылетает из чата раньше, чем
        # администратор дойдёт до очереди заявок. На команду и на нажатие
        # кнопки отвечаем: это адресованное боту действие, и тишина в ответ
        # неотличима от поломки.
        return Verdict(False, CHAT_NOT_APPROVED if interactive else None)
    if command is None or command in CHAT_COMMANDS:
        return Verdict(True)
    if command in PRIVATE_COMMANDS:
        return Verdict(False, CHAT_PROFILE_IS_ADMIN_MANAGED)
    return Verdict(True)


class ModerationMiddleware(BaseMiddleware):
    """Пускает дальше только подтверждённых — и только туда, где им место.

    Политика доступа (`AccessMiddleware`) отвечает на вопрос «кого бот вообще
    слушает» и знает только списки id из конфигурации. Этот слой отвечает на
    другой вопрос — «подтвердил ли администратора этот чат», — и ответ живёт в
    базе, а не в `.env`.

    Ошибка похода в базу не должна закрывать бота: если репозиторий недоступен,
    апдейт проходит дальше, а обработчик всё равно упрётся в ту же базу и
    ответит по-человечески через `dispatcher.errors()`.
    """

    def __init__(self, subscribers: Callable[[], Any]) -> None:
        self._subscribers = subscribers

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        message = event if isinstance(event, Message) else getattr(event, "message", None)
        chat = getattr(message, "chat", None)
        chat_type = getattr(chat, "type", None)
        chat_id = getattr(chat, "id", None)
        if chat_id is None:
            return await handler(event, data)

        status: UserStatus | None
        try:
            subscriber = await self._subscribers().get_by_chat_id(chat_id)
            status = subscriber.status if subscriber else None
        except Exception:
            return await handler(event, data)

        command = command_in(event) if isinstance(event, Message) else None
        # Нажатие кнопки — не команда: под материалом в подтверждённом чате
        # кнопки должны работать, и запрещать их вместе с командами нельзя.
        # Но ответить отказом на нажатие нужно — в отличие от обычной реплики.
        verdict = decide(
            chat_type=chat_type,
            command=command,
            status=status,
            interactive=isinstance(event, CallbackQuery) or command is not None,
        )
        if verdict.allowed:
            data["subscriber_status"] = status
            return await handler(event, data)
        if verdict.note is None:
            return None
        if isinstance(event, CallbackQuery):
            await event.answer(verdict.note[:200], show_alert=True)
        elif message is not None:
            await message.answer(verdict.note)
        return None
