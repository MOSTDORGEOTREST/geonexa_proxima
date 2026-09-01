"""Asynchronous Crossref works collector."""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta

from geonexa_proxima.collectors.base import (
    AsyncHTTPProvider,
    TaxonomyInput,
    as_dict,
    as_list,
    combined_query,
)
from geonexa_proxima.domain import Author, CollectedItem, SourceName

_TAG_RE = re.compile(r"<[^>]+>")


class CrossrefCollector(AsyncHTTPProvider):
    def __init__(
        self,
        query: str | None = None,
        taxonomy: TaxonomyInput = None,
        *,
        email: str | None = None,
        **kwargs: object,
    ) -> None:
        contact = email or "unknown"
        kwargs.setdefault("user_agent", f"GeoNexa-Proxima/0.1 (mailto:{contact})")
        super().__init__(**kwargs)
        self.query = combined_query(query, taxonomy) or "geotechnical geospatial"
        self.email = email

    #: Потолок выдачи за один запрос: постраничного обхода нет.
    page_limit = 1000

    async def collect(
        self, since: datetime, limit: int, until: datetime | None = None
    ) -> list[CollectedItem]:
        window = f"from-pub-date:{since.date().isoformat()}"
        if until is not None:
            # `until-pub-date` включает названный день, поэтому берём предыдущий.
            window += f",until-pub-date:{(until.date() - timedelta(days=1)).isoformat()}"
        params: dict[str, object] = {
            "query.bibliographic": self.query,
            "filter": window,
            "rows": min(limit, 1000),
            "sort": "published",
            "order": "desc",
        }
        if self.email:
            params["mailto"] = self.email
        response = await self._request("GET", "https://api.crossref.org/works", params=params)
        message = as_dict(as_dict(response.json()).get("message"))
        return [self._to_item(as_dict(work)) for work in as_list(message.get("items"))[:limit]]

    @staticmethod
    def _to_item(work: dict[str, object]) -> CollectedItem:
        doi = str(work.get("DOI") or "").lower()
        resource = as_dict(work.get("resource"))
        primary = as_dict(resource.get("primary"))
        links = as_list(work.get("link"))
        title = _first_text(work.get("title")) or "Untitled"
        return CollectedItem(
            source=SourceName.CROSSREF,
            external_id=doi or str(work.get("URL") or title),
            title=title,
            abstract=_clean_markup(work.get("abstract")),
            authors=[
                Author(
                    name=" ".join(
                        part
                        for part in (
                            str(author.get("given") or ""),
                            str(author.get("family") or ""),
                        )
                        if part
                    ),
                    orcid=_normalize_orcid(author.get("ORCID")),
                )
                for value in as_list(work.get("author"))
                if (author := as_dict(value)) and (author.get("given") or author.get("family"))
            ],
            keywords=[str(value) for value in as_list(work.get("subject")) if value],
            doi=doi or None,
            publication_date=_crossref_date(work),
            venue=_first_text(work.get("container-title")),
            citation_count=work.get("is-referenced-by-count")
            if isinstance(work.get("is-referenced-by-count"), int)
            else None,
            url=primary.get("URL") or work.get("URL") or None,
            code_url=_code_link(links),
            raw=work,
        )


def _first_text(value: object) -> str | None:
    values = as_list(value)
    return str(values[0]) if values else None


def _clean_markup(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(html.unescape(_TAG_RE.sub(" ", value)).split()) or None


def _normalize_orcid(value: object) -> str | None:
    return str(value).removeprefix("https://orcid.org/") if value else None


def _crossref_date(work: dict[str, object]) -> date | None:
    for key in ("published", "published-print", "published-online", "created"):
        parts = as_list(as_dict(work.get(key)).get("date-parts"))
        first = as_list(parts[0]) if parts else []
        if first and isinstance(first[0], int):
            padded = [*first[:3], 1, 1]
            try:
                return date(padded[0], padded[1], padded[2])
            except ValueError:
                continue
    return None


def _code_link(links: list[object]) -> str | None:
    for value in links:
        link = as_dict(value)
        url = link.get("URL")
        if isinstance(url, str) and ("github.com/" in url or "gitlab.com/" in url):
            return url
    return None
