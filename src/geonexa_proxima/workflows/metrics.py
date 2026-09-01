"""Почасовой пересчёт суточных агрегатов.

Пересчитываются последние N дней, а не только сегодняшний: доставка и
дозагруженные цитирования приходят с опозданием, и без окна они бы навсегда
выпали из статистики. Запись идёт через UPSERT, поэтому повторный прогон
безопасен.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from prefect import flow, get_run_logger
from sqlalchemy import text

from geonexa_proxima.metrics.rollups import RETENTION_ROLLUP, SCOPES
from geonexa_proxima.services.container import load_container


@flow(name="geonexa-metrics-rollup", log_prints=True)
async def metrics_rollup_flow(
    *,
    bootstrap_target: str | None = None,
    day_from: date | None = None,
    day_to: date | None = None,
    scope: str = "all",
) -> dict[str, Any]:
    """Пересчитать агрегаты за окно. Идемпотентно: повторный прогон безопасен."""

    logger = get_run_logger()
    container = load_container(target=bootstrap_target)
    settings = container.settings
    if not settings.metrics_enabled:
        # Выключатель существует ради стендов и разбора инцидентов: считать
        # агрегаты по неполным данным хуже, чем не считать вовсе.
        logger.info("METRICS_ENABLED=false — пересчёт пропущен")
        await container.close()
        return {"skipped": "metrics_disabled"}
    today = date.today()
    end = day_to or today
    start = day_from or (end - timedelta(days=settings.metrics_rollup_lookback_days - 1))
    run_id = uuid.uuid4()
    written = 0
    # Разрез проверяем до открытия транзакции. Неизвестное имя роняло флоу
    # `KeyError` внутри того же `begin()`, в котором писалась строка `running`:
    # она откатывалась, а обработчик ошибки обновлял её по идентификатору и не
    # находил ничего. Прогон исчезал из `metrics_rollup_runs` целиком — ровно
    # тот случай, ради которого эта таблица и заведена.
    #
    # `retention` и `llm` схема разрешает, но своего запроса в SCOPES у них нет:
    # удержание считается отдельной веткой ниже, расход моделей — не здесь.
    known = {*SCOPES, "all", "retention"}
    if scope not in known:
        raise ValueError(
            f"Неизвестный разрез метрик: {scope!r}. Доступны: {', '.join(sorted(known))}"
        )
    # Запись о прогоне живёт в своей транзакции. В общей она откатывалась
    # вместе с любой ошибкой пересчёта — таймаутом запроса, обрывом связи, — и
    # обработчик ошибки обновлял несуществующую строку. Прогон исчезал из
    # таблицы целиком, и «встало» было не отличить от «честный ноль».
    async with container.require_engine().begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO metrics_rollup_runs (id, scope, day_from, day_to, status) "
                "VALUES (:id, :scope, :day_from, :day_to, 'running')"
            ),
            {"id": str(run_id), "scope": scope, "day_from": start, "day_to": end},
        )
    try:
        async with container.require_engine().begin() as connection:
            if scope == "all":
                targets = SCOPES
            else:
                targets = {scope: SCOPES[scope]} if scope in SCOPES else {}
            params = {"tz": settings.metrics_timezone, "day_from": start, "day_to": end}
            for name, statement in targets.items():
                result = await connection.execute(statement, params)
                written += result.rowcount or 0
                logger.info("Агрегат %s: строк %s", name, result.rowcount)
            if scope in ("all", "retention"):
                result = await connection.execute(
                    RETENTION_ROLLUP,
                    {"tz": settings.metrics_timezone, "weeks": settings.metrics_cohort_weeks},
                )
                written += result.rowcount or 0
            await connection.execute(
                text(
                    "UPDATE metrics_rollup_runs SET status='succeeded', finished_at=now(), "
                    "rows_written=:rows, duration_seconds="
                    "EXTRACT(EPOCH FROM (now() - started_at)) WHERE id=:id"
                ),
                {"id": str(run_id), "rows": written},
            )
        return {"scope": scope, "from": str(start), "to": str(end), "rows": written}
    except Exception as error:
        async with container.require_engine().begin() as connection:
            await connection.execute(
                text(
                    "UPDATE metrics_rollup_runs SET status='failed', finished_at=now(), "
                    "error=:error WHERE id=:id"
                ),
                {"id": str(run_id), "error": str(error)[:4000]},
            )
        raise
    finally:
        await container.close()
