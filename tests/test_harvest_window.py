"""Нарезка прогона по суткам: что именно соберёт плановый запуск.

Проверяется не арифметика с датами ради арифметики, а поведение, за которым
приходят: последние завершившиеся сутки, догон пропущенного с пределом и отказ
собирать то, что уже собрано.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from geonexa_proxima.collectors.base import in_window
from geonexa_proxima.services.harvest_window import (
    day_range,
    day_window,
    parse_day,
    scheduled_windows,
)


def _at(moment: str) -> datetime:
    return datetime.fromisoformat(moment).replace(tzinfo=UTC)


def test_day_window_is_a_utc_day() -> None:
    window = day_window(date(2026, 8, 30))

    assert window.since == datetime(2026, 8, 30, 0, 0, tzinfo=UTC)
    assert window.until == datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
    assert window.label == "30.08.2026"


def test_window_matches_what_sources_are_asked_for() -> None:
    """Регрессия и главная причина считать сутки в UTC.

    Окно, сдвинутое в московский пояс, — это 29-е 21:00 UTC — 30-е 21:00 UTC.
    Источникам из него уходит запрос по датам `since.date()`…`until.date()-1`,
    то есть за 29-е, соседнее окно спрашивает 28-е и 29-е, а фильтр по ответу
    выбрасывает всё, что вышло 30-го. Сутки в UTC совпадают с тем, что
    спрашивают у источника, ровно в одну дату.
    """

    window = day_window(date(2026, 8, 30))

    # То, что уходит в OpenAlex, Crossref и GitHub: ровно одна дата.
    assert window.since.date() == date(2026, 8, 30)
    assert window.until.date() - timedelta(days=1) == date(2026, 8, 30)
    # И то, что оставляет фильтр по ответу, — та же самая дата.
    assert in_window(date(2026, 8, 30), window.since, window.until)
    assert not in_window(date(2026, 8, 29), window.since, window.until)
    assert not in_window(date(2026, 8, 31), window.since, window.until)


def test_scheduled_run_takes_the_last_finished_day() -> None:
    windows = scheduled_windows(now=_at("2026-08-31T09:00:00"), covered_until=None)

    assert [window.day for window in windows] == [date(2026, 8, 30)]


def test_night_run_before_utc_midnight_does_not_grab_an_unfinished_day() -> None:
    """01:00 по Москве — это ещё 22:00 UTC предыдущих суток.

    Сутки, которые в этот момент идут, брать нельзя: они соберутся наполовину,
    прогон запишет их как собранные, и вторая половина не попадёт в корпус
    никогда.
    """

    windows = scheduled_windows(now=_at("2026-08-30T22:00:00"), covered_until=None)

    assert [window.day for window in windows] == [date(2026, 8, 29)]


def test_scheduled_run_catches_up_missed_days() -> None:
    covered = day_window(date(2026, 8, 27)).until

    windows = scheduled_windows(now=_at("2026-08-31T09:00:00"), covered_until=covered)

    assert [window.day for window in windows] == [
        date(2026, 8, 28),
        date(2026, 8, 29),
        date(2026, 8, 30),
    ]


def test_catch_up_is_capped() -> None:
    """После долгого простоя один прогон не идёт в источники сотней запросов."""

    covered = day_window(date(2026, 1, 1)).until

    windows = scheduled_windows(now=_at("2026-08-31T09:00:00"), covered_until=covered, max_days=3)

    assert [window.day for window in windows] == [
        date(2026, 8, 28),
        date(2026, 8, 29),
        date(2026, 8, 30),
    ]


def test_nothing_to_collect_when_yesterday_is_already_in() -> None:
    covered = day_window(date(2026, 8, 30)).until

    assert scheduled_windows(now=_at("2026-08-31T09:00:00"), covered_until=covered) == []


def test_manual_run_collects_yesterday_again() -> None:
    """Ручной запуск не имеет права молча ничего не сделать.

    Пустой ответ на нажатие кнопки в админке неотличим от поломки, поэтому
    последние сутки собираются заново.
    """

    covered = day_window(date(2026, 8, 30)).until

    windows = scheduled_windows(now=_at("2026-08-31T09:00:00"), covered_until=covered, force=True)

    assert [window.day for window in windows] == [date(2026, 8, 30)]


def test_day_range_is_inclusive_and_contiguous() -> None:
    windows = day_range(date(2026, 8, 28), date(2026, 8, 30))

    assert [window.day for window in windows] == [
        date(2026, 8, 28),
        date(2026, 8, 29),
        date(2026, 8, 30),
    ]
    # Соседние сутки стыкуются без зазора и без нахлёста: материал попадает
    # ровно в один прогон.
    assert windows[0].until == windows[1].since
    assert windows[1].until == windows[2].since


def test_day_range_rejects_reversed_bounds() -> None:
    with pytest.raises(ValueError, match="раньше начала"):
        day_range(date(2026, 8, 30), date(2026, 8, 28))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-30", date(2026, 8, 30)),
        ("2026-08-30T12:00:00", date(2026, 8, 30)),
        (date(2026, 8, 30), date(2026, 8, 30)),
        (None, None),
        ("", None),
    ],
)
def test_parse_day(value: object, expected: date | None) -> None:
    assert parse_day(value) == expected  # type: ignore[arg-type]


def test_parse_day_explains_bad_input() -> None:
    with pytest.raises(ValueError, match="ГГГГ-ММ-ДД"):
        parse_day("30 августа")


def test_future_day_is_refused() -> None:
    """Регрессия: ручной прогон за будущие сутки замораживал плановый сбор.

    Опечатка в годе (2027 вместо 2026) закрывалась прогоном как успешная,
    граница собранного уезжала на год вперёд, и каждую ночь плановый сбор
    писал «собирать нечего» — без единой ошибки в логе.
    """

    with pytest.raises(ValueError, match="ещё не наступили"):
        day_range(date(2027, 8, 30), date(2027, 8, 30), now=_at("2026-08-31T09:00:00"))


def test_range_is_clamped_to_the_last_finished_day() -> None:
    """Сутки, которые идут прямо сейчас, собрались бы наполовину."""

    windows = day_range(date(2026, 8, 29), date(2026, 9, 5), now=_at("2026-08-31T09:00:00"))

    assert [window.day for window in windows] == [date(2026, 8, 29), date(2026, 8, 30)]


def test_future_covered_boundary_does_not_freeze_the_schedule() -> None:
    """Даже если такая запись уже попала в базу, плановый сбор продолжает идти."""

    broken = day_window(date(2027, 1, 1)).until

    windows = scheduled_windows(now=_at("2026-08-31T09:00:00"), covered_until=broken, force=True)

    assert [window.day for window in windows] == [date(2026, 8, 30)]
