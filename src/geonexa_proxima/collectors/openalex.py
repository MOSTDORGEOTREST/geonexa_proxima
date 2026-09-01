"""Asynchronous OpenAlex works collector."""

from __future__ import annotations

from datetime import datetime, timedelta

from geonexa_proxima.collectors.base import (
    AsyncHTTPProvider,
    TaxonomyInput,
    as_dict,
    as_list,
    combined_query,
    parse_date,
)
from geonexa_proxima.domain import Author, CollectedItem, SourceName


class OpenAlexCollector(AsyncHTTPProvider):
    def __init__(
        self,
        query: str | None = None,
        taxonomy: TaxonomyInput = None,
        *,
        email: str | None = None,
        **kwargs: object,
    ) -> None:
        contact = f"mailto:{email}" if email else "https://github.com/geonexa-proxima"
        kwargs.setdefault("user_agent", f"GeoNexa-Proxima/0.1 ({contact})")
        super().__init__(**kwargs)
        self.query = combined_query(query, taxonomy) or "geotechnical geospatial"
        self.email = email

    #: Сколько записей OpenAlex отдаёт за один запрос. Постраничного обхода
    #: нет, поэтому это же число — реальный потолок выдачи за окно.
    page_limit = 200

    async def collect(
        self, since: datetime, limit: int, until: datetime | None = None
    ) -> list[CollectedItem]:
        window = f"from_publication_date:{since.date().isoformat()}"
        if until is not None:
            # Обе границы у OpenAlex включающие, а окно суток — полуинтервал:
            # день `until` принадлежит следующему прогону.
            window += f",to_publication_date:{(until.date() - timedelta(days=1)).isoformat()}"
        params: dict[str, object] = {
            "search": self.query,
            "filter": window,
            "per-page": min(limit, 200),
            "sort": "publication_date:desc",
        }
        if self.email:
            params["mailto"] = self.email
        response = await self._request("GET", "https://api.openalex.org/works", params=params)
        results = as_list(as_dict(response.json()).get("results"))
        return [self._to_item(as_dict(work)) for work in results[:limit]]

    @staticmethod
    def _to_item(work: dict[str, object]) -> CollectedItem:
        ids = as_dict(work.get("ids"))
        primary_location = as_dict(work.get("primary_location"))
        source = as_dict(primary_location.get("source"))
        open_access = as_dict(work.get("open_access"))
        keywords = [
            str(as_dict(value).get("display_name"))
            for value in as_list(work.get("keywords")) + as_list(work.get("topics"))
            if as_dict(value).get("display_name")
        ]
        authors = []
        for authorship in as_list(work.get("authorships")):
            author = as_dict(as_dict(authorship).get("author"))
            name = author.get("display_name")
            if name:
                authors.append(
                    Author(
                        name=str(name),
                        orcid=_strip_prefix(author.get("orcid"), "https://orcid.org/"),
                    )
                )
        openalex_id = _strip_prefix(work.get("id"), "https://openalex.org/") or ""
        return CollectedItem(
            source=SourceName.OPENALEX,
            external_id=openalex_id,
            title=str(work.get("title") or work.get("display_name") or "Untitled"),
            abstract=_inverted_abstract(work.get("abstract_inverted_index")),
            authors=authors,
            keywords=list(dict.fromkeys(keywords)),
            doi=_strip_prefix(ids.get("doi") or work.get("doi"), "https://doi.org/"),
            publication_date=parse_date(work.get("publication_date")),
            venue=str(source.get("display_name")) if source.get("display_name") else None,
            citation_count=_as_int(work.get("cited_by_count")),
            url=primary_location.get("landing_page_url") or ids.get("openalex") or None,
            code_url=(
                open_access.get("oa_url") if _looks_like_code(open_access.get("oa_url")) else None
            ),
            raw=work,
        )


def _strip_prefix(value: object, prefix: str) -> str | None:
    if not value:
        return None
    text = str(value)
    return text.removeprefix(prefix)


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _looks_like_code(value: object) -> bool:
    return isinstance(value, str) and ("github.com/" in value or "gitlab.com/" in value)


def _inverted_abstract(value: object) -> str | None:
    index = as_dict(value)
    positioned: list[tuple[int, str]] = []
    for word, positions in index.items():
        for position in as_list(positions):
            if isinstance(position, int):
                positioned.append((position, word))
    return " ".join(word for _, word in sorted(positioned)) or None
