"""Глобальный сбор: один процесс на всю платформу.

Персонализации здесь нет и быть не должно — корпус общий, оценка глобальная.
Флоу только оркеструет: вся логика живёт в сервисах, поэтому её можно
протестировать без Prefect.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from prefect import flow, get_run_logger, task
from prefect.cache_policies import NO_CACHE
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from geonexa_proxima.services.container import load_container


class HarvestAlreadyRunning(RuntimeError):
    """Сбор уже идёт.

    Частичный уникальный индекс `uq_harvest_runs_running` держит инвариант «не
    больше одного прогона одновременно»: два параллельных сбора ходили бы в те
    же источники, дважды тратили токены и гонялись за одни и те же строки.
    Отдельный класс нужен, чтобы это состояние выглядело как внятная фраза, а
    не как двести строк `IntegrityError` в логе.
    """


#: Кэш выключен намеренно. Prefect по умолчанию хеширует аргументы задачи,
#: чтобы понять, можно ли переиспользовать результат. Наши задачи принимают
#: живой контейнер с движком БД и блокировками — он не сериализуется, и Prefect
#: пишет в лог трейсбек «Unable to create hash» на каждом прогоне. Кэшировать
#: тут всё равно нечего: обе задачи пишут в базу, их смысл — побочный эффект.
@task(name="reclaim-stale-harvest-runs", cache_policy=NO_CACHE)
async def reclaim_stale_runs(container: Any, stale_after_minutes: int) -> int:
    """Закрыть прогоны, которые никто не закрыл.

    Процесс сбора может умереть между открытием записи и её закрытием: упал
    воркер, убили контейнер, кончилась память. Запись остаётся в статусе
    `running` навсегда, а уникальный индекс не пускает следующий сбор — и
    система перестаёт собирать вообще, сообщая об этом неразборчивым
    `IntegrityError`. Поэтому перед каждым стартом подбираем брошенное.

    Порог с запасом к длительности живого сбора, но не больше: пока запись не
    подобрана, система не собирает вообще.
    """

    async with container.require_engine().begin() as connection:
        result = await connection.execute(
            text(
                "UPDATE harvest_runs SET status = 'failed', finished_at = now(), "
                "error = coalesce(error, 'Прогон оборван: процесс не закрыл запись. "
                "Помечен неудачным автоматически, иначе сбор был бы заблокирован.') "
                "WHERE status = 'running' "
                "  AND started_at < now() - make_interval(mins => :minutes) "
                "RETURNING id"
            ),
            {"minutes": stale_after_minutes},
        )
        return len(result.fetchall())


@task(name="open-harvest-run", retries=0, cache_policy=NO_CACHE)
async def open_run(container: Any, trigger: str, since: datetime) -> uuid.UUID:
    """Открыть прогон. Частичный unique index не даст запустить второй параллельно."""

    run_id = uuid.uuid4()
    engine = container.require_engine()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO harvest_runs (id, harvest_profile_id, trigger, status, since, "
                    "triggered_by) SELECT :id, p.id, :trigger, 'running', :since, :by "
                    "FROM harvest_profiles p WHERE p.is_active LIMIT 1"
                ),
                {
                    "id": str(run_id),
                    "trigger": trigger,
                    "since": since,
                    "by": f"flow:{trigger}",
                },
            )
    except IntegrityError as error:
        # Транзакция выше уже откачена, поэтому за подробностями идём заново.
        async with engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT id, started_at, triggered_by FROM harvest_runs "
                            "WHERE status = 'running' ORDER BY started_at LIMIT 1"
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise
        raise HarvestAlreadyRunning(
            f"Сбор уже идёт: прогон {row['id']} начат "
            f"{row['started_at']:%d.%m.%Y %H:%M} ({row['triggered_by'] or 'источник неизвестен'}). "
            f"Дождитесь его окончания или отмените в разделе «Прогоны»."
        ) from error
    return run_id


@task(name="close-harvest-run", cache_policy=NO_CACHE)
async def close_run(
    container: Any, run_id: uuid.UUID, status: str, stats: dict[str, Any], error: str | None
) -> None:
    import json

    async with container.require_engine().begin() as connection:
        await connection.execute(
            text(
                "UPDATE harvest_runs SET status=:status, finished_at=now(), "
                "stats=CAST(:stats AS jsonb), error=:error WHERE id=:id"
            ),
            {
                "id": str(run_id),
                "status": status,
                "stats": json.dumps(stats, ensure_ascii=False, default=str),
                "error": (error or "")[:4000] or None,
            },
        )


@flow(name="geonexa-global-harvest", log_prints=True)
async def global_harvest_flow(
    *,
    bootstrap_target: str | None = None,
    trigger: str = "schedule",
    lookback_hours: int | None = None,
    limit_per_source: int | None = None,
) -> dict[str, Any]:
    """Собрать, отфильтровать и оценить материалы один раз для всех подписчиков."""

    logger = get_run_logger()
    container = load_container(target=bootstrap_target)
    settings = container.settings
    hours = lookback_hours or settings.collection_lookback_hours
    since = datetime.now(UTC) - timedelta(hours=hours)
    run_id: uuid.UUID | None = None
    try:
        reclaimed = await reclaim_stale_runs(container, settings.harvest_run_stale_minutes)
        if reclaimed:
            logger.warning(
                "Подобрано брошенных прогонов: %s — они висели в статусе running "
                "дольше %s мин и блокировали сбор",
                reclaimed,
                settings.harvest_run_stale_minutes,
            )
        run_id = await open_run(container, trigger, since)
        logger.info("Прогон %s открыт, окно с %s", run_id, since.isoformat())
        service = container.ingestion_service(run_id=run_id)
        stats = await service.ingest(
            since=since,
            lookback_hours=hours,
            limit_per_source=limit_per_source or settings.max_items_per_source,
        )
        # Журнал решений и счётчики терминов копятся пачками: без сброса
        # последняя пачка не доехала бы до базы.
        await service.flush_journals()
        payload = stats.as_dict()
        await close_run(container, run_id, "succeeded", payload, None)
        logger.info("Прогон завершён: %s", payload)
        return payload
    except Exception as error:
        if run_id is not None:
            await close_run(container, run_id, "failed", {}, str(error))
        raise
    finally:
        await container.close()
