"""Единая точка подъёма сервиса.

Каждый контейнер — api, bot, prefect-worker — начинает с одного и того же:
дождаться базу, привести схему, засеять обязательные записи. Дублировать это
в трёх Dockerfile значило бы получить три слегка разных поведения.

Гонку при одновременном старте снимает advisory lock: миграции выполняет тот,
кто взял блокировку первым, остальные ждут и работают с готовой схемой.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from geonexa_proxima.bootstrap.seed import seed_all
from geonexa_proxima.config import Settings
from geonexa_proxima.db.session import get_engine, pool_snapshot

log = logging.getLogger(__name__)

# Произвольное, но постоянное число: важно лишь, чтобы все процессы платформы
# брали одну и ту же блокировку.
BOOTSTRAP_LOCK_ID = 8_237_411


async def wait_for_database(
    engine: AsyncEngine, *, attempts: int = 30, delay_seconds: float = 2.0
) -> None:
    """Дождаться готовности базы.

    В compose контейнер приложения стартует раньше, чем PostgreSQL успевает
    принять соединения; падать на первой попытке — значит требовать ручного
    перезапуска на каждом подъёме стека.
    """

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            if attempt > 1:
                log.info("База ответила с %s попытки", attempt)
            return
        except (SQLAlchemyError, OSError) as error:
            last_error = error
            if attempt == attempts:
                break
            await asyncio.sleep(delay_seconds)
    raise RuntimeError(
        f"База не ответила за {attempts} попыток ({attempts * delay_seconds:.0f} с): {last_error}"
    )


async def start_service(
    settings: Settings,
    *,
    service: str,
    auto_migrate: bool | None = None,
    seed: bool | None = None,
) -> dict[str, Any]:
    """Поднять сервис: дождаться базу, привести схему, засеять записи."""

    from geonexa_proxima.bootstrap.schema import ensure_schema, inspect_schema
    from geonexa_proxima.logging import configure_from_settings

    # Логирование настраивается до первой строчки лога, иначе бутстрап пишет
    # мимо той конфигурации, которую задал администратор.
    configure_from_settings(settings)
    engine = get_engine(settings, application_name=f"geonexa-{service}")
    auto_migrate = settings.db_auto_migrate if auto_migrate is None else auto_migrate
    seed = settings.db_auto_seed if seed is None else seed

    await wait_for_database(
        engine,
        attempts=settings.db_wait_attempts,
        delay_seconds=settings.db_wait_delay_seconds,
    )

    report: dict[str, Any] = {"service": service}
    async with engine.begin() as connection:
        # Ждём блокировку, а не пропускаем шаг: иначе второй контейнер пойдёт
        # работать с недомигрированной схемой.
        await connection.execute(
            text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": BOOTSTRAP_LOCK_ID}
        )
        try:
            before = await inspect_schema(engine)
            report["schema_before"] = before.summary
            if not before.is_current:
                if not auto_migrate:
                    raise RuntimeError(
                        f"Схема не соответствует коду ({before.summary}), "
                        f"а DB_AUTO_MIGRATE выключен."
                    )
                log.info("Схема отстаёт (%s) — накатываем миграции", before.summary)
                await ensure_schema(engine, allow_upgrade=True)
            after = await inspect_schema(engine)
            report["schema_after"] = after.summary
            report["migrated"] = not before.is_current
            if seed:
                seed_report = await seed_all(engine, settings)
                report["seed"] = seed_report.as_dict()
                report["seeded"] = seed_report.changed
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": BOOTSTRAP_LOCK_ID}
            )

    report["pool"] = pool_snapshot(engine)
    log.info(
        "Сервис %s готов: %s | пул %s",
        service,
        report.get("schema_after"),
        report["pool"],
    )
    return report
