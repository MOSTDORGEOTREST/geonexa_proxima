"""Чистые проверки kind-aware репозитория: без БД, только правила.

Живой прогон против PostgreSQL — `scripts/check_subscribers.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from geonexa_proxima.db.subscriber_repository import (
    CHAT_KINDS,
    PERSONAL_KINDS,
    ChatIdentity,
    ChatRecord,
    SubscriptionRecord,
    _kinds,
    _statuses,
    kind_from_chat_type,
)
from geonexa_proxima.domain import SubscriberKind, UserStatus


@pytest.mark.parametrize(
    ("chat_type", "expected"),
    [
        ("group", "group"),
        ("supergroup", "group"),
        ("SuperGroup", "group"),
        ("channel", "channel"),
        ("private", "user"),
        (None, "group"),
        ("", "group"),
    ],
)
def test_kind_from_chat_type(chat_type: str | None, expected: str) -> None:
    assert kind_from_chat_type(chat_type) == expected


def test_chat_identity_derives_kind() -> None:
    assert ChatIdentity(-100, "channel", "Канал").kind == "channel"
    assert ChatIdentity(-100, "supergroup", "Группа").kind == "group"


def test_kinds_defaults_and_validation() -> None:
    assert _kinds(None, PERSONAL_KINDS) == ("user",)
    assert _kinds([SubscriberKind.GROUP, "channel"], PERSONAL_KINDS) == ("group", "channel")
    with pytest.raises(ValueError, match="пустым"):
        _kinds([], PERSONAL_KINDS)
    with pytest.raises(ValueError, match="неизвестный вид"):
        _kinds(["bot"], PERSONAL_KINDS)


def test_statuses_validation() -> None:
    assert _statuses(None) is None
    assert _statuses([UserStatus.ACTIVE]) == ("active",)
    with pytest.raises(ValueError, match="неизвестный статус"):
        _statuses(["retired"])


def test_personal_and_chat_kinds_do_not_overlap() -> None:
    assert not set(PERSONAL_KINDS) & set(CHAT_KINDS)
    assert set(PERSONAL_KINDS) | set(CHAT_KINDS) == {k.value for k in SubscriberKind}


def _chat(kind: str, bot_status: str, can_post: bool | None) -> ChatRecord:
    return ChatRecord(
        subscriber_id=uuid4(),
        kind=kind,
        telegram_chat_id=-1001,
        title="Чат",
        username=None,
        status="active",
        bot_status=bot_status,
        chat_type=kind,
        member_count=10,
        can_post_messages=can_post,
        added_by_user_id=None,
        added_at=None,
        removed_at=None,
        last_checked_at=None,
        error=None,
    )


def test_channel_without_post_rights_is_not_deliverable() -> None:
    """Канал, где боту нельзя постить, — не адресат, а строка в списке чатов."""

    assert _chat("channel", "administrator", True).can_deliver
    assert not _chat("channel", "administrator", False).can_deliver
    assert not _chat("channel", "administrator", None).can_deliver
    # В группе право постить есть у любого участника.
    assert _chat("group", "member", None).can_deliver


def test_absent_bot_is_never_deliverable() -> None:
    for status in ("left", "kicked"):
        record = _chat("group", status, True)
        assert not record.is_present
        assert not record.can_deliver


def _subscription(**overrides: object) -> SubscriptionRecord:
    now = datetime.now(UTC)
    payload = {
        "id": uuid4(),
        "subscriber_id": uuid4(),
        "plan_id": uuid4(),
        "plan_key": "pro",
        "plan_name": "Pro",
        "status": "active",
        "starts_at": now - timedelta(days=1),
        "ends_at": now + timedelta(days=1),
        "grace_until": None,
        "auto_renew": False,
        "source": "admin",
        "notes": None,
        "created_at": now,
    }
    payload.update(overrides)
    return SubscriptionRecord(**payload)  # type: ignore[arg-type]


def test_subscription_running_window() -> None:
    now = datetime.now(UTC)
    assert _subscription().is_running(now=now)
    assert _subscription(ends_at=None).is_running(now=now)
    assert not _subscription(status="expired").is_running(now=now)
    assert not _subscription(status="cancelled").is_running(now=now)
    assert not _subscription(starts_at=now + timedelta(days=1)).is_running(now=now)
    assert not _subscription(ends_at=now - timedelta(minutes=1)).is_running(now=now)


def test_grace_period_keeps_subscription_running() -> None:
    """Льготный период существует ровно для того, чтобы дожить до оплаты."""

    now = datetime.now(UTC)
    expired = _subscription(ends_at=now - timedelta(hours=1))
    assert not expired.is_running(now=now)
    with_grace = _subscription(
        ends_at=now - timedelta(hours=1), grace_until=now + timedelta(days=2)
    )
    assert with_grace.is_running(now=now)
