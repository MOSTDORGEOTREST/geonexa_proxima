"""Регистрация флоу в Prefect и синхронизация расписаний из БД.

Расписания живут в таблице ``schedules``: админка правит строку, а этот модуль
переносит правку в Prefect. Источник намерения — база, источник исполнения —
Prefect; путать их нельзя, иначе изменение через админку потеряется при
следующем деплое.

Каждому флоу соответствует ровно один deployment с предсказуемым именем,
поэтому повторная регистрация обновляет существующий, а не плодит копии.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from geonexa_proxima.config import Settings
from geonexa_proxima.flow_catalog import FLOWS, WORK_POOL_DEFAULT, WORK_QUEUE_DEFAULT


async def load_schedules(engine: AsyncEngine) -> dict[str, dict[str, Any]]:
    """Расписания из БД — то, что администратор задал в админке."""

    async with engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    text(
                        "SELECT key, name, kind, cron, interval_seconds, timezone, enabled, "
                        "parameters, prefect_deployment_id FROM schedules"
                    )
                )
            )
            .mappings()
            .all()
        )
    return {row["key"]: dict(row) for row in rows}


async def mark_synced(
    engine: AsyncEngine, key: str, deployment_id: str | None, *, pending: bool = False
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE schedules SET prefect_deployment_id = :deployment_id, "
                "sync_pending = :pending, updated_at = now() WHERE key = :key"
            ),
            {"key": key, "deployment_id": deployment_id, "pending": pending},
        )


async def ensure_work_pool(name: str, *, pool_type: str = "process") -> None:
    """Создать рабочий пул, если его ещё нет.

    Порядок в entrypoint такой: сначала регистрация деплойментов, потом старт
    воркера. Пул создаёт воркер, поэтому на чистой установке первого запуска
    пула ещё не существует, и `apply()` падает с «Work pool … not found» —
    все десять флоу разом. Создаём его сами: операция идемпотентная.
    """

    from prefect.client.orchestration import get_client
    from prefect.client.schemas.actions import WorkPoolCreate
    from prefect.exceptions import ObjectAlreadyExists

    async with get_client() as client:
        try:
            await client.create_work_pool(WorkPoolCreate(name=name, type=pool_type))
        except ObjectAlreadyExists:
            return


async def deploy_all(
    engine: AsyncEngine,
    settings: Settings,
    *,
    work_pool: str | None = None,
    image: str | None = None,
) -> dict[str, Any]:
    """Зарегистрировать все флоу с расписаниями из БД.

    Вызывается при старте prefect-worker: после него админка видит готовые
    deployment'ы и может запускать их вручную.
    """

    from prefect import flow as prefect_flow
    from prefect.client.schemas.schedules import CronSchedule, IntervalSchedule
    from prefect.deployments.runner import EntrypointType

    pool = work_pool or settings.prefect_work_pool or WORK_POOL_DEFAULT
    queue = settings.prefect_work_queue or WORK_QUEUE_DEFAULT
    await ensure_work_pool(pool)
    schedules = await load_schedules(engine)
    report: dict[str, Any] = {"deployed": [], "skipped": [], "failed": {}}

    for spec in FLOWS:
        row = schedules.get(spec.key)
        try:
            loaded = (
                prefect_flow.from_source(
                    source=str(_project_root()),
                    entrypoint=spec.entrypoint,
                )
                if image
                else _import_flow(spec.entrypoint)
            )

            schedule = None
            if row and row["enabled"]:
                if row["cron"]:
                    schedule = CronSchedule(cron=row["cron"], timezone=row["timezone"])
                elif row["interval_seconds"]:
                    schedule = IntervalSchedule(interval=int(row["interval_seconds"]))

            parameters = (row["parameters"] if row else None) or spec.parameters or {}
            tags = ["geonexa", spec.schedule_kind]
            # Процессный воркер по умолчанию запускает флоу во временном
            # каталоге. Для нас это дважды плохо: кода флоу там нет (он в
            # установленном пакете), и относительные пути из конфигурации —
            # `config/harvest.yaml`, `models/…` — перестают разрешаться.
            # Прибиваем рабочий каталог к корню проекта.
            job_variables = {"working_dir": str(_project_root())}

            if image:
                deployment_id = await loaded.deploy(
                    name=spec.key,
                    work_pool_name=pool,
                    work_queue_name=queue,
                    image=image,
                    build=False,
                    push=False,
                    schedule=schedule,
                    description=spec.description,
                    parameters=parameters,
                    tags=tags,
                    job_variables=job_variables,
                )
            else:
                # Два шага, и оба с await. `to_deployment` в асинхронном
                # контексте отдаёт корутину, а не RunnerDeployment: цепочка
                # `.to_deployment(...).apply()` молча уходила в
                # «'coroutine' object has no attribute 'apply'», и ни один
                # deployment не регистрировался — воркер при этом стартовал.
                deployment = await loaded.to_deployment(
                    name=spec.key,
                    schedule=schedule,
                    description=spec.description,
                    parameters=parameters,
                    tags=tags,
                    # Пул задаём здесь: без него deployment попадает никуда,
                    # и воркер, слушающий geonexa-pool, его не увидит.
                    work_pool_name=pool,
                    work_queue_name=queue,
                    job_variables=job_variables,
                    # Флоу берётся импортом модуля, а не чтением файла по пути.
                    # Пакет установлен в образ, файла рядом с процессом нет, и
                    # с FILE_PATH воркер падал на
                    # «FileNotFoundError: …/src/geonexa_proxima/workflows/harvest.py».
                    entrypoint_type=EntrypointType.MODULE_PATH,
                )
                deployment_id = await deployment.apply()

            await mark_synced(engine, spec.key, str(deployment_id))
            report["deployed"].append(spec.key)
        except Exception as error:  # регистрация одного флоу не должна валить остальные
            report["failed"][spec.key] = str(error)
            if row:
                await mark_synced(engine, spec.key, row["prefect_deployment_id"], pending=True)
    return report


def _import_flow(entrypoint: str) -> Any:
    module_name, _, attribute = entrypoint.partition(":")
    module = __import__(module_name, fromlist=[attribute])
    return getattr(module, attribute)


def _project_root() -> Any:
    from pathlib import Path

    return Path(__file__).resolve().parents[3]
