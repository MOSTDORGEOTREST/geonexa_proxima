"""Что уходит источнику за одни сутки.

Проверка сетевая по смыслу, но не по исполнению: запрос перехватывается, и
сверяется ровно то, ради чего вводились сутки, — что все четыре источника
спрашивают одну и ту же дату, ту же, что оставляет наш фильтр по ответу.
Разъехавшись здесь на день, платформа теряет материалы молча: воронка выглядит
здоровой, просто корпус беднее.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from geonexa_proxima.collectors.arxiv import ArxivCollector
from geonexa_proxima.collectors.base import in_window
from geonexa_proxima.collectors.crossref import CrossrefCollector
from geonexa_proxima.collectors.github import GitHubCollector
from geonexa_proxima.collectors.openalex import OpenAlexCollector
from geonexa_proxima.services.harvest_window import day_window

DAY = date(2026, 8, 30)
STAMP = "2026-08-30"


class _Response:
    """Пустой, но разбираемый ответ каждого из четырёх форматов."""

    content = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    def json(self) -> dict[str, Any]:
        return {"results": [], "message": {"items": []}, "items": []}


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def fake_request(self: Any, method: str, url: str, **kwargs: Any) -> _Response:
        seen[type(self).__name__] = kwargs.get("params") or {}
        return _Response()

    for collector in (ArxivCollector, OpenAlexCollector, CrossrefCollector, GitHubCollector):
        monkeypatch.setattr(collector, "_request", fake_request)
    return seen


async def _ask(collector: Any) -> None:
    window = day_window(DAY)
    await collector.collect(window.since, 300, window.until)


async def test_arxiv_asks_the_source_itself_for_the_day(captured: dict[str, Any]) -> None:
    """Фильтровать выдачу arXiv по ответу нельзя: она идёт от свежего к старому.

    За позавчерашние сутки такой фильтр вернул бы сегодняшние работы и отбросил
    их все до единой — сутки молча остались бы пустыми.
    """

    await _ask(ArxivCollector(query="soil liquefaction"))

    query = captured["ArxivCollector"]["search_query"]
    assert "submittedDate:[202608300000 TO 202608302359]" in query


async def test_openalex_asks_for_exactly_one_date(captured: dict[str, Any]) -> None:
    await _ask(OpenAlexCollector(query="soil liquefaction"))

    assert captured["OpenAlexCollector"]["filter"] == (
        f"from_publication_date:{STAMP},to_publication_date:{STAMP}"
    )


async def test_crossref_asks_for_exactly_one_date(captured: dict[str, Any]) -> None:
    await _ask(CrossrefCollector(query="soil liquefaction"))

    assert captured["CrossrefCollector"]["filter"] == (
        f"from-pub-date:{STAMP},until-pub-date:{STAMP}"
    )


async def test_github_asks_for_exactly_one_date(captured: dict[str, Any]) -> None:
    await _ask(GitHubCollector(query="soil liquefaction"))

    assert f"pushed:{STAMP}..{STAMP}" in captured["GitHubCollector"]["q"]


async def test_open_window_keeps_the_old_behaviour(captured: dict[str, Any]) -> None:
    """Без верхней границы источники работают как раньше — «от даты и до свежего»."""

    window = day_window(DAY)
    await GitHubCollector(query="soil liquefaction").collect(window.since, 300)
    await OpenAlexCollector(query="soil liquefaction").collect(window.since, 300)

    assert f"pushed:>={STAMP}" in captured["GitHubCollector"]["q"]
    assert captured["OpenAlexCollector"]["filter"] == f"from_publication_date:{STAMP}"


def test_response_filter_agrees_with_the_request() -> None:
    """Фильтр по ответу обязан оставлять ровно ту дату, которую спросили."""

    window = day_window(DAY)

    assert in_window(DAY, window.since, window.until)
    assert not in_window(date(2026, 8, 29), window.since, window.until)
    assert not in_window(date(2026, 8, 31), window.since, window.until)
