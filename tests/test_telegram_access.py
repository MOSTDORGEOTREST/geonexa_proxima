"""Политика доступа: кого бот слушает и почему.

Раньше это был один список id, и пустой список значил «никому». Свежая
установка молча не отвечала даже владельцу — а по логам это выглядело как
неработающий бот.
"""

from __future__ import annotations

import pytest

from geonexa_proxima.config import RegistrationMode
from geonexa_proxima.telegram.middleware import AccessPolicy


def test_empty_allowlist_does_not_lock_everyone_out() -> None:
    policy = AccessPolicy(RegistrationMode.ALLOWLIST)

    assert policy.allows(12345)
    assert policy.is_effectively_open


def test_allowlist_filters_when_it_is_not_empty() -> None:
    policy = AccessPolicy(RegistrationMode.ALLOWLIST, allowed_user_ids=[1, 2])

    assert policy.allows(1)
    assert not policy.allows(3)
    assert not policy.is_effectively_open


def test_open_mode_lets_everyone_in() -> None:
    policy = AccessPolicy(RegistrationMode.OPEN, allowed_user_ids=[1])

    assert policy.allows(999)
    assert policy.is_effectively_open


def test_invite_mode_admits_only_listed_users() -> None:
    policy = AccessPolicy(RegistrationMode.INVITE, allowed_user_ids=[7])

    assert policy.allows(7)
    assert not policy.allows(8)
    # Пустой список здесь означает именно «никого»: режим invite про это и есть.
    assert not AccessPolicy(RegistrationMode.INVITE).allows(7)


def test_owner_is_never_locked_out() -> None:
    """Иначе можно запереть себя снаружи и чинить это только через базу."""

    policy = AccessPolicy(RegistrationMode.INVITE, allowed_user_ids=[], owner_ids=[42])

    assert policy.allows(42)
    assert not policy.allows(43)


def test_anonymous_update_is_rejected() -> None:
    assert not AccessPolicy(RegistrationMode.OPEN).allows(None)


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
def test_group_chats_can_be_switched_off(chat_type: str) -> None:
    policy = AccessPolicy(RegistrationMode.OPEN, owner_ids=[42], allow_group_chats=False)

    assert not policy.allows(42, chat_type=chat_type)
    assert policy.allows(42, chat_type="private")


def test_bot_answers_even_when_a_handler_crashes() -> None:
    """Упавший обработчик не должен выглядеть как «команда не работает».

    Без обработчика ошибок исключение уходит только в лог: в чате не
    появляется ничего, и снаружи сломанный сервис неотличим от пустой выдачи.
    """

    import inspect

    from geonexa_proxima.telegram import bot as module

    source = inspect.getsource(module.create_telegram_app)
    assert "@dispatcher.errors()" in source, "у диспетчера нет обработчика ошибок"
    # Ответ должен уходить и на сообщения, и на нажатия кнопок.
    assert "callback_query.answer" in source
    assert "message.answer" in source
