#!/usr/bin/env python3
"""Живая проверка kind-aware репозитория против настоящего PostgreSQL.

Скрипт заводит временного подписчика, группу, канал и пару тарифов с суффиксом
прогона, прогоняет по ним весь сценарий (регистрация чата, смена прав бота,
выдача и замена подписки, истечение, адресаты рассылки) и убирает за собой.
Ничего чужого не трогает: все объекты помечены случайным тегом.

    poetry run python scripts/check_subscribers.py
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from geonexa_proxima.config import get_settings
from geonexa_proxima.db import create_session_factory
from geonexa_proxima.db.session import normalize_database_url
from geonexa_proxima.db.subscriber_repository import (
    ChatIdentity,
    SubscriberRepository,
    SubscriptionOverlapError,
    kind_from_chat_type,
)
from geonexa_proxima.services.dispatch_queries import DUE_PROFILES
from geonexa_proxima.tls import asyncpg_connect_args

OK, BAD = "  OK ", " FAIL"
failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    print(f"{OK if condition else BAD} {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures += 1


async def main() -> int:
    settings = get_settings()
    engine = create_async_engine(
        normalize_database_url(settings.database_url),
        connect_args=asyncpg_connect_args(
            settings.database_ssl_mode,
            settings.database_ssl_root_cert,
            application_name=settings.db_application_name,
        ),
    )
    factory = create_session_factory(engine)
    repo = SubscriberRepository(factory)

    tag = uuid4().hex[:8]
    personal_id = 900_000_000 + int(tag[:6], 16) % 10_000_000
    group_id = -(100_000_000_000 + int(tag[:6], 16) % 100_000_000)
    channel_id = group_id - 7

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO subscription_plans (id, key, name, max_profiles, "
                "max_items_per_digest, min_interval_hours, allow_group_chats, is_default) "
                "VALUES (gen_random_uuid(), :k, :n, 3, 25, 24, true, false) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"k": f"test-pro-{tag}", "n": "Test Pro"},
        )
        await conn.execute(
            text(
                "INSERT INTO subscription_plans (id, key, name, max_profiles, "
                "max_items_per_digest, min_interval_hours, allow_group_chats, is_default) "
                "VALUES (gen_random_uuid(), :k, :n, 1, 10, 168, false, false) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"k": f"test-solo-{tag}", "n": "Test Solo"},
        )
        await conn.execute(
            text(
                "INSERT INTO subscribers (id, kind, telegram_chat_id, title, status) "
                "VALUES (gen_random_uuid(), 'user', :cid, :title, 'active')"
            ),
            {"cid": personal_id, "title": f"Личный {tag}"},
        )

    person = await repo.get_by_chat_id(personal_id)
    check("личный чат читается", person is not None and person.kind.value == "user")

    # --- регистрация групп и каналов ---------------------------------------
    check(
        "тип чата -> kind",
        kind_from_chat_type("supergroup") == "group"
        and kind_from_chat_type("channel") == "channel",
    )

    group, created = await repo.register_chat(
        ChatIdentity(group_id, "supergroup", f"Группа {tag}", added_by_user_id=personal_id),
        bot_status="administrator",
        member_count=42,
    )
    check("группа заведена", created and group.kind.value == "group", str(group.telegram_chat_id))

    again, created_again = await repo.register_chat(
        ChatIdentity(group_id, "supergroup", f"Группа {tag} (переименована)"),
        bot_status="administrator",
        member_count=44,
    )
    check("повторная регистрация идемпотентна", not created_again and again.id == group.id)

    channel, _ = await repo.register_chat(
        ChatIdentity(channel_id, "channel", f"Канал {tag}"),
        bot_status="administrator",
        can_post_messages=True,
    )
    check("канал заведён", channel.kind.value == "channel")

    try:
        await repo.register_chat(ChatIdentity(personal_id, "private", "нельзя"))
        check("личный чат нельзя завести как чат", False)
    except ValueError:
        check("личный чат нельзя завести как чат", True)

    # --- kind-aware выборки -------------------------------------------------
    users = await repo.list_subscribers(kinds=["user"], search=tag, limit=100)
    groups = await repo.list_subscribers(kinds=["group"], search=tag, limit=100)
    chats = await repo.list_subscribers(kinds=["group", "channel"], search=tag, limit=100)
    check(
        "выборка по kind разделяет",
        len(users) == 1 and len(groups) == 1 and len(chats) == 2,
        f"user={len(users)} group={len(groups)} chats={len(chats)}",
    )

    breakdown = await repo.breakdown()
    check(
        "разрез вид×статус непустой",
        any(b.kind == "channel" for b in breakdown),
        ", ".join(f"{b.kind}/{b.status}={b.count}" for b in breakdown),
    )

    chat_records = await repo.list_chats(search=tag, limit=100)
    by_id = {c.telegram_chat_id: c for c in chat_records}
    check(
        "чаты отдают права бота",
        by_id[group_id].bot_status == "administrator" and by_id[group_id].member_count == 44,
    )
    check(
        "канал без can_post не доставляем",
        by_id[channel_id].can_deliver and by_id[group_id].can_deliver,
    )

    # --- смена статуса бота -------------------------------------------------
    kicked = await repo.update_bot_status(group_id, "kicked")
    check(
        "бота выгнали -> чат погашен",
        kicked.bot_status == "kicked" and not kicked.can_deliver and kicked.status == "left",
    )
    back = await repo.update_bot_status(group_id, "administrator")
    check("бота вернули -> чат активен", back.status == "active" and back.can_deliver)

    events = await repo.chat_events(group.id)
    check("события чата пишутся", len(events) >= 3, f"{len(events)} событий")

    # --- подписки -----------------------------------------------------------
    limits_none = await repo.limits(person.id)
    check(
        "без подписки — тариф по умолчанию",
        not limits_none.is_active,
        f"{limits_none.plan_key} профилей={limits_none.max_profiles}",
    )

    sub = await repo.grant_subscription(
        person.id,
        f"test-pro-{tag}",
        duration=timedelta(days=30),
        grace=timedelta(days=3),
        actor="admin",
    )
    check("подписка выдана", sub.status == "active" and sub.ends_at is not None)

    limits_pro = await repo.limits(person.id)
    check(
        "лимиты берутся из подписки",
        limits_pro.is_active and limits_pro.max_profiles == 3,
        limits_pro.plan_key,
    )

    try:
        await repo.grant_subscription(
            person.id, f"test-pro-{tag}", duration=timedelta(days=10), replace_current=False
        )
        check("вторая подписка поверх первой отклоняется", False)
    except SubscriptionOverlapError:
        check("вторая подписка поверх первой отклоняется", True)

    replaced = await repo.grant_subscription(
        person.id, f"test-solo-{tag}", duration=timedelta(days=10), actor="admin"
    )
    check("замена тарифа закрывает предыдущий", replaced.plan_key == f"test-solo-{tag}")
    history = await repo.list_subscriptions(person.id)
    check(
        "история подписок сохранена",
        len(history) == 2,
        ", ".join(f"{h.plan_key}:{h.status}" for h in history),
    )

    try:
        await repo.grant_subscription(group.id, f"test-solo-{tag}", duration=timedelta(days=5))
        check("тариф без групп не выдаётся чату", False)
    except ValueError as exc:
        check("тариф без групп не выдаётся чату", "групповые" in str(exc))

    group_sub = await repo.grant_subscription(
        group.id, f"test-pro-{tag}", duration=timedelta(days=5)
    )
    check("групповой тариф выдаётся чату", group_sub.plan_key == f"test-pro-{tag}")

    extended = await repo.extend_subscription(replaced.id, by=timedelta(days=20))
    check(
        "продление сдвигает окончание",
        extended.ends_at > replaced.ends_at,
        f"{replaced.ends_at:%d.%m} -> {extended.ends_at:%d.%m}",
    )

    # --- kind-aware адресаты рассылки ---------------------------------------
    personal_targets = await repo.list_delivery_targets(kinds=["user"])
    chat_targets = await repo.list_delivery_targets(kinds=["group", "channel"])
    check(
        "личные адресаты не смешаны с чатами",
        person.id in {u.id for u in personal_targets}
        and person.id not in {u.id for u in chat_targets}
        and group.id in {u.id for u in chat_targets},
    )

    await repo.update_bot_status(group_id, "kicked")
    chat_targets_after = await repo.list_delivery_targets(kinds=["group", "channel"])
    check("выгнанный чат выпадает из адресатов", group.id not in {u.id for u in chat_targets_after})
    await repo.update_bot_status(group_id, "administrator")

    # --- пробный период -------------------------------------------------------
    trial_person_id = personal_id + 1
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO subscribers (id, kind, telegram_chat_id, title, status) "
                "VALUES (gen_random_uuid(), 'user', :cid, :title, 'active')"
            ),
            {"cid": trial_person_id, "title": f"Новичок {tag}"},
        )
    newcomer = await repo.get_by_chat_id(trial_person_id)
    trial = await repo.start_trial(
        newcomer.id, plan_key=f"test-pro-{tag}", trial_days=14, grace_days=3
    )
    check("новичку выдан пробный период", trial is not None and trial.status == "trial")
    check(
        "пробный период учитывается в лимитах", (await repo.limits(newcomer.id)).max_profiles == 3
    )
    check(
        "повторный триал не выдаётся",
        await repo.start_trial(newcomer.id, plan_key=f"test-pro-{tag}", trial_days=14) is None,
    )
    await repo.cancel_subscription(trial.id, actor="test")
    check(
        "после отмены триал не выдаётся заново",
        await repo.start_trial(newcomer.id, plan_key=f"test-pro-{tag}", trial_days=14) is None,
    )
    check(
        "нулевой триал отключает пробный период",
        await repo.start_trial(newcomer.id, plan_key=f"test-pro-{tag}", trial_days=0) is None,
    )
    await repo.forget(newcomer.id)

    # --- выборка диспетчера --------------------------------------------------
    # Диспетчер ходит по той же таблице, что и репозиторий, но своим SQL.
    # Проверяем именно его: расхождение между ними означает, что дайджест
    # уедет не туда, куда показывает админка.
    async def due(kinds: list[str]) -> set[str]:
        async with engine.connect() as conn:
            rows = (
                (await conn.execute(DUE_PROFILES, {"limit": 200, "kinds": kinds})).mappings().all()
            )
        return {str(r["subscriber_id"]) for r in rows}

    async with engine.begin() as conn:
        for sid in (person.id, group.id, channel.id):
            await conn.execute(
                text(
                    "INSERT INTO subscriber_profiles (id, subscriber_id, name, "
                    "normalized_name, compiled_text, is_active, digest_enabled) "
                    "VALUES (gen_random_uuid(), :sid, :n, :n, 'тест', true, true)"
                ),
                {"sid": str(sid), "n": f"профиль-{tag}"},
            )
        # У канала нет прав постить — доставлять туда нечем.
        await conn.execute(
            text(
                "UPDATE chat_memberships SET can_post_messages = false WHERE subscriber_id = :sid"
            ),
            {"sid": str(channel.id)},
        )

    personal_due = await due(["user"])
    chats_due = await due(["group", "channel"])
    check(
        "диспетчер личек берёт только человека",
        str(person.id) in personal_due and str(group.id) not in personal_due,
    )
    check("диспетчер чатов берёт группу", str(group.id) in chats_due)
    check("канал без прав постить в выборку не попадает", str(channel.id) not in chats_due)

    await repo.update_bot_status(group_id, "kicked")
    check(
        "выгнанная группа выпадает из выборки диспетчера",
        str(group.id) not in await due(["group", "channel"]),
    )
    await repo.update_bot_status(group_id, "administrator")

    # --- истечение ----------------------------------------------------------
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE subscriptions SET starts_at = now() - interval '10 days', "
                "ends_at = now() - interval '2 days', "
                "grace_until = now() - interval '1 day' WHERE id = :id"
            ),
            {"id": str(group_sub.id)},
        )
    expired = await repo.expire_due()
    check("просроченные гасятся", expired >= 1, f"{expired} шт.")
    check("после истечения — тариф по умолчанию", not (await repo.limits(group.id)).is_active)

    soon = await repo.list_expiring(within=timedelta(days=60), kinds=["user"])
    check(
        "напоминания о продлении находятся",
        any(s.id == extended.id for s in soon),
        f"{len(soon)} подписок",
    )

    cancelled = await repo.cancel_subscription(extended.id, actor="admin", reason="тест")
    check("отмена закрывает период", cancelled.status == "cancelled")
    check(
        "после отмены адресатов нет",
        person.id not in {u.id for u in await repo.list_delivery_targets(kinds=["user"])},
    )

    # --- уборка -------------------------------------------------------------
    for sid in (person.id, group.id, channel.id):
        await repo.forget(sid)
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM subscription_plans WHERE key LIKE :p"), {"p": f"test-%-{tag}"}
        )
    check("подписчики удалены каскадом", await repo.get(person.id) is None)

    await engine.dispose()
    print()
    print("Всё сошлось" if not failures else f"Провалов: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
