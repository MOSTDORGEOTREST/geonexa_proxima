#!/usr/bin/env python3
"""Живая проверка очереди доставки и уборки старых событий.

Очередь — единственное место, где параллельные воркеры делят строки одной
таблицы. Ошибка здесь означает либо двойную отправку, либо застрявшее навсегда
задание, и обе видны только под нагрузкой. Скрипт заводит временные записи,
прогоняет по ним весь цикл и убирает за собой.

    poetry run python scripts/check_delivery.py
"""

from __future__ import annotations

import asyncio
from itertools import pairwise
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from geonexa_proxima.config import get_settings
from geonexa_proxima.db.session import normalize_database_url
from geonexa_proxima.metrics.purge import purge
from geonexa_proxima.services.delivery import (
    GROUP,
    PERSONAL,
    DeliveryQueue,
    next_retry_delay,
    rate_limit_delay,
    target_channel,
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
    chat_id = 970_000_000 + int(tag[:6], 16) % 9_000_000

    async with engine.begin() as conn:
        # Прерванный прогон оставляет за собой задания, и следующий прогон
        # разбирал бы их вместе со своими. Скрипт обязан быть перезапускаемым.
        await conn.execute(text("DELETE FROM subscribers WHERE title LIKE 'Очередь %'"))
        subscriber_id = (
            await conn.execute(
                text(
                    "INSERT INTO subscribers (id, kind, telegram_chat_id, title, status) "
                    "VALUES (gen_random_uuid(), 'user', :cid, :t, 'active') RETURNING id"
                ),
                {"cid": chat_id, "t": f"Очередь {tag}"},
            )
        ).scalar_one()
        profile_id = (
            await conn.execute(
                text(
                    "INSERT INTO subscriber_profiles (id, subscriber_id, name, normalized_name,"
                    " compiled_text, is_active) VALUES (gen_random_uuid(), :s, :n, :n, 'тест',"
                    " true) RETURNING id"
                ),
                {"s": str(subscriber_id), "n": f"профиль-{tag}"},
            )
        ).scalar_one()
        digest_ids = [
            (
                await conn.execute(
                    text(
                        "INSERT INTO digests (id, subscriber_id, profile_id, period_start,"
                        " period_end, status) VALUES (gen_random_uuid(), :s, :p,"
                        " now() - interval '7 days', now(), 'ready') RETURNING id"
                    ),
                    {"s": str(subscriber_id), "p": str(profile_id)},
                )
            ).scalar_one()
            for _ in range(12)
        ]

    queue = DeliveryQueue(engine, retry_backoff_seconds=settings.delivery_retry_backoff_seconds)
    check(
        "backoff берётся из настроек",
        queue.retry_backoff_seconds == settings.delivery_retry_backoff_seconds,
        f"{queue.retry_backoff_seconds} с",
    )

    for digest_id in digest_ids:
        await queue.enqueue(digest_id, subscriber_id, PERSONAL, chat_id, payload={"blocks": []})
    repeated = await queue.enqueue(digest_ids[0], subscriber_id, PERSONAL, chat_id)
    check("повторная постановка того же дайджеста игнорируется", repeated is None)

    # --- параллельные воркеры не должны пересечься -------------------------
    workers = [DeliveryQueue(engine, worker_id=f"w{i}", retry_backoff_seconds=5) for i in range(4)]
    batches = await asyncio.gather(*(w.claim(PERSONAL, batch_size=5) for w in workers))
    claimed = [job.id for batch in batches for job in batch]
    check(
        "четыре воркера разобрали задания без пересечений",
        len(claimed) == len(set(claimed)) == len(digest_ids),
        f"{len(claimed)} заданий, уникальных {len(set(claimed))}",
    )
    check("чужой канал ничего не забирает", not await workers[0].claim(GROUP, batch_size=5))

    # --- ретраи и признание провала ----------------------------------------
    victim = batches[0][0]
    delays = []
    for _ in range(6):
        # Реальный воркер всегда помечает отправку до попытки: именно здесь
        # растёт счётчик попыток, от которого зависят backoff и признание провала.
        await workers[0].mark_sending(victim.id)
        state = await workers[0].mark_failed(victim.id, "проба")
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT attempts, status, EXTRACT(EPOCH FROM (next_retry_at - now()))"
                            " AS wait FROM delivery_jobs WHERE id = :id"
                        ),
                        {"id": str(victim.id)},
                    )
                )
                .mappings()
                .one()
            )
        delays.append(None if row["wait"] is None else round(float(row["wait"])))
        if state == "failed":
            break
    check("после исчерпания попыток задание признано провалившимся", row["status"] == "failed")
    growing = [d for d in delays if d]
    check(
        "задержка растёт экспоненциально",
        all(b > a for a, b in pairwise(growing)),
        " → ".join(f"{d}с" for d in growing),
    )
    check(
        "формула в Python совпадает с SQL",
        next_retry_delay(1, 5) == 5 and next_retry_delay(3, 5) == 20,
    )

    # --- зависшие задания возвращаются -------------------------------------
    stuck = batches[1][0]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE delivery_jobs SET status='sending', claimed_at = now() -"
                " interval '2 hours' WHERE id = :id"
            ),
            {"id": str(stuck.id)},
        )
    released = await queue.release_stale(older_than_minutes=30)
    check("зависшее после смерти воркера задание вернулось в очередь", released >= 1)

    # --- лимиты --------------------------------------------------------------
    check(
        "глобальный лимит учитывается вместе с чатовым",
        rate_limit_delay(PERSONAL, 1.0, 20, 25) == 1.0
        and rate_limit_delay(PERSONAL, 100.0, 20, 2) == 0.5,
        "самый строгий из применимых",
    )
    check(
        "канал задания определяется видом подписчика",
        target_channel("user") == PERSONAL and target_channel("channel") == GROUP,
    )

    # --- уборка ---------------------------------------------------------------
    async with engine.begin() as conn:
        profile_key = (
            await conn.execute(text("SELECT id FROM harvest_profiles LIMIT 1"))
        ).scalar_one()
        run_id = (
            await conn.execute(
                text(
                    "INSERT INTO harvest_runs (id, harvest_profile_id, trigger, status)"
                    " VALUES (gen_random_uuid(), :p, 'manual', 'succeeded') RETURNING id"
                ),
                {"p": str(profile_key)},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO harvest_decisions (harvest_run_id, source, external_id, stage,"
                " decision, title, created_at) VALUES (:r, 'arxiv', :e, 'keyword',"
                " 'rejected', 'старое решение', now() - make_interval(days => :d))"
            ),
            {
                "r": str(run_id),
                "e": f"old-{tag}",
                "d": settings.harvest_decision_retention_days + 5,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO harvest_decisions (harvest_run_id, source, external_id, stage,"
                " decision, title) VALUES (:r, 'arxiv', :e, 'keyword', 'accepted', 'свежее')"
            ),
            {"r": str(run_id), "e": f"new-{tag}"},
        )
    removed = await purge(engine, settings)
    async with engine.connect() as conn:
        left = (
            (
                await conn.execute(
                    text("SELECT external_id FROM harvest_decisions WHERE external_id LIKE :p"),
                    {"p": f"%-{tag}"},
                )
            )
            .scalars()
            .all()
        )
    check("устаревшие решения удалены", removed.get("harvest_decisions", 0) >= 1, str(removed))
    check("свежие решения на месте", left == [f"new-{tag}"], str(left))

    # --- уборка за собой ------------------------------------------------------
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM harvest_decisions WHERE external_id LIKE :p"), {"p": f"%-{tag}"}
        )
        await conn.execute(text("DELETE FROM harvest_runs WHERE id = :id"), {"id": str(run_id)})
        await conn.execute(
            text("DELETE FROM subscribers WHERE id = :id"), {"id": str(subscriber_id)}
        )
    async with engine.connect() as conn:
        rest = (
            await conn.execute(
                text("SELECT count(*) FROM delivery_jobs WHERE subscriber_id = :id"),
                {"id": str(subscriber_id)},
            )
        ).scalar_one()
    check("каскад унёс задания вместе с подписчиком", rest == 0)

    await engine.dispose()
    print()
    print("Всё сошлось" if not failures else f"Провалов: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
