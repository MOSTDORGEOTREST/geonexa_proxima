"""Shared HTTP and query helpers for external collectors."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypeAlias

import httpx

TaxonomyInput: TypeAlias = Mapping[str, object] | Sequence[str] | None


def taxonomy_terms(taxonomy: TaxonomyInput) -> list[str]:
    """Flatten a taxonomy into unique non-empty search terms."""

    terms: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, str):
            if value.strip():
                terms.append(value.strip())
        elif isinstance(value, Mapping):
            for key, nested in value.items():
                visit(key)
                visit(nested)
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            for nested in value:
                visit(nested)

    visit(taxonomy)
    return list(dict.fromkeys(terms))


def combined_query(query: str | None, taxonomy: TaxonomyInput) -> str:
    parts = ([query.strip()] if query and query.strip() else []) + taxonomy_terms(taxonomy)
    return " OR ".join(dict.fromkeys(parts))


def parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        try:
            return parsedate_to_datetime(value).date()
        except (TypeError, ValueError):
            return None


def as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


class AsyncHTTPProvider:
    """Small retrying HTTP base with injectable clients for tests and reuse."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        user_agent: str = "GeoNexa-Proxima/0.1 (research discovery; contact: unknown)",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._retries = max(1, retries)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
        )

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self._retries):
            try:
                response = await self._client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as error:
                last_error = error
                retryable = not isinstance(error, httpx.HTTPStatusError) or (
                    error.response.status_code == 429 or error.response.status_code >= 500
                )
                if not retryable or attempt + 1 == self._retries:
                    raise
                retry_after = (
                    error.response.headers.get("Retry-After")
                    if isinstance(error, httpx.HTTPStatusError)
                    else None
                )
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else 0.5 * 2**attempt
                )
                await asyncio.sleep(delay)
        raise RuntimeError("HTTP request failed") from last_error

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> AsyncHTTPProvider:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def is_since(value: date | None, since: datetime) -> bool:
    return value is None or value >= since.date()


def in_window(value: date | None, since: datetime, until: datetime | None) -> bool:
    """Попадает ли дата публикации в окно ``[since, until)``.

    Верхняя граница исключающая и сравнивается по дате: окно суток задаётся
    как полночь-полночь, и материал, вышедший в день ``until``, принадлежит
    уже следующему окну. Без этого соседние сутки перекрывались бы на день, а
    один и тот же материал попадал в два прогона.

    Дата неизвестна — материал пропускаем: у части источников её просто нет, и
    отбрасывать их молча хуже, чем взять лишнее.
    """

    if value is None:
        return True
    if value < since.date():
        return False
    return until is None or value < until.date()
