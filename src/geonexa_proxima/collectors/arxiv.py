"""Asynchronous arXiv Atom collector."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree

from geonexa_proxima.collectors.base import (
    AsyncHTTPProvider,
    TaxonomyInput,
    combined_query,
    in_window,
    parse_date,
)
from geonexa_proxima.domain import Author, CollectedItem, SourceName

_ATOM = "http://www.w3.org/2005/Atom"
_ARXIV = "http://arxiv.org/schemas/atom"


class ArxivCollector(AsyncHTTPProvider):
    def __init__(
        self,
        query: str | None = None,
        taxonomy: TaxonomyInput = None,
        **kwargs: object,
    ) -> None:
        kwargs.setdefault("user_agent", "GeoNexa-Proxima/0.1 (arXiv research collector)")
        super().__init__(**kwargs)
        self.query = combined_query(query, taxonomy) or "geotechnical OR geospatial"

    #: Потолок выдачи arXiv за один запрос.
    page_limit = 2000

    async def collect(
        self, since: datetime, limit: int, until: datetime | None = None
    ) -> list[CollectedItem]:
        response = await self._request(
            "GET",
            "https://export.arxiv.org/api/query",
            params={
                "search_query": self._search_query(since, until),
                "start": 0,
                "max_results": limit,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
            headers={"Accept": "application/atom+xml"},
        )
        root = ElementTree.fromstring(response.content)
        items: list[CollectedItem] = []
        for entry in root.findall(f"{{{_ATOM}}}entry"):
            published = parse_date(entry.findtext(f"{{{_ATOM}}}published"))
            if not in_window(published, since, until):
                continue
            entry_url = (entry.findtext(f"{{{_ATOM}}}id") or "").strip()
            arxiv_id = re.sub(r"v\d+$", "", entry_url.rstrip("/").rsplit("/", 1)[-1])
            links = {
                link.attrib.get("rel", ""): link.attrib.get("href", "")
                for link in entry.findall(f"{{{_ATOM}}}link")
            }
            categories = [
                node.attrib["term"]
                for node in entry.findall(f"{{{_ATOM}}}category")
                if node.attrib.get("term")
            ]
            doi = entry.findtext(f"{{{_ARXIV}}}doi")
            items.append(
                CollectedItem(
                    source=SourceName.ARXIV,
                    external_id=arxiv_id,
                    title=_clean(entry.findtext(f"{{{_ATOM}}}title")),
                    abstract=_clean(entry.findtext(f"{{{_ATOM}}}summary")) or None,
                    authors=[
                        Author(name=_clean(author.findtext(f"{{{_ATOM}}}name")))
                        for author in entry.findall(f"{{{_ATOM}}}author")
                        if _clean(author.findtext(f"{{{_ATOM}}}name"))
                    ],
                    keywords=categories,
                    doi=doi.strip() if doi else None,
                    arxiv_id=arxiv_id,
                    publication_date=published,
                    venue=(entry.findtext(f"{{{_ARXIV}}}journal_ref") or "").strip() or None,
                    url=links.get("alternate") or entry_url or None,
                    raw={"atom": ElementTree.tostring(entry, encoding="unicode")},
                )
            )
        return items[:limit]

    def _search_query(self, since: datetime, until: datetime | None = None) -> str:
        if '"' in self.query:
            terms = re.sub(r'"([^"]+)"', lambda match: f'all:"{match.group(1)}"', self.query)
        else:
            parts = [part.strip() for part in self.query.split(" OR ") if part.strip()]
            terms = " OR ".join(f'all:"{part}"' for part in parts)
        if until is None:
            return terms
        # Окно задаётся самому arXiv, а не отбирается на нашей стороне. Выдача
        # идёт от свежего к старому пачкой в max_results: за вчерашние сутки
        # фильтр по ответу сработал бы, а за позавчерашние вернул бы сегодняшние
        # работы и отбросил их все до единой — сутки молча остались бы пустыми.
        # Границы у arXiv включающие с обеих сторон, а наше окно — полуинтервал:
        # верхнюю отодвигаем на минуту назад, иначе полночь попала бы и в эти
        # сутки, и в следующие. Пробелы в значении кодирует httpx, «+» внутри
        # строки уехал бы на сервер как %2B и сломал разбор запроса.
        window = f"submittedDate:[{_stamp(since)} TO {_stamp(until - timedelta(minutes=1))}]"
        return f"({terms}) AND {window}"


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _stamp(value: datetime) -> str:
    """Метка времени в формате arXiv: YYYYMMDDHHMM, без разделителей и в UTC."""

    return value.astimezone(UTC).strftime("%Y%m%d%H%M")
