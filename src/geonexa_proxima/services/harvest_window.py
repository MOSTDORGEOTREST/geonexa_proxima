"""Какие сутки собирать в этом прогоне.

Сбор идёт сутками, а не «последними N часами». Причина практическая: запрос за
неделю к arXiv или Crossref упирается в лимит выдачи, и часть материалов молча
не доезжает — воронка при этом выглядит нормально, просто корпус беднее, чем
должен быть. Сутки помещаются в один запрос к каждому источнику.

**Сутки считаются в UTC, и это не мелочь.** У всех четырёх источников окно
задаётся датами в их собственном понимании: `from_publication_date` у OpenAlex,
`until-pub-date` у Crossref, `pushed:X..Y` у GitHub, `submittedDate` у arXiv —
и все они про UTC. Сутки, сдвинутые в московский пояс, разъезжаются с этими
фильтрами: окно «30 августа по Москве» — это 29-е 21:00 UTC — 30-е 21:00 UTC,
источникам уходит запрос за 29-е, соседнее окно спрашивает 28-е и 29-е, а наш
же фильтр по ответу выбрасывает всё, что вышло 30-го. Календарные сутки
администратора и календарные сутки источника — разные вещи, и притворяться, что
это одно и то же, значит терять материалы и не замечать этого.

Модуль намеренно чистый: ни базы, ни Prefect, ни настроек. Он получает время и
последнюю собранную границу, а возвращает список окон — это делает нарезку
проверяемой без инфраструктуры.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

#: Больше этого одним прогоном не догоняем. Глубокий провал — это ручной
#: прогон с указанными датами, а не сюрприз на сорок запросов к источникам
#: в ночь, когда воркер наконец поднялся.
DEFAULT_MAX_CATCHUP_DAYS = 7


@dataclass(frozen=True, slots=True)
class Window:
    """Одни сутки UTC: полночь включительно — следующая полночь исключительно."""

    day: date
    since: datetime
    until: datetime

    @property
    def label(self) -> str:
        return f"{self.day:%d.%m.%Y}"


def day_window(day: date) -> Window:
    """Сутки ``day`` в UTC."""

    start = datetime.combine(day, time.min, tzinfo=UTC)
    return Window(day=day, since=start, until=start + timedelta(days=1))


def last_complete_day(now: datetime) -> date:
    """Последние завершившиеся сутки UTC — дальше этого собирать нечего."""

    return (now.astimezone(UTC) - timedelta(days=1)).date()


def day_range(first: date, last: date, *, now: datetime | None = None) -> list[Window]:
    """Подряд идущие сутки с ``first`` по ``last`` включительно.

    Верхняя граница отсекается по последним завершившимся суткам. Сутки,
    которые ещё идут, собрались бы наполовину и записались бы как собранные
    целиком; сутки в будущем вдобавок сдвигали бы вперёд границу собранного —
    после ошибки в годе (2027 вместо 2026) плановый сбор молча не работал бы
    до самого 2027-го.
    """

    if last < first:
        raise ValueError(f"Конец диапазона {last} раньше начала {first}")
    horizon = last_complete_day(now or datetime.now(UTC))
    if first > horizon:
        raise ValueError(
            f"Сутки {first:%d.%m.%Y} ещё не наступили или не закончились: "
            f"собирать можно по {horizon:%d.%m.%Y} включительно."
        )
    last = min(last, horizon)
    total = (last - first).days + 1
    return [day_window(first + timedelta(days=offset)) for offset in range(total)]


def scheduled_windows(
    *,
    now: datetime,
    covered_until: datetime | None = None,
    max_days: int = DEFAULT_MAX_CATCHUP_DAYS,
    force: bool = False,
) -> list[Window]:
    """Что собирать плановому прогону.

    Обычный случай — последние завершившиеся сутки UTC. Если прошлые прогоны не
    отработали (упал воркер, стоял контейнер), к ним добавляются пропущенные
    дни, но не больше ``max_days``: иначе первый же прогон после долгого
    простоя устроит источникам сотню запросов подряд и получит бан вместо
    материалов.

    ``covered_until`` — верхняя граница последнего успешного прогона. Она уже
    исключающая, поэтому день, которому она принадлежит, и есть первый
    несобранный.

    ``force`` нужен ручному запуску: он собирает последние сутки заново, даже
    если они уже собраны. Пустой ответ на нажатие кнопки неотличим от поломки.
    """

    yesterday = last_complete_day(now)
    horizon = yesterday - timedelta(days=max(1, max_days) - 1)
    if covered_until is None:
        first = yesterday
    else:
        first = covered_until.astimezone(UTC).date()
        if first > yesterday:
            # Граница собранного в будущем — это след ошибочного прогона или
            # сбитых часов. Без этой ветки плановый сбор просто перестал бы
            # работать до той даты, и в логе стояло бы «собирать нечего».
            return [day_window(yesterday)] if force else []
        first = max(first, horizon)
    return day_range(first, yesterday, now=now)


def parse_day(value: str | date | None) -> date | None:
    """Дата из параметра флоу. Prefect отдаёт их строками из JSON."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError as error:
        raise ValueError(
            f"Дата «{value}» не разобрана: ожидается ГГГГ-ММ-ДД, например 2026-08-30."
        ) from error
