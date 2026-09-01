"""Asynchronous GitHub repository collector."""

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
from geonexa_proxima.domain import Author, CollectedItem, ItemKind, SourceName

#: Потолок длины поискового запроса GitHub — 256 символов вместе со всеми
#: квалификаторами. Более длинный запрос отвергается с 422 Validation Failed,
#: а 422 не ретраится: источник просто не приносит ничего и никогда.
QUERY_LIMIT = 256

#: Сколько записей GitHub отдаёт за один запрос. Постраничного обхода здесь
#: нет, поэтому это же число — реальный потолок выдачи за окно.
PAGE_LIMIT = 100


def _fit(query: str, reserved: int, offset: int = 0) -> str:
    """Уложить набор OR-условий в остаток бюджета длины.

    Профиль сбора склеивает дюжину условий через OR, и вместе они дают шестьсот
    с лишним символов — втрое больше, чем принимает поиск GitHub. Отбрасываем
    хвост, а не падаем: часть условий приносит материалы, целиком отвергнутый
    запрос не приносит ничего.

    ``offset`` проворачивает список: сбор идёт каждые сутки, и за три-четыре
    прогона в запрос попадают все условия профиля, а не одни и те же четыре
    первых. Смещение считается от даты окна, поэтому повторный прогон за те же
    сутки даёт тот же запрос — иначе добор материалов зависел бы от того, в
    какой день его запустили.
    """

    budget = QUERY_LIMIT - reserved
    parts = [part.strip() for part in query.split(" OR ") if part.strip()]
    if not parts:
        return ""
    start = offset % len(parts)
    rotated = parts[start:] + parts[:start]
    kept: list[str] = []
    length = 0
    for part in rotated:
        addition = len(part) + (4 if kept else 0)
        if length + addition > budget:
            break
        kept.append(part)
        length += addition
    if not kept:
        # Даже одно условие не влезло — режем его по живому: обрезанный запрос
        # всё-таки ищет, а превысивший лимит не ищет вовсе.
        return rotated[0][:budget]
    return " OR ".join(kept)


def _pushed(since: datetime, until: datetime | None) -> str:
    """Диапазон `pushed` для поиска GitHub.

    Без верхней границы — открытый интервал `>=`, как раньше. С границей —
    `X..Y`, где Y на день меньше: обе границы у GitHub включающие, а сутки
    задаются полночью следующего дня.
    """

    start = since.date().isoformat()
    if until is None:
        return f">={start}"
    return f"{start}..{(until.date() - timedelta(days=1)).isoformat()}"


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

    #: Читается сборщиком: по нему видно, что источник упёрся в свой потолок,
    #: а не в `MAX_ITEMS_PER_SOURCE`.
    page_limit = PAGE_LIMIT

    async def collect(
        self, since: datetime, limit: int, until: datetime | None = None
    ) -> list[CollectedItem]:
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
                "q": self._search_query(since, until),
                "sort": "updated",
                "order": "desc",
                "per_page": min(limit, 100),
            },
            headers=headers,
        )
        repositories = as_list(as_dict(response.json()).get("items"))
        return [self._to_item(as_dict(repository)) for repository in repositories[:limit]]

    def _search_query(self, since: datetime, until: datetime | None) -> str:
        suffix = f") in:name,description,readme pushed:{_pushed(since, until)}"
        return "(" + _fit(self.query, len(suffix) + 1, since.date().toordinal()) + suffix

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
