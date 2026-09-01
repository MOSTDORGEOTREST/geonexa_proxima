"""Групп-центричная схема: кого пускают, куда и что при этом говорят.

Правил здесь три, и каждое ловит ошибку, которую снаружи видно как «бот не
работает», а не как отказ:

* заявка не равна доступу — и человек, и чат заводятся неактивными;
* личка занимается только профилем, материалы приходят в группы;
* профиль чата ведёт администратор, а не участники переписки.

Проверяются именно решения, а не aiogram: `decide` — чистая функция, и таблица
случаев читается быстрее, чем сценарий с поддельными апдейтами.
"""

from __future__ import annotations

import re

import pytest

from geonexa_proxima.domain import SubscriberKind, UserStatus
from geonexa_proxima.telegram import onboarding
from geonexa_proxima.telegram.middleware import (
    CHAT_PROFILE_IS_ADMIN_MANAGED,
    command_in,
    command_of,
    decide,
)

CYRILLIC = re.compile("[А-Яа-яЁё]")


def test_command_is_parsed_without_bot_suffix_and_case() -> None:
    """В группе Telegram дописывает `@имя_бота` — без этого гейт не сработает."""

    assert command_of("/Daily@proxima_bot вчера") == "daily"
    assert command_of("/interests") == "interests"
    # Обычный текст — не команда: это ответ на вопрос формы, и запрещать его
    # нельзя, иначе заполнение профиля обрывается на первом же сообщении.
    assert command_of("инженер-геотехник, разжижение грунтов") is None
    assert command_of(None) is None


def test_unknown_person_is_admitted_only_to_start() -> None:
    assert decide(chat_type="private", command="start", status=None).allowed
    denied = decide(chat_type="private", command="daily", status=None)
    assert not denied.allowed
    assert denied.note == onboarding.PRIVATE_UNKNOWN


def test_pending_person_waits_and_is_told_so() -> None:
    """Заявка не равна доступу: /start отвечает, остальное — нет."""

    assert decide(chat_type="private", command="start", status=UserStatus.PENDING).allowed
    denied = decide(chat_type="private", command="interests", status=UserStatus.PENDING)
    assert not denied.allowed
    assert denied.note == onboarding.PRIVATE_NOT_APPROVED
    # И свободный текст тоже: иначе неподтверждённый заполнял бы профиль,
    # которого ему ещё не открыли.
    assert not decide(chat_type="private", command=None, status=UserStatus.PENDING).allowed


def test_private_chat_is_only_about_the_profile() -> None:
    active = UserStatus.ACTIVE
    assert decide(chat_type="private", command="profile_edit", status=active).allowed
    # Ответ на вопрос формы обязан проходить.
    assert decide(chat_type="private", command=None, status=active).allowed
    denied = decide(chat_type="private", command="daily", status=active)
    assert not denied.allowed
    assert denied.note == onboarding.PRIVATE_ONLY_PROFILE


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
def test_unapproved_chat_gets_nothing(chat_type: str) -> None:
    for status in (None, UserStatus.PENDING):
        verdict = decide(chat_type=chat_type, command="daily", status=status)
        assert not verdict.allowed
        assert verdict.note == onboarding.CHAT_NOT_APPROVED


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_approved_chat_gets_materials_but_not_the_profile(chat_type: str) -> None:
    active = UserStatus.ACTIVE
    assert decide(chat_type=chat_type, command="daily", status=active).allowed
    assert decide(chat_type=chat_type, command="search", status=active).allowed
    # Нажатие кнопки под материалом — не команда, и запрещать его нельзя:
    # иначе обратная связь в группе молча перестаёт работать.
    assert decide(chat_type=chat_type, command=None, status=active).allowed
    denied = decide(chat_type=chat_type, command="profile_edit", status=active)
    assert not denied.allowed
    assert denied.note == CHAT_PROFILE_IS_ADMIN_MANAGED


@pytest.mark.parametrize("status", [UserStatus.BLOCKED, UserStatus.LEFT])
@pytest.mark.parametrize("chat_type", ["private", "group", "channel"])
def test_blocked_never_passes(status: UserStatus, chat_type: str) -> None:
    verdict = decide(chat_type=chat_type, command="start", status=status)
    assert not verdict.allowed
    assert verdict.note == onboarding.ACCESS_CLOSED


def test_onboarding_names_the_whole_hierarchy() -> None:
    """Человек, впервые увидевший бота, должен понять, чей он.

    Три имени и три разные вещи: МОСТДОРГЕОТРЕСТ — компания, ГЕОНЕКСА —
    платформа, Проксима — сервис. Потерять одно из них в приветствии значит
    оставить получателя с вопросом «а это вообще от кого».
    """

    for text in (onboarding.PRIVATE_WELCOME, onboarding.CHAT_APPROVED):
        for name in ("МОСТДОРГЕОТРЕСТ", "ГЕОНЕКСА", "Проксима"):
            assert name in text, (name, text[:80])


def test_welcome_says_where_the_service_actually_works() -> None:
    """Иначе человек ждёт дайджест в личку и считает бота сломанным."""

    assert "групп" in onboarding.PRIVATE_WELCOME
    assert "групп" in onboarding.PRIVATE_ONLY_PROFILE


def test_every_outgoing_text_is_russian() -> None:
    texts = {
        name: value
        for name, value in vars(onboarding).items()
        if name.isupper() and isinstance(value, str)
    }
    assert texts, "в модуле онбординга не нашлось ни одного текста"
    assert all(CYRILLIC.search(value) for value in texts.values()), sorted(texts)


def test_approval_message_depends_on_who_is_approved() -> None:
    assert onboarding.approval_message(SubscriberKind.USER) == onboarding.PRIVATE_APPROVED
    for kind in (SubscriberKind.GROUP, SubscriberKind.CHANNEL, "channel"):
        assert onboarding.approval_message(kind) == onboarding.CHAT_APPROVED


def test_returning_chat_is_re_queued_unless_it_was_approved() -> None:
    """Бота выкинули и вернули — это не повод открывать доступ самому.

    И не повод требовать подтверждения во второй раз: переезд группы в
    супергруппу выглядит для нас ровно так же, как первое добавление.
    """

    from types import SimpleNamespace

    from geonexa_proxima.db.subscriber_repository import _status_after_return, is_approved

    fresh = SimpleNamespace(meta={})
    approved = SimpleNamespace(meta={"approved": True, "approved_by": "admin"})

    assert not is_approved(fresh)
    assert is_approved(approved)
    assert _status_after_return(fresh) == UserStatus.PENDING.value
    assert _status_after_return(approved) == UserStatus.ACTIVE.value


def test_unapproved_chat_stays_silent_on_ordinary_messages() -> None:
    """Регрессия: бот отвечал отказом на КАЖДОЕ сообщение в неподтверждённом чате.

    Бота добавили в рабочий чат на сто человек, администратор ещё не нажал
    «Подтвердить» — и бот отвечал «чат ещё не подтверждён» на каждую реплику,
    упирался в лимит группы и вылетал из чата раньше, чем заявку успевали
    рассмотреть.
    """

    verdict = decide(chat_type="supergroup", command=None, status=None, interactive=False)

    assert not verdict.allowed
    assert verdict.note is None


def test_unapproved_chat_answers_a_command() -> None:
    """На адресованное боту действие тишина неотличима от поломки."""

    verdict = decide(chat_type="supergroup", command="daily", status=None, interactive=True)

    assert not verdict.allowed
    assert verdict.note is not None


def test_command_is_recognised_in_a_file_caption() -> None:
    """aiogram матчит команду и в подписи к файлу.

    Если политика смотрит только на текст, фото с подписью `/profile_edit`
    выглядит обычным сообщением и проходит мимо разграничения «профиль чата
    ведёт администратор».
    """

    class _Message:
        text = None
        caption = "/profile_edit новая тема"

    assert command_in(_Message()) == "profile_edit"


def test_plain_caption_is_not_a_command() -> None:
    class _Message:
        text = None
        caption = "фотография с изысканий"

    assert command_in(_Message()) is None
