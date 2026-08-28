"""Asynchronous GitHub repository collector."""

from __future__ import annotations

from datetime import datetime

from geonexa_proxima.collectors.base import (
    AsyncHTTPProvider,
    TaxonomyInput,
    as_dict,
    as_list,
    combined_query,
    parse_date,
)
from geonexa_proxima.domain import Author, CollectedItem, ItemKind, SourceName


class GitHubCollector(AsyncHTTPProvider):
    def __init__(
        self,
        query: str | None = None,
        taxonomy: TaxonomyInput = None,
        *,
        token: str | None = None,
        **kwargs: object,
    ) -> None:
        kwargs.setdefault("user_agent", "GeoNexa-Proxima/0.1 (GitHub research collector)")
        super().__init__(**kwargs)
        self.query = combined_query(query, taxonomy) or "geotechnical geospatial"
        self.token = token

    async def collect(self, since: datetime, limit: int) -> list[CollectedItem]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = await self._request(
            "GET",
            "https://api.github.com/search/repositories",
            params={
                "q": (
                    f"({self.query}) in:name,description,readme pushed:>={since.date().isoformat()}"
                ),
                "sort": "updated",
                "order": "desc",
                "per_page": min(limit, 100),
            },
            headers=headers,
        )
        repositories = as_list(as_dict(response.json()).get("items"))
        return [self._to_item(as_dict(repository)) for repository in repositories[:limit]]

    @staticmethod
    def _to_item(repository: dict[str, object]) -> CollectedItem:
        owner = as_dict(repository.get("owner"))
        license_info = as_dict(repository.get("license"))
        full_name = str(repository.get("full_name") or repository.get("name") or "")
        return CollectedItem(
            source=SourceName.GITHUB,
            external_id=str(repository.get("id") or full_name),
            kind=ItemKind.SOFTWARE,
            title=full_name,
            abstract=str(repository.get("description")) if repository.get("description") else None,
            authors=[Author(name=str(owner["login"]))] if owner.get("login") else [],
            keywords=[str(topic) for topic in as_list(repository.get("topics")) if topic],
            publication_date=parse_date(repository.get("created_at")),
            venue=str(license_info.get("spdx_id")) if license_info.get("spdx_id") else None,
            citation_count=repository.get("stargazers_count")
            if isinstance(repository.get("stargazers_count"), int)
            else None,
            url=repository.get("html_url") or None,
            code_url=repository.get("html_url") or None,
            raw=repository,
        )
