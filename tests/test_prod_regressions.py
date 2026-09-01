"""Сторожа на дефекты, найденные при подготовке к проду.

Каждый тест здесь закрывает конкретный отказ, который уже был в коде и стоил
бы дорого в бою. Собраны вместе намеренно: это не «модуль такой-то», а список
граблей, на которые наступать второй раз нельзя.
"""

from __future__ import annotations

from datetime import UTC, datetime

import yaml

from geonexa_proxima.collectors.github import QUERY_LIMIT, GitHubCollector
from geonexa_proxima.db.models import DIGEST_STATUS_VALUES
from geonexa_proxima.workflows.delivery import is_permanent


def test_digest_statuses_cover_what_the_dispatcher_writes() -> None:
    """Регрессия: репозиторий отвергал статусы, которые схема разрешает.

    Диспетчер ставил задание в очередь и падал на следующей строке с
    ValueError. Срок следующего выпуска не сдвигался, профиль оставался
    «пора», и следующий прогон ставил ещё одно задание с новым digest_id —
    в чат уходил второй экземпляр того же дайджеста.
    """

    assert {"queued", "skipped", "partial"} <= set(DIGEST_STATUS_VALUES)


def test_github_query_fits_the_search_limit() -> None:
    """Регрессия: запрос был втрое длиннее лимита, и источник молчал всегда.

    Профиль сбора склеивает дюжину условий через OR — 613 символов при лимите
    поиска GitHub в 256. Ответ 422 не ретраится, `_collect_one` записывает
    ошибку, сутки закрываются успешно с дырой: GitHub не приносил ничего
    никогда, а прогон при этом был зелёный.
    """

    taxonomy = yaml.safe_load(open("config/taxonomy.yaml", encoding="utf-8"))
    collector = GitHubCollector(taxonomy=taxonomy.get("discovery_queries"))
    window_start = datetime(2026, 8, 30, tzinfo=UTC)

    query = collector._search_query(window_start, datetime(2026, 8, 31, tzinfo=UTC))

    assert len(query) <= QUERY_LIMIT
    # Условия не выброшены целиком — иначе запрос перестал бы искать по делу.
    assert query.count(" OR ") >= 2


def test_github_query_rotates_across_days() -> None:
    """За несколько суток в запрос попадают все условия профиля, а не одни и те же."""

    taxonomy = yaml.safe_load(open("config/taxonomy.yaml", encoding="utf-8"))
    collector = GitHubCollector(taxonomy=taxonomy.get("discovery_queries"))

    first = collector._search_query(
        datetime(2026, 8, 30, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)
    )
    second = collector._search_query(
        datetime(2026, 9, 5, tzinfo=UTC), datetime(2026, 9, 6, tzinfo=UTC)
    )

    assert first != second


def test_permanent_telegram_errors_are_not_retried() -> None:
    """«Бота заблокировали» не лечится пятью попытками с нарастающей паузой."""

    assert is_permanent(RuntimeError("Forbidden: bot was blocked by the user"))
    assert is_permanent(RuntimeError("Bad Request: chat not found"))
    assert is_permanent(RuntimeError("Forbidden: bot was kicked from the supergroup chat"))


def test_temporary_telegram_errors_are_retried() -> None:
    """А сеть, 429 и внутренние ошибки Telegram — лечатся."""

    assert not is_permanent(TimeoutError("Read timeout"))
    assert not is_permanent(RuntimeError("Bad Gateway"))

    flood = RuntimeError("Too Many Requests: retry after 30")
    flood.retry_after = 30  # type: ignore[attr-defined]
    assert not is_permanent(flood)
