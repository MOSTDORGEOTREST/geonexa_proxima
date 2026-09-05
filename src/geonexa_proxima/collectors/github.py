"""Asynchronous GitHub repository collector."""

from __future__ import annotations

import asyncio
import re
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
#: нет, поэтому это же число — реальный потолок выдачи за окно на запрос.
PAGE_LIMIT = 100

#: Сколько простых запросов уходит в GitHub за одно окно. Поиск репозиториев
#: не понимает скобок и допускает не больше пяти операторов AND/OR/NOT на
#: запрос: булево выражение профиля («(a AND b) OR (c AND d) OR …») он
#: отвергал с 422, и источник молчал всегда. Поэтому выражение раскладывается
#: на простые конъюнкции — по одной на запрос, — а за окно уходит несколько
#: таких запросов. Лимит поиска — 30 запросов в минуту с токеном и 10 без,
#: так что четыре с паузой укладываются в любой из них.
QUERIES_PER_WINDOW = 4

#: Пауза между запросами поиска. Без неё GitHub отвечает 403 «secondary rate
#: limit» уже на третьем-четвёртом запросе подряд.
QUERY_PAUSE_SECONDS = 1.2

_BOOLEAN_OR = re.compile(r"\s+OR\s+")
_BOOLEAN_AND = re.compile(r"\s+AND\s+")
_BOOLEAN_NOT = re.compile(r"\s+AND\s+NOT\s+\([^)]*\)|\s+NOT\s+\S+")
_QUALIFIER_IN = re.compile(r"\bin:\S+")


def _split_top_level(query: str, separator: re.Pattern[str]) -> list[str]:
    """Разбить строку по оператору, не залезая внутрь скобок."""

    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(query):
        char = query[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            match = separator.match(query, index)
            if match:
                parts.append(query[start:index])
                index = match.end()
                start = index
                continue
        index += 1
    parts.append(query[start:])
    return [part.strip() for part in parts if part.strip()]


def _strip_parentheses(part: str) -> str:
    part = part.strip()
    while part.startswith("(") and part.endswith(")"):
        # Снимаем только парные внешние скобки: «(a) OR (b)» ими не является.
        depth = 0
        balanced = True
        for index, char in enumerate(part):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(part) - 1:
                    balanced = False
                    break
        if not balanced:
            break
        part = part[1:-1].strip()
    return part


def simple_queries(query: str) -> list[str]:
    """Разложить булево выражение на список простых запросов GitHub.

    OR на любой глубине становится границей запроса, AND — пробелом (для
    поиска GitHub пробел и есть неявное AND), NOT-хвосты отбрасываются:
    исключения дешевле применить гейтом по ответу, чем объяснять их поиску,
    который понимает только «-слово». Каждый результат — конъюнкция фраз в
    кавычках, которую поиск репозиториев принимает без вопросов.
    """

    flat: list[str] = []

    def visit(expression: str) -> None:
        expression = _strip_parentheses(expression)
        if not expression:
            return
        alternatives = _split_top_level(expression, _BOOLEAN_OR)
        if len(alternatives) > 1:
            for alternative in alternatives:
                visit(alternative)
            return
        conjuncts = _split_top_level(expression, _BOOLEAN_AND)
        if len(conjuncts) > 1:
            pieces: list[str] = []
            for conjunct in conjuncts:
                if conjunct.upper().startswith("NOT ") or conjunct.upper().startswith("NOT("):
                    continue
                nested = simple_queries(conjunct)
                # Внутри AND-ветки собственный OR раскрывать не станем — это
                # взрыв комбинаций; берём первую альтернативу, остальные
                # придут с ротацией в другие сутки через соседние ветки.
                if nested:
                    pieces.append(nested[0])
            if pieces:
                flat.append(" ".join(pieces))
            return
        cleaned = _BOOLEAN_NOT.sub("", expression).strip()
        # Квалификатор полей добавляет сам коллектор — из запроса профиля его
        # убираем, иначе он окажется в строке дважды.
        cleaned = _QUALIFIER_IN.sub(" ", cleaned).strip()
        cleaned = _strip_parentheses(cleaned)
        if cleaned:
            flat.append(cleaned)

    visit(query)
    return list(dict.fromkeys(flat))


def _fit(query: str, reserved: int) -> str:
    """Уложить один простой запрос в остаток бюджета длины.

    Обрезаем по границе слова: обрезанный запрос всё-таки ищет, а превысивший
    лимит не ищет вовсе.
    """

    budget = QUERY_LIMIT - reserved
    if len(query) <= budget:
        return query
    cut = query[:budget]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    # Незакрытая кавычка после обрезки тоже даёт 422.
    if cut.count('"') % 2:
        cut = cut[: cut.rfind('"')].rstrip()
    return cut


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
        queries_per_window: int = QUERIES_PER_WINDOW,
        **kwargs: object,
    ) -> None:
        kwargs.setdefault("user_agent", "GeoNexa-Proxima/0.1 (GitHub research collector)")
        super().__init__(**kwargs)
        self.query = combined_query(query, taxonomy) or "geotechnical geospatial"
        self.token = token
        self.queries_per_window = max(1, queries_per_window)

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
        seen: dict[str, CollectedItem] = {}
        for index, query in enumerate(self._search_queries(since, until)):
            if index:
                await asyncio.sleep(QUERY_PAUSE_SECONDS)
            response = await self._request(
                "GET",
                "https://api.github.com/search/repositories",
                params={
                    "q": query,
                    "sort": "updated",
                    "order": "desc",
                    "per_page": min(limit, PAGE_LIMIT),
                },
                headers=headers,
            )
            for repository in as_list(as_dict(response.json()).get("items")):
                item = self._to_item(as_dict(repository))
                # Один репозиторий отвечает на несколько запросов профиля —
                # в корпус он должен попасть один раз.
                seen.setdefault(item.external_id, item)
            if len(seen) >= limit:
                break
        return list(seen.values())[:limit]

    def _search_queries(self, since: datetime, until: datetime | None) -> list[str]:
        """Простые запросы на окно: ротация по дате, чтобы обойти весь профиль.

        Смещение считается от даты окна, поэтому повторный прогон за те же
        сутки даёт те же запросы — иначе добор материалов зависел бы от того,
        в какой день его запустили.
        """

        suffix = f" in:name,description,readme pushed:{_pushed(since, until)}"
        parts = simple_queries(self.query) or [self.query]
        start = since.date().toordinal() * self.queries_per_window % len(parts)
        rotated = parts[start:] + parts[:start]
        chosen = rotated[: self.queries_per_window]
        return [_fit(part, len(suffix)) + suffix for part in chosen]

    def _search_query(self, since: datetime, until: datetime | None) -> str:
        """Первый запрос окна — для тестов и отладки."""

        return self._search_queries(since, until)[0]

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
