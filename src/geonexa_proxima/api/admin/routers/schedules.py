"""Расписания и ручной запуск флоу — то, ради чего админка и нужна оператору."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text

from geonexa_proxima.api.admin.deps import (
    Admin,
    AppSettings,
    Engine,
    audit,
    execute,
    fetch_all,
    fetch_one,
    require,
)
from geonexa_proxima.flow_catalog import BY_KEY, FLOWS, coerce_parameters, describe_fields
from geonexa_proxima.services.prefect_admin import (
    PrefectAdmin,
    PrefectUnavailable,
    describe_cron,
)

router = APIRouter(tags=["admin:schedules"])


class ScheduleUpdate(BaseModel):
    cron: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=2_592_000)
    timezone: str | None = None
    enabled: bool | None = None
    parameters: dict[str, Any] | None = None

    @model_validator(mode="after")
    def one_kind_of_schedule(self) -> ScheduleUpdate:
        if self.cron and self.interval_seconds:
            raise ValueError("задайте либо cron, либо interval_seconds")
        return self


class RunRequest(BaseModel):
    parameters: dict[str, Any] | None = None
    #: Дописать переданное поверх параметров расписания (по умолчанию) или
    #: запустить ровно с тем, что передано.
    merge: bool = True


class CronCheck(BaseModel):
    cron: str
    timezone: str = "Europe/Moscow"


def _prefect(request: Request, settings: Any, db: Any) -> PrefectAdmin:
    existing = getattr(request.app.state, "prefect_admin", None)
    if existing is None:
        existing = PrefectAdmin(settings, db)
        request.app.state.prefect_admin = existing
    return existing


@router.get("/schedules")
async def list_schedules(
    admin: Admin,
    db: Engine,
    kind: str | None = None,
    enabled: bool | None = None,
) -> list[dict[str, Any]]:
    """Расписания вместе с описанием флоу и ближайшими запусками."""

    conditions = ["true"]
    params: dict[str, Any] = {}
    if kind:
        conditions.append("kind = :kind")
        params["kind"] = kind
    if enabled is not None:
        conditions.append("enabled = :enabled")
        params["enabled"] = enabled
    rows = await fetch_all(
        db,
        text(f"SELECT * FROM schedules WHERE {' AND '.join(conditions)} ORDER BY kind, key"),
        params,
    )
    for row in rows:
        spec = BY_KEY.get(row["key"])
        row["description"] = spec.description if spec else None
        row["entrypoint"] = spec.entrypoint if spec else None
        # Описание полей едет вместе со строкой: форма параметров в админке
        # строится из него, а не из JSON, который надо подсматривать в коде.
        row["fields"] = describe_fields(spec) if spec else []
        row["default_parameters"] = dict(spec.parameters or {}) if spec else {}
        row["schedule"] = (
            describe_cron(row["cron"], timezone=row.get("timezone") or "Europe/Moscow")
            if row.get("cron")
            else {"valid": True, "next": [], "interval_seconds": row.get("interval_seconds")}
        )
    return rows


@router.get("/schedules/flows")
async def catalog(admin: Admin) -> list[dict[str, Any]]:
    """Каталог флоу — что вообще можно запустить."""

    return [
        {
            "key": spec.key,
            "name": spec.name,
            "entrypoint": spec.entrypoint,
            "description": spec.description,
            "schedule_kind": spec.schedule_kind,
            "parameters": spec.parameters or {},
            "fields": describe_fields(spec),
        }
        for spec in FLOWS
    ]


@router.post("/schedules/validate")
async def validate_cron(payload: CronCheck, admin: Admin) -> dict[str, Any]:
    """Показать, когда выражение сработает, — до сохранения, а не после."""

    return describe_cron(payload.cron, timezone=payload.timezone)


@router.patch("/schedules/{schedule_id}")
async def patch_schedule(
    schedule_id: UUID,
    payload: ScheduleUpdate,
    admin: Admin,
    db: Engine,
    settings: AppSettings,
    request: Request,
) -> dict[str, Any]:
    """Изменить расписание.

    Сначала запись в БД, потом push в Prefect: если оркестратор недоступен,
    правка не теряется, а помечается `sync_pending` и досылается позже.
    """

    row = require(
        await fetch_one(
            db, text("SELECT * FROM schedules WHERE id = :id"), {"id": str(schedule_id)}
        ),
        "Расписание",
    )
    if payload.cron and not describe_cron(payload.cron).get("valid"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Cron-выражение не разбирается")

    if payload.parameters is not None:
        import json

        try:
            parameters = coerce_parameters(row["key"], payload.parameters)
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
        # Параметры пишутся ДО push в Prefect: `_push_schedule` читает их из
        # той же строки, и в прежнем порядке деплоймент получал старые
        # параметры, а новые доезжали только со следующей правкой расписания.
        await execute(
            db,
            text(
                "UPDATE schedules SET parameters = CAST(:parameters AS jsonb),"
                " sync_pending = true, updated_at = now() WHERE id = :id"
            ),
            {
                "parameters": json.dumps(parameters, ensure_ascii=False),
                "id": str(schedule_id),
            },
        )

    client = _prefect(request, settings, db)
    try:
        result = await client.set_schedule(
            row["key"],
            cron=payload.cron or (None if payload.interval_seconds else row["cron"]),
            interval_seconds=payload.interval_seconds
            or (None if payload.cron else row["interval_seconds"]),
            timezone=payload.timezone or row["timezone"],
            enabled=row["enabled"] if payload.enabled is None else payload.enabled,
            actor=admin.username,
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error

    await audit(
        db,
        admin,
        request,
        action="schedule.update",
        entity_type="schedule",
        entity_id=str(schedule_id),
        payload=payload.model_dump(exclude_none=True),
    )
    return result


@router.post("/schedules/{schedule_id}/toggle")
async def toggle(
    schedule_id: UUID, admin: Admin, db: Engine, settings: AppSettings, request: Request
) -> dict[str, Any]:
    row = require(
        await fetch_one(
            db, text("SELECT * FROM schedules WHERE id = :id"), {"id": str(schedule_id)}
        ),
        "Расписание",
    )
    client = _prefect(request, settings, db)
    result = await client.set_schedule(
        row["key"],
        cron=row["cron"],
        interval_seconds=None if row["cron"] else row["interval_seconds"],
        timezone=row["timezone"],
        enabled=not row["enabled"],
        actor=admin.username,
    )
    await audit(
        db,
        admin,
        request,
        action="schedule.toggle",
        entity_type="schedule",
        entity_id=str(schedule_id),
        payload={"enabled": not row["enabled"]},
    )
    return result


@router.post("/schedules/{schedule_id}/run")
async def run_now(
    schedule_id: UUID,
    payload: RunRequest,
    admin: Admin,
    db: Engine,
    settings: AppSettings,
    request: Request,
) -> dict[str, Any]:
    """Запустить флоу немедленно — с параметрами расписания или своими."""

    row = require(
        await fetch_one(
            db, text("SELECT * FROM schedules WHERE id = :id"), {"id": str(schedule_id)}
        ),
        "Расписание",
    )
    client = _prefect(request, settings, db)
    parameters: dict[str, Any] | None = row.get("parameters") or None
    if payload.parameters is not None:
        try:
            given = coerce_parameters(row["key"], payload.parameters)
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
        # Параметры кнопки дополняют параметры расписания, а не заменяют их:
        # «собрать за 90 дней» не должно сбрасывать лимит на источник.
        parameters = {**(row.get("parameters") or {}), **given} if payload.merge else given
    try:
        result = await client.run_now(
            row["key"],
            parameters=parameters or None,
            actor=admin.username,
        )
    except PrefectUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    await audit(
        db,
        admin,
        request,
        action="schedule.run",
        entity_type="schedule",
        entity_id=str(schedule_id),
        payload={"key": row["key"], "parameters": parameters or {}},
    )
    return result


@router.get("/prefect/health")
async def prefect_health(
    admin: Admin, db: Engine, settings: AppSettings, request: Request
) -> dict[str, Any]:
    return await _prefect(request, settings, db).health()


@router.get("/prefect/deployments")
async def deployments(
    admin: Admin, db: Engine, settings: AppSettings, request: Request
) -> list[dict[str, Any]]:
    try:
        return await _prefect(request, settings, db).deployments()
    except PrefectUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


async def _deployment_names(db: Any) -> dict[str, str]:
    """id деплоймента -> человеческое имя флоу.

    Prefect отдаёт в прогоне только `deployment_id`; сопоставление живёт у нас,
    в `schedules`. Без него в списке прогонов вместо «Сбор материалов» стоял бы
    UUID, и понять, что именно упало, можно было бы только переходом в Prefect.
    """

    rows = await fetch_all(
        db,
        text(
            "SELECT prefect_deployment_id AS id, key, name FROM schedules "
            "WHERE prefect_deployment_id IS NOT NULL"
        ),
        {},
    )
    # Берём `name` из расписания, а не `FlowSpec.name`: последнее — техническое
    # имя в Prefect (`geonexa-delivery-group`), а в списке прогонов человеку
    # нужно «Рассылка в группы и каналы».
    return {str(row["id"]): row["name"] or row["key"] for row in rows}


@router.get("/prefect/flow-runs")
async def flow_runs(
    admin: Admin,
    db: Engine,
    settings: AppSettings,
    request: Request,
    kind: str | None = None,
    state: str | None = None,
    include_scheduled: bool = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """Прогоны. При недоступности Prefect отдаём локальное зеркало `flow_runs`."""

    try:
        runs = await _prefect(request, settings, db).runs(
            key=kind, limit=limit, state=state, include_scheduled=include_scheduled
        )
        names = await _deployment_names(db)
        return [
            {
                "id": run.id,
                "name": run.name,
                "flow": names.get(str(run.deployment), run.name),
                "state": run.state,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "duration_seconds": run.duration_seconds,
                "parameters": run.parameters,
                "deployment_id": run.deployment,
                "source": "prefect",
            }
            for run in runs
        ]
    except PrefectUnavailable:
        # Поля те же, что у ветки с Prefect: фронт не должен знать, откуда
        # пришли данные, иначе при падении оркестратора ломается ещё и таблица.
        rows = await fetch_all(
            db,
            text(
                "SELECT prefect_flow_run_id AS id,"
                " coalesce(kind, flow_name) AS name,"
                " coalesce(kind, flow_name) AS flow,"
                " state, started_at, finished_at,"
                " extract(epoch FROM (finished_at - started_at)) AS duration_seconds,"
                " 'mirror' AS source FROM flow_runs"
                " WHERE (CAST(:kind AS text) IS NULL OR kind = :kind)"
                "   AND (CAST(:state AS text) IS NULL OR state = :state)"
                "   AND (:include_scheduled OR started_at IS NOT NULL)"
                " ORDER BY started_at DESC LIMIT :limit"
            ),
            {
                "kind": kind,
                "state": state,
                "include_scheduled": include_scheduled,
                "limit": limit,
            },
        )
        for row in rows:
            spec = BY_KEY.get(str(row.get("flow")))
            if spec:
                row["flow"] = spec.description or spec.key
        return rows


@router.get("/prefect/flow-runs/{flow_run_id}/logs")
async def flow_run_logs(
    flow_run_id: str,
    admin: Admin,
    db: Engine,
    settings: AppSettings,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[dict[str, Any]]:
    try:
        return await _prefect(request, settings, db).logs(flow_run_id, limit=limit)
    except PrefectUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


@router.post("/prefect/flow-runs/{flow_run_id}/cancel")
async def cancel_run(
    flow_run_id: str,
    admin: Admin,
    db: Engine,
    settings: AppSettings,
    request: Request,
) -> dict[str, bool]:
    try:
        await _prefect(request, settings, db).cancel(flow_run_id)
    except PrefectUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    await audit(
        db, admin, request, action="prefect.cancel", entity_type="flow_run", entity_id=flow_run_id
    )
    return {"cancelled": True}


@router.post("/prefect/resync")
async def resync(
    admin: Admin, db: Engine, settings: AppSettings, request: Request
) -> dict[str, Any]:
    """Дослать в Prefect расписания, помеченные `sync_pending`."""

    result = await _prefect(request, settings, db).resync_pending()
    await audit(db, admin, request, action="prefect.resync", payload=result)
    return result
