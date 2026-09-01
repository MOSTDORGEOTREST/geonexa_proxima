#!/usr/bin/env python3
"""Живая проверка диспетчера: кому и как часто уходит дайджест.

Самая дорогая ошибка этого узла — не падение, а тишина: если срок следующего
дайджеста не двигается, диспетчер отбирает один и тот же профиль на каждом
прогоне и рассылает дубликаты. Проверяется именно это.

    poetry run python scripts/check_dispatch.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from geonexa_proxima.config import get_settings
from geonexa_proxima.db.session import normalize_database_url
from geonexa_proxima.services.dispatch_queries import (
    DUE_PROFILES,
    PROFILE_CONTEXT,
    SCHEDULE_NEXT,
    digest_interval_hours,
)
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
    tag = uuid4().hex[:8]
    chat_id = 950_000_000 + int(tag[:6], 16) % 9_000_000

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM subscribers WHERE title LIKE 'Диспетчер %'"))
        subscriber_id = (
            await conn.execute(
                text(
                    "INSERT INTO subscribers (id, kind, telegram_chat_id, title, status)"
                    " VALUES (gen_random_uuid(), 'user', :cid, :t, 'active') RETURNING id"
                ),
                {"cid": chat_id, "t": f"Диспетчер {tag}"},
            )
        ).scalar_one()
        profile_id = (
            await conn.execute(
                text(
                    "INSERT INTO subscriber_profiles (id, subscriber_id, name, normalized_name,"
                    " compiled_text, is_active, digest_enabled) VALUES (gen_random_uuid(), :s,"
                    " :n, :n, 'тест', true, true) RETURNING id"
                ),
                {"s": str(subscriber_id), "n": f"профиль-{tag}"},
            )
        ).scalar_one()
        plan_id = (
            await conn.execute(
                text(
                    "INSERT INTO subscription_plans (id, key, name, max_profiles,"
                    " max_items_per_digest, min_interval_hours, allow_group_chats)"
                    " VALUES (gen_random_uuid(), :k, 'Диспетчер-тест', 3, 25, 48, true)"
                    " RETURNING id"
                ),
                {"k": f"dispatch-{tag}"},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO subscriptions (id, subscriber_id, plan_id, status, starts_at,"
                " ends_at) VALUES (gen_random_uuid(), :s, :p, 'active', now() - interval"
                " '1 day', now() + interval '30 days')"
            ),
            {"s": str(subscriber_id), "p": str(plan_id)},
        )

    async def due() -> set[str]:
        async with engine.connect() as conn:
            rows = (
                (await conn.execute(DUE_PROFILES, {"limit": 100, "kinds": ["user"]}))
                .mappings()
                .all()
            )
        return {str(r["profile_id"]) for r in rows}

    check("новый профиль сразу попадает в выборку", str(profile_id) in await due())

    # --- контекст профиля --------------------------------------------------
    async with engine.connect() as conn:
        row = (
            (await conn.execute(PROFILE_CONTEXT, {"id": str(profile_id), "default_interval": 168}))
            .mappings()
            .first()
        )
    check(
        "интервал берётся из тарифа",
        row["min_interval_hours"] == 48,
        f"{row['min_interval_hours']} ч",
    )

    interval = digest_interval_hours(
        plan_minimum=row["min_interval_hours"], profile_settings=row["digest_settings"]
    )
    check(
        "профиль не может просить чаще тарифа",
        digest_interval_hours(plan_minimum=48, profile_settings={"interval_hours": 1}) == 48,
    )

    # --- срок двигается ----------------------------------------------------
    async with engine.begin() as conn:
        next_at = (
            await conn.execute(SCHEDULE_NEXT, {"id": str(profile_id), "hours": interval})
        ).scalar_one()
    check("срок следующего дайджеста назначен", next_at is not None, str(next_at))
    check(
        "срок примерно через интервал тарифа",
        abs((next_at - datetime.now(UTC)).total_seconds() - interval * 3600) < 120,
        f"{(next_at - datetime.now(UTC)).total_seconds() / 3600:.1f} ч",
    )

    check(
        "после дайджеста профиль выпадает из выборки",
        str(profile_id) not in await due(),
        "иначе диспетчер слал бы дубликаты на каждом прогоне",
    )

    # --- когда срок наступил -----------------------------------------------
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE subscriber_profiles SET next_digest_at = now() - interval '1 minute'"
                " WHERE id = :id"
            ),
            {"id": str(profile_id)},
        )
    check("по наступлении срока профиль возвращается", str(profile_id) in await due())

    # --- подписка кончилась -------------------------------------------------
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE subscriptions SET status = 'expired' WHERE subscriber_id = :s"),
            {"s": str(subscriber_id)},
        )
    check("без действующей подписки дайджест не строится", str(profile_id) not in await due())

    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE subscriptions SET status = 'active' WHERE subscriber_id = :s"),
            {"s": str(subscriber_id)},
        )
        await conn.execute(
            text(
                "UPDATE subscriber_profiles SET paused_until = now() + interval '1 day'"
                " WHERE id = :id"
            ),
            {"id": str(profile_id)},
        )
    check("на паузе профиль пропускается", str(profile_id) not in await due())

    # --- уборка -------------------------------------------------------------
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM subscribers WHERE id = :id"), {"id": str(subscriber_id)}
        )
        await conn.execute(
            text("DELETE FROM subscription_plans WHERE key = :k"), {"k": f"dispatch-{tag}"}
        )
    await engine.dispose()
    print()
    print("Всё сошлось" if not failures else f"Провалов: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
