"""Asynchronous arXiv Atom collector."""

from __future__ import annotations

import re
from datetime import datetime
from xml.etree import ElementTree

from geonexa_proxima.collectors.base import (
    AsyncHTTPProvider,
    TaxonomyInput,
    combined_query,
    is_since,
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

    async def collect(self, since: datetime, limit: int) -> list[CollectedItem]:
        response = await self._request(
            "GET",
            "https://export.arxiv.org/api/query",
            params={
                "search_query": self._search_query(),
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
            if not is_since(published, since):
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

    def _search_query(self) -> str:
        if '"' in self.query:
            return re.sub(r'"([^"]+)"', lambda match: f'all:"{match.group(1)}"', self.query)
        parts = [part.strip() for part in self.query.split(" OR ") if part.strip()]
        return " OR ".join(f'all:"{part}"' for part in parts)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())
