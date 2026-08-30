"""Курсоры источников: с какого момента собирать в следующий раз."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from geonexa_proxima.services.cursors import (
    DEFAULT_OVERLAP,
    MAX_CATCHUP,
    Cursor,
    newest,
)

NOW = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)


def _cursor(watermark: datetime | None) -> Cursor:
    return Cursor(
        query_id=uuid4(),
        source="arxiv",
        key="arxiv",
        watermark=watermark,
        last_external_id=None,
        last_success_at=None,
    )


def test_without_a_cursor_the_default_window_applies() -> None:
    fallback = NOW - timedelta(hours=192)

    assert _cursor(None).resume_from(fallback=fallback, now=NOW) == fallback


def test_resume_overlaps_the_watermark() -> None:
    """Источники индексируют задним числом: стык окон без нахлёста теряет статьи."""

    watermark = NOW - timedelta(hours=20)

    start = _cursor(watermark).resume_from(fallback=NOW - timedelta(hours=192), now=NOW)

    assert start == watermark - DEFAULT_OVERLAP


def test_long_outage_does_not_trigger_a_full_backfill() -> None:
    """После месячного простоя не пытаемся скачать месяц одним прогоном."""

    watermark = NOW - timedelta(days=90)

    start = _cursor(watermark).resume_from(fallback=NOW - timedelta(hours=192), now=NOW)

    assert start == NOW - MAX_CATCHUP
    assert start > watermark


def test_a_cursor_from_the_future_is_clamped() -> None:
    """Сбитые часы или ручная правка не должны отправлять сбор в будущее."""

    start = _cursor(NOW + timedelta(days=5)).resume_from(
        fallback=NOW - timedelta(hours=192), now=NOW
    )

    assert start <= NOW


def test_short_outage_is_covered_completely() -> None:
    """Сутки простоя: собрать надо всё пропущенное, а не последние N часов."""

    watermark = NOW - timedelta(hours=30)
    fallback = NOW - timedelta(hours=6)

    start = _cursor(watermark).resume_from(fallback=fallback, now=NOW)

    assert start < fallback, "окно по умолчанию короче простоя — дыра в корпусе"
    assert start == watermark - DEFAULT_OVERLAP


class _Item:
    def __init__(self, published: object) -> None:
        self.publication_date = published


def test_watermark_is_the_freshest_publication() -> None:
    items = [_Item(date(2026, 3, 1)), _Item(date(2026, 3, 18)), _Item(date(2026, 3, 9))]

    assert newest(items) == datetime(2026, 3, 18, tzinfo=UTC)


def test_watermark_ignores_items_without_a_date() -> None:
    assert newest([_Item(None), _Item(date(2026, 1, 5))]) == datetime(2026, 1, 5, tzinfo=UTC)
    assert newest([_Item(None)]) is None
    assert newest([]) is None


def test_naive_datetimes_are_treated_as_utc() -> None:
    """Источник может отдать дату без зоны; сравнивать её потом всё равно придётся."""

    result = newest([_Item(datetime(2026, 2, 2, 10, 0))])

    assert result is not None and result.tzinfo is not None
