"""Один сбор одновременно — и что происходит, когда предыдущий не закрылся.

Инвариант держит частичный уникальный индекс `uq_harvest_runs_running`: два
параллельных сбора ходили бы в одни источники, дважды тратили токены и гонялись
за одни и те же строки. Обратная сторона инварианта — запись, которую упавший
процесс не закрыл, блокирует сбор навсегда, а сообщает об этом двумястами
строк `IntegrityError`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from geonexa_proxima.workflows.harvest import HarvestAlreadyRunning, open_run, reclaim_stale_runs


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def mappings(self) -> _Result:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _Connection:
    def __init__(self, engine: _Engine, *, failing: bool) -> None:
        self.engine = engine
        self.failing = failing

    async def execute(self, statement: Any, params: Any = None) -> _Result:
        sql = str(statement)
        self.engine.statements.append((sql, params))
        if self.failing and "INSERT INTO harvest_runs" in sql:
            raise IntegrityError("INSERT", params, Exception("uq_harvest_runs_running"))
        if "UPDATE harvest_runs" in sql:
            return _Result(self.engine.reclaimed)
        return _Result(self.engine.running)


class _Engine:
    """Пишущие вызовы падают, читающие отвечают тем, что положили в `running`."""

    def __init__(self, *, failing: bool = False, running: list | None = None) -> None:
        self.failing = failing
        self.running = running or []
        self.reclaimed: list[dict[str, Any]] = []
        self.statements: list[tuple[str, Any]] = []

    @asynccontextmanager
    async def begin(self):
        yield _Connection(self, failing=self.failing)

    @asynccontextmanager
    async def connect(self):
        yield _Connection(self, failing=False)


class _Container:
    def __init__(self, engine: _Engine) -> None:
        self._engine = engine

    def require_engine(self) -> _Engine:
        return self._engine


@pytest.mark.asyncio
async def test_second_harvest_reports_a_readable_reason() -> None:
    engine = _Engine(
        failing=True,
        running=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "started_at": datetime(2026, 8, 30, 20, 21, tzinfo=UTC),
                "triggered_by": "flow:manual",
            }
        ],
    )

    with pytest.raises(HarvestAlreadyRunning) as raised:
        await open_run.fn(_Container(engine), "manual", datetime.now(UTC))

    message = str(raised.value)
    assert "Сбор уже идёт" in message
    # В сообщении должно быть всё, чем можно распорядиться: какой прогон,
    # когда начат, кем и что с этим делать.
    assert "11111111" in message
    assert "30.08.2026 20:21" in message
    assert "flow:manual" in message
    assert "Прогоны" in message


@pytest.mark.asyncio
async def test_integrity_error_without_a_running_row_is_not_swallowed() -> None:
    """Если блокирующей записи нет, значит нарушен другой инвариант.

    Подменять чужую ошибку своей формулировкой нельзя: она уведёт от причины.
    """

    engine = _Engine(failing=True, running=[])
    with pytest.raises(IntegrityError):
        await open_run.fn(_Container(engine), "manual", datetime.now(UTC))


@pytest.mark.asyncio
async def test_stale_runs_are_reclaimed_with_the_configured_threshold() -> None:
    engine = _Engine()
    engine.reclaimed = [{"id": "a"}, {"id": "b"}]

    count = await reclaim_stale_runs.fn(_Container(engine), 90)

    assert count == 2
    sql, params = engine.statements[0]
    assert "UPDATE harvest_runs" in sql
    assert "status = 'running'" in sql
    assert params == {"minutes": 90}
    # Причина должна остаться в записи: иначе в истории прогон выглядит просто
    # неудачным, и непонятно, что его никто не выполнял.
    assert "оборван" in sql


@pytest.mark.asyncio
async def test_a_fresh_run_is_not_reclaimed_by_the_query() -> None:
    """Порог применяется в SQL, а не в Python — проверяем, что он там есть."""

    engine = _Engine()
    await reclaim_stale_runs.fn(_Container(engine), 90)
    sql, _ = engine.statements[0]
    assert "make_interval(mins => :minutes)" in sql


@pytest.mark.asyncio
async def test_reclaim_respects_the_heartbeat_of_a_long_run() -> None:
    """Ручной сбор за квартал идёт часами: отметка после каждых суток
    держит его живым, и ночной плановый запуск не объявляет его брошенным."""

    engine = _Engine()
    await reclaim_stale_runs.fn(_Container(engine), 90)
    sql, _ = engine.statements[0]
    assert "heartbeat_at" in sql
    assert "coalesce" in sql.lower()
