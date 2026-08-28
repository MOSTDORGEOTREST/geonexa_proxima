"""Optional Semantic Scholar metadata enrichment."""

from __future__ import annotations

import asyncio
from urllib.parse import quote

import httpx

from geonexa_proxima.collectors.base import AsyncHTTPProvider, as_dict, as_list, parse_date
from geonexa_proxima.domain import Author, CollectedItem


class SemanticScholarEnricher(AsyncHTTPProvider):
    def __init__(self, *, api_key: str | None = None, **kwargs: object) -> None:
        kwargs.setdefault("user_agent", "GeoNexa-Proxima/0.1 (Semantic Scholar research enricher)")
        super().__init__(**kwargs)
        self.api_key = api_key

    async def enrich(self, item: CollectedItem) -> CollectedItem:
        identifier = _identifier(item)
        if not identifier:
            return item
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        try:
            response = await self._request(
                "GET",
                f"https://api.semanticscholar.org/graph/v1/paper/{quote(identifier, safe='')}",
                params={
                    "fields": (
                        "paperId,title,abstract,authors,year,publicationDate,venue,citationCount,"
                        "externalIds,url,openAccessPdf,fieldsOfStudy"
                    )
                },
                headers=headers,
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return item
            raise
        data = as_dict(response.json())
        external_ids = as_dict(data.get("externalIds"))
        semantic_authors = [
            Author(name=str(author["name"]))
            for value in as_list(data.get("authors"))
            if (author := as_dict(value)).get("name")
        ]
        open_pdf = as_dict(data.get("openAccessPdf"))
        raw = {**item.raw, "semantic_scholar": data}
        return CollectedItem.model_validate(
            {
                **item.model_dump(),
                "abstract": item.abstract or data.get("abstract"),
                "authors": item.authors or semantic_authors,
                "doi": item.doi or external_ids.get("DOI"),
                "arxiv_id": item.arxiv_id or external_ids.get("ArXiv"),
                "publication_date": item.publication_date
                or parse_date(data.get("publicationDate")),
                "venue": item.venue or data.get("venue"),
                "citation_count": data.get("citationCount")
                if isinstance(data.get("citationCount"), int)
                else item.citation_count,
                "url": item.url or data.get("url") or open_pdf.get("url"),
                "keywords": item.keywords
                or [str(value) for value in as_list(data.get("fieldsOfStudy"))],
                "raw": raw,
            }
        )

    async def enrich_many(
        self, items: list[CollectedItem], *, concurrency: int = 4
    ) -> list[CollectedItem]:
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def guarded(item: CollectedItem) -> CollectedItem:
            async with semaphore:
                return await self.enrich(item)

        return list(await asyncio.gather(*(guarded(item) for item in items)))


def _identifier(item: CollectedItem) -> str | None:
    if item.doi:
        return f"DOI:{item.doi}"
    if item.arxiv_id:
        return f"ARXIV:{item.arxiv_id}"
    return None
