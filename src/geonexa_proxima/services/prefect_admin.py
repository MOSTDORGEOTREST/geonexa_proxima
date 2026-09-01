"""Управление Prefect из админки: запуск, расписания, логи.

Всё общение идёт через REST API Prefect, а не через запись в его базу: схема
базы Prefect — его внутреннее дело и меняется между версиями.

Разделение ответственности: таблица ``schedules`` хранит намерение
администратора, Prefect — факт исполнения. Правка расписания идёт в базу и
сразу пушится в Prefect; если Prefect недоступен, строка помечается
``sync_pending`` и досинхронизируется обслуживающим флоу, а не теряется.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from geonexa_proxima.config import Settings
from geonexa_proxima.domain import NotFoundError
from geonexa_proxima.flow_catalog import BY_KEY


class PrefectUnavailable(RuntimeError):
    """Prefect не отвечает. Это не повод терять правку администратора."""


class FlowNotRegistered(NotFoundError):
    """Deployment для этого флоу ещё не зарегистрирован в Prefect.

    Отдельный класс, чтобы админка показала не «Internal Server Error», а
    инструкцию: пока воркер не зарегистрировал деплойменты, запускать нечего.
    """


@dataclass(frozen=True, slots=True)
class FlowRunSummary:
    id: str
    name: str
    deployment: str | None
    state: str
    started_at: str | None
    finished_at: str | None
    duration_seconds: float | None
    parameters: dict[str, Any]

    @property
    def is_running(self) -> bool:
        return self.state in {"RUNNING", "PENDING", "SCHEDULED"}


class PrefectAdmin:
    """Тонкий клиент к Prefect REST API для нужд админки."""

    def __init__(
        self, settings: Settings, engine: AsyncEngine, client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.base_url = settings.prefect_api_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if settings.prefect_api_key:
            headers["Authorization"] = f"Bearer {settings.prefect_api_key.get_secret_value()}"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=httpx.Timeout(20.0)
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise PrefectUnavailable(
                f"Prefect ответил {error.response.status_code} на {method} {path}"
            ) from error
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise PrefectUnavailable(f"Prefect недоступен: {error}") from error
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # -- состояние ---------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """Живой ли Prefect и сколько прогонов сейчас идёт."""

        try:
            version = await self._request("GET", "/admin/version")
            running = await self._request(
                "POST",
                "/flow_runs/count",
                json={"flow_runs": {"state": {"type": {"any_": ["RUNNING", "PENDING"]}}}},
            )
            return {"available": True, "version": version, "running": running}
        except PrefectUnavailable as error:
            return {"available": False, "error": str(error)}

    async def deployments(self) -> list[dict[str, Any]]:
        rows = await self._request("POST", "/deployments/filter", json={"limit": 200})
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "flow_id": row.get("flow_id"),
                "paused": row.get("paused", False),
                "schedules": row.get("schedules", []),
                "tags": row.get("tags", []),
            }
            for row in rows or []
        ]

    async def deployment_id(self, key: str) -> str:
        """Найти deployment по ключу расписания."""

        async with self.engine.connect() as connection:
            stored = await connection.scalar(
                text("SELECT prefect_deployment_id FROM schedules WHERE key = :key"),
                {"key": key},
            )
        if stored:
            return str(stored)
        spec = BY_KEY.get(key)
        if spec is None:
            raise FlowNotRegistered(f"Неизвестный флоу: {key}")
        rows = await self._request(
            "POST",
            "/deployments/filter",
            json={"deployments": {"name": {"any_": [key]}}, "limit": 1},
        )
        if not rows:
            raise FlowNotRegistered(
                f"Deployment «{key}» ещё не зарегистрирован в Prefect. "
                f"Запусти prefect-worker или выполни `geonexa prefect deploy` — "
                f"до этого запускать нечего."
            )
        deployment_id = str(rows[0]["id"])
        # Запоминаем найденное: иначе каждый запуск и каждая правка расписания
        # снова идут в Prefect за одним и тем же id, а админка не может
        # показать ссылку на deployment, пока Prefect не ответит.
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE schedules SET prefect_deployment_id = :deployment_id, "
                    "updated_at = now() WHERE key = :key"
                ),
                {"key": key, "deployment_id": deployment_id},
            )
        return deployment_id

    # -- запуск ------------------------------------------------------------

    async def run_now(
        self, key: str, *, parameters: dict[str, Any] | None = None, actor: str = "admin"
    ) -> dict[str, Any]:
        """Запустить флоу вручную. Именно этим отлаживают сбор и рассылку."""

        deployment_id = await self.deployment_id(key)
        payload: dict[str, Any] = {
            "state": {"type": "SCHEDULED"},
            "tags": ["manual", f"actor:{actor}"],
        }
        if parameters:
            payload["parameters"] = parameters
        run = await self._request(
            "POST", f"/deployments/{deployment_id}/create_flow_run", json=payload
        )
        await self._mirror_run(run, kind=key)
        return {"flow_run_id": run["id"], "name": run.get("name"), "state": "SCHEDULED"}

    async def cancel(self, flow_run_id: str) -> None:
        await self._request(
            "POST",
            f"/flow_runs/{flow_run_id}/set_state",
            json={"state": {"type": "CANCELLING"}, "force": True},
        )

    # -- наблюдение --------------------------------------------------------

    #: Всё, что не «ждёт своей очереди». Prefect сортирует по времени старта, а
    #: у запланированного прогона его нет — такие всплывают наверх и вытесняют
    #: из выборки настоящие прогоны, ради которых список и открывают.
    STARTED_STATES = (
        "PENDING",
        "RUNNING",
        "COMPLETED",
        "FAILED",
        "CRASHED",
        "CANCELLED",
        "CANCELLING",
        "PAUSED",
    )

    async def runs(
        self,
        *,
        key: str | None = None,
        limit: int = 25,
        state: str | None = None,
        include_scheduled: bool = True,
    ) -> list[FlowRunSummary]:
        flow_runs: dict[str, Any] = {}
        if state:
            flow_runs["state"] = {"type": {"any_": [state.upper()]}}
        elif not include_scheduled:
            flow_runs["state"] = {"type": {"any_": list(self.STARTED_STATES)}}
        payload: dict[str, Any] = {
            "limit": limit,
            "sort": "START_TIME_DESC",
            "flow_runs": flow_runs,
        }
        if key:
            payload["deployments"] = {"name": {"any_": [key]}}
        rows = await self._request("POST", "/flow_runs/filter", json=payload)
        return [
            FlowRunSummary(
                id=row["id"],
                name=row.get("name", ""),
                deployment=row.get("deployment_id"),
                state=(row.get("state") or {}).get("type", "UNKNOWN"),
                started_at=row.get("start_time"),
                finished_at=row.get("end_time"),
                duration_seconds=row.get("total_run_time"),
                parameters=row.get("parameters") or {},
            )
            for row in rows or []
        ]

    #: Prefect не отдаёт больше 200 строк за запрос и отвечает 422 на попытку
    #: попросить больше. Обрезать лог до двухсот строк нельзя: интересен как
    #: раз конец, где всё сломалось, — поэтому листаем страницами.
    LOG_PAGE = 200

    async def logs(self, flow_run_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """Логи прогона — то, ради чего в админку и заходят при отладке."""

        collected: list[dict[str, Any]] = []
        offset = 0
        while len(collected) < limit:
            page = min(self.LOG_PAGE, limit - len(collected))
            rows = await self._request(
                "POST",
                "/logs/filter",
                json={
                    "logs": {"flow_run_id": {"any_": [flow_run_id]}},
                    "sort": "TIMESTAMP_ASC",
                    "limit": page,
                    "offset": offset,
                },
            )
            rows = rows or []
            collected.extend(
                {
                    "timestamp": row.get("timestamp"),
                    "level": row.get("level"),
                    "message": row.get("message"),
                    "logger": row.get("name"),
                }
                for row in rows
            )
            if len(rows) < page:
                break
            offset += len(rows)
        return collected

    # -- расписания --------------------------------------------------------

    async def set_schedule(
        self,
        key: str,
        *,
        cron: str | None = None,
        interval_seconds: int | None = None,
        timezone: str | None = None,
        enabled: bool = True,
        actor: str = "admin",
    ) -> dict[str, Any]:
        """Записать расписание в БД и протолкнуть его в Prefect.

        Порядок важен: сначала база. Если Prefect недоступен, правка уже
        сохранена и помечена как несинхронизированная — админ увидит это в
        интерфейсе, а обслуживающий флоу дошлёт её позже.
        """

        if bool(cron) == bool(interval_seconds):
            raise ValueError("Задай ровно одно: cron или interval_seconds")
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE schedules SET cron = :cron, interval_seconds = :interval, "
                    "timezone = coalesce(:tz, timezone), enabled = :enabled, "
                    "sync_pending = true, updated_at = now() WHERE key = :key"
                ),
                {
                    "key": key,
                    "cron": cron,
                    "interval": interval_seconds,
                    "tz": timezone,
                    "enabled": enabled,
                },
            )
        try:
            await self._push_schedule(key, cron, interval_seconds, timezone, enabled)
        except PrefectUnavailable as error:
            return {"saved": True, "synced": False, "reason": str(error)}
        async with self.engine.begin() as connection:
            await connection.execute(
                text("UPDATE schedules SET sync_pending = false WHERE key = :key"),
                {"key": key},
            )
        return {"saved": True, "synced": True}

    async def _push_schedule(
        self,
        key: str,
        cron: str | None,
        interval_seconds: int | None,
        timezone: str | None,
        enabled: bool,
    ) -> None:
        deployment_id = await self.deployment_id(key)
        schedule: dict[str, Any] = (
            {"cron": cron, "timezone": timezone or self.settings.timezone}
            if cron
            else {"interval": float(interval_seconds or 0)}
        )
        payload: dict[str, Any] = {"schedules": [{"schedule": schedule, "active": enabled}]}
        parameters = await self._parameters(key)
        if parameters:
            # Вместе с расписанием едут и параметры строки. Иначе правка вроде
            # «слать в 16:00» доезжает до Prefect только при следующем полном
            # деплое: расписание меняется, а параметр запуска остаётся прежним,
            # и понять это по интерфейсу невозможно.
            payload["parameters"] = parameters
        await self._request("PATCH", f"/deployments/{deployment_id}", json=payload)

    async def _parameters(self, key: str) -> dict[str, Any]:
        """Параметры запуска: из строки расписания, иначе из каталога флоу.

        Порядок тот же, что при полном деплое (`deploy_all`). Разойтись им
        нельзя: тогда «дослать» и перезапуск воркера оставляли бы deployment в
        разных состояниях.
        """

        from geonexa_proxima.flow_catalog import BY_KEY

        async with self.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text("SELECT parameters FROM schedules WHERE key = :key"),
                        {"key": key},
                    )
                )
                .mappings()
                .first()
            )
        stored = (row or {}).get("parameters") or None
        spec = BY_KEY.get(key)
        return dict(stored or (spec.parameters if spec else None) or {})

    async def resync_pending(self) -> dict[str, Any]:
        """Дослать расписания, которые не удалось протолкнуть раньше."""

        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT key, cron, interval_seconds, timezone, enabled "
                            "FROM schedules WHERE sync_pending"
                        )
                    )
                )
                .mappings()
                .all()
            )
        synced, failed = [], {}
        for row in rows:
            try:
                await self._push_schedule(
                    row["key"],
                    row["cron"],
                    row["interval_seconds"],
                    row["timezone"],
                    row["enabled"],
                )
                async with self.engine.begin() as connection:
                    await connection.execute(
                        text("UPDATE schedules SET sync_pending = false WHERE key = :key"),
                        {"key": row["key"]},
                    )
                synced.append(row["key"])
            except (PrefectUnavailable, NotFoundError) as error:
                failed[row["key"]] = str(error)
        return {"synced": synced, "failed": failed}

    # -- зеркало для админки ----------------------------------------------

    async def _mirror_run(self, run: dict[str, Any], *, kind: str) -> None:
        """Продублировать прогон локально, чтобы дашборд не ходил в Prefect на каждый чих."""

        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO flow_runs (
                        id, prefect_flow_run_id, flow_name, kind, state, started_at)
                    VALUES (gen_random_uuid(), :prefect_id, :name, :kind, :state, now())
                    ON CONFLICT (prefect_flow_run_id) DO UPDATE
                       SET state = EXCLUDED.state, updated_at = now()
                    """
                ),
                {
                    "prefect_id": run["id"],
                    "name": run.get("name", kind),
                    "kind": kind,
                    "state": "SCHEDULED",
                },
            )


def describe_cron(
    expression: str, *, count: int = 5, timezone: str = "Europe/Moscow"
) -> dict[str, Any]:
    """Человеческое описание расписания и ближайшие запуски.

    Админка показывает это рядом с полем ввода: cron легко написать так, что он
    сработает не тогда, когда ожидалось, и увидеть это до сохранения дешевле.
    """

    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        from croniter import croniter
    except ImportError:
        return {"valid": None, "reason": "croniter не установлен", "next": []}

    if not croniter.is_valid(expression):
        return {"valid": False, "reason": "выражение не разбирается", "next": []}
    now = datetime.now(ZoneInfo(timezone))
    iterator = croniter(expression, now)
    upcoming = [iterator.get_next(datetime).isoformat() for _ in range(count)]
    return {"valid": True, "next": upcoming, "timezone": timezone}
