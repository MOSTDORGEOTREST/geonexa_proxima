"""Клиент Prefect: два ограничения его API, о которые уже спотыкались.

Оба дефекта проявляются только на живом сервере и только в неудобный момент:
логи не открываются ровно тогда, когда прогон упал, а список прогонов пуст
ровно тогда, когда в очереди накопились запланированные запуски.
"""

from __future__ import annotations

from typing import Any

import pytest

from geonexa_proxima.config import Settings
from geonexa_proxima.services.prefect_admin import PrefectAdmin


class _Recorder(PrefectAdmin):
    """Подменяет транспорт: нас интересует, что именно уходит в Prefect."""

    def __init__(self, pages: list[list[dict[str, Any]]] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._pages = pages or [[]]
        # Настоящий __init__ поднял бы httpx-клиент, а он здесь не нужен.
        self.settings = Settings(_env_file=None)
        self.engine = None  # type: ignore[assignment]

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path, kwargs.get("json") or {}))
        return self._pages[min(len(self.calls) - 1, len(self._pages) - 1)]


def _log_rows(count: int) -> list[dict[str, Any]]:
    return [
        {"timestamp": f"2026-01-01T00:00:{index:02d}Z", "level": 20, "message": f"строка {index}"}
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_logs_are_paged_because_prefect_caps_the_page() -> None:
    """Prefect отвечает 422 на limit больше 200.

    Просить 1000 строк одним запросом нельзя, а обрезать лог до двухсот —
    значит потерять конец, где и написано, что сломалось.
    """

    client = _Recorder(pages=[_log_rows(200), _log_rows(200), _log_rows(37)])
    lines = await client.logs("run-1", limit=1000)

    assert len(lines) == 437
    limits = [payload["limit"] for _, path, payload in client.calls if path == "/logs/filter"]
    assert max(limits) <= PrefectAdmin.LOG_PAGE
    # Страницы должны сдвигаться, иначе одна и та же вернётся трижды.
    offsets = [payload["offset"] for _, _, payload in client.calls]
    assert offsets == [0, 200, 400]


@pytest.mark.asyncio
async def test_short_answer_stops_paging() -> None:
    """Неполная страница означает конец лога — дальше ходить незачем."""

    client = _Recorder(pages=[_log_rows(12)])
    lines = await client.logs("run-1", limit=1000)

    assert len(lines) == 12
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_scheduled_runs_can_be_excluded() -> None:
    """Запланированный прогон не имеет времени старта.

    Prefect сортирует по времени старта, поэтому такие всплывают наверх и
    вытесняют из выборки настоящие прогоны — на странице расписаний в списке
    «последних» оказывалась одна очередь.
    """

    client = _Recorder()
    await client.runs(limit=20, include_scheduled=False)

    _, path, payload = client.calls[0]
    assert path == "/flow_runs/filter"
    states = payload["flow_runs"]["state"]["type"]["any_"]
    assert "SCHEDULED" not in states
    assert {"RUNNING", "COMPLETED", "FAILED"} <= set(states)


@pytest.mark.asyncio
async def test_by_default_the_queue_is_visible() -> None:
    client = _Recorder()
    await client.runs(limit=20)

    _, _, payload = client.calls[0]
    assert payload["flow_runs"] == {}
