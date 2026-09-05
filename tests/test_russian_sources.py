"""Российские источники: OAI-PMH (КиберЛенинка) и запросы по одному.

Сетевого здесь ничего нет: ответы источников подменяются, проверяется
разбор XML, склейка нескольких запросов без повторов, обход по ISSN и
честная ошибка на капче вместо XML.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from geonexa_proxima.collectors.crossref import CrossrefCollector
from geonexa_proxima.collectors.factory import create_collectors, load_sources
from geonexa_proxima.collectors.oai import OAICollector, OAIError
from geonexa_proxima.collectors.openalex import OpenAlexCollector
from geonexa_proxima.config import Settings
from geonexa_proxima.domain import SourceName
from geonexa_proxima.harvest import HarvestMatcher, load_harvest_profile

SINCE = datetime(2026, 9, 1, tzinfo=UTC)
UNTIL = datetime(2026, 9, 2, tzinfo=UTC)

_OAI_PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListRecords>
    <record>
      <header><identifier>oai:cyberleninka.ru:article/1</identifier>
        <datestamp>2026-09-01</datestamp></header>
      <metadata>
        <oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
                   xmlns:dc="http://purl.org/dc/elements/1.1/">
          <dc:title>Оценка несущей способности свай по данным статического зондирования</dc:title>
          <dc:title>Pile bearing capacity from CPT data</dc:title>
          <dc:creator>Иванов И. И.</dc:creator>
          <dc:subject>механика грунтов</dc:subject>
          <dc:description>Рассмотрены методы расчёта.</dc:description>
          <dc:date>2026-08-30</dc:date>
          <dc:identifier>https://cyberleninka.ru/article/n/1</dc:identifier>
          <dc:identifier>10.12345/abc.1</dc:identifier>
          <dc:language>ru</dc:language>
          <dc:source>Основания, фундаменты и механика грунтов</dc:source>
        </oai_dc:dc>
      </metadata>
    </record>
    <record>
      <header status="deleted"><identifier>oai:cyberleninka.ru:article/2</identifier>
        <datestamp>2026-09-01</datestamp></header>
    </record>
    {token}
  </ListRecords>
</OAI-PMH>
"""


class _Response:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def json(self) -> Any:
        return {}


def _oai_pages(pages: list[bytes]):
    calls: list[dict[str, Any]] = []

    async def fake_request(self: Any, method: str, url: str, **kwargs: Any) -> _Response:
        calls.append(dict(kwargs.get("params") or {}))
        return _Response(pages[min(len(calls) - 1, len(pages) - 1)])

    return fake_request, calls


async def test_oai_parses_dublin_core_and_follows_resumption_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _OAI_PAGE.format(token="<resumptionToken>page2</resumptionToken>").encode()
    second = _OAI_PAGE.format(token="").encode()
    fake, calls = _oai_pages([first, second])
    monkeypatch.setattr(OAICollector, "_request", fake)
    monkeypatch.setattr("geonexa_proxima.collectors.oai.PAGE_PAUSE_SECONDS", 0)

    collector = OAICollector("https://cyberleninka.ru/oai", source=SourceName.CYBERLENINKA)
    items = await collector.collect(SINCE, 100, UNTIL)

    # Две страницы с одной и той же статьёй — в результате она одна.
    assert len(items) == 1
    item = items[0]
    assert item.source is SourceName.CYBERLENINKA
    assert item.external_id == "oai:cyberleninka.ru:article/1"
    assert "свай" in item.title and "CPT" in item.title
    assert item.doi == "10.12345/abc.1"
    assert item.language == "ru"
    assert item.venue == "Основания, фундаменты и механика грунтов"
    assert item.publication_date is not None
    # Первый запрос — окно суток, второй — только токен.
    assert calls[0]["from"] == "2026-09-01" and calls[0]["until"] == "2026-09-01"
    assert calls[1] == {"verb": "ListRecords", "resumptionToken": "page2"}


async def test_oai_reports_captcha_instead_of_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    page = "<html><body>Вы точно человек? captcha</body></html>".encode()
    fake, _ = _oai_pages([page])
    monkeypatch.setattr(OAICollector, "_request", fake)

    with pytest.raises(OAIError, match="капч"):
        await OAICollector("https://cyberleninka.ru/oai").collect(SINCE, 10, UNTIL)


async def test_crossref_runs_each_query_and_each_issn_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_request(self: Any, method: str, url: str, **kwargs: Any) -> Any:
        params = dict(kwargs.get("params") or {})
        calls.append(params)
        doi = f"10.1/{len(calls)}" if len(calls) < 3 else "10.1/1"

        class _R:
            def json(self) -> Any:
                return {"message": {"items": [{"DOI": doi, "title": ["T"]}]}}

        return _R()

    monkeypatch.setattr(CrossrefCollector, "_request", fake_request)
    monkeypatch.setattr("geonexa_proxima.collectors.base.QUERY_PAUSE_SECONDS", 0)
    collector = CrossrefCollector(queries=["механика грунтов", "geotechnical"], issns=["0038-0741"])

    items = await collector.collect(SINCE, 100, UNTIL)

    assert [call.get("query.bibliographic") for call in calls] == [
        "механика грунтов",
        "geotechnical",
        None,
    ]
    assert "issn:0038-0741" in calls[2]["filter"]
    assert all("from-pub-date:2026-09-01" in call["filter"] for call in calls)
    # Третий ответ повторил первый DOI — материал один.
    assert sorted(item.external_id for item in items) == ["10.1/1", "10.1/2"]


async def test_openalex_without_query_list_keeps_the_old_single_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_request(self: Any, method: str, url: str, **kwargs: Any) -> Any:
        calls.append(dict(kwargs.get("params") or {}))

        class _R:
            def json(self) -> Any:
                return {"results": []}

        return _R()

    monkeypatch.setattr(OpenAlexCollector, "_request", fake_request)
    await OpenAlexCollector(query="soil liquefaction").collect(SINCE, 50, UNTIL)
    assert len(calls) == 1 and calls[0]["search"] == "soil liquefaction"


def test_factory_builds_russian_sources_from_config() -> None:
    settings = Settings(_env_file=None, admin_password="x")
    collectors = create_collectors(settings)
    names = [type(collector).__name__ for collector in collectors]
    assert names.count("OAICollector") >= 1
    oai = next(c for c in collectors if isinstance(c, OAICollector))
    assert oai.source is SourceName.CYBERLENINKA
    crossref = next(c for c in collectors if isinstance(c, CrossrefCollector))
    assert any("грунт" in query for query in crossref.queries)
    assert "0038-0741" in crossref.issns
    openalex = next(c for c in collectors if isinstance(c, OpenAlexCollector))
    assert any("геолог" in query for query in openalex.queries)


def test_every_source_section_has_russian_queries_where_search_exists() -> None:
    """Русские запросы обязаны быть у каждого источника с текстовым поиском."""

    sources = load_sources(Path("config/harvest.yaml"))
    for key in ("openalex", "crossref", "github"):
        queries = [entry["query"] for entry in sources[key]["queries"] if entry.get("enabled")]
        assert any(any("а" <= ch <= "я" for ch in query.lower()) for query in queries), key


def test_gate_accepts_russian_engineering_titles_from_cyberleninka() -> None:
    matcher = HarvestMatcher(load_harvest_profile(Path("config/harvest.yaml")))
    titles = [
        "Оценка несущей способности свай по данным статического зондирования",
        "Особенности проектирования котлованов в условиях плотной городской застройки",
        "Прогноз осадок земляного полотна на слабых грунтах",
        "Применение нейронных сетей для интерпретации данных инженерно-геологических изысканий",
        "Термостабилизация многолетнемёрзлых грунтов оснований трубопроводов",
    ]
    for title in titles:
        result = matcher.match(title, None)
        assert result.decision.value != "rejected", (title, matcher.explain(result))


async def test_oai_prefilter_keeps_only_what_the_gate_would_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без поиска по словам лимит источника съедали бы чужие дисциплины."""

    page = _OAI_PAGE.format(token="").encode()
    fake, _ = _oai_pages([page])
    monkeypatch.setattr(OAICollector, "_request", fake)

    rejecting = OAICollector("https://cyberleninka.ru/oai", keep=lambda item: False)
    assert await rejecting.collect(SINCE, 10, UNTIL) == []

    accepting = OAICollector("https://cyberleninka.ru/oai", keep=lambda item: "свай" in item.title)
    assert len(await accepting.collect(SINCE, 10, UNTIL)) == 1
