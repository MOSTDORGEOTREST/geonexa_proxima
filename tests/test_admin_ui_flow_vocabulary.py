"""Словарь админки не должен разъезжаться с каталогом флоу.

Кнопки ручного запуска в `admin-ui/src/lib/flows.ts` названы человеческими
словами, но за каждой стоит ключ расписания. Опечатка или переименование флоу
в Python не ломает сборку фронта — кнопка просто перестаёт находить строку и
молча выключается. Тест сверяет обе стороны.
"""

from __future__ import annotations

import pathlib
import re

from geonexa_proxima.flow_catalog import BY_KEY

ROOT = pathlib.Path(__file__).resolve().parents[1]
FLOWS_TS = ROOT / "admin-ui" / "src" / "lib" / "flows.ts"

#: Виды расписаний, которые вообще существуют (`schedules.kind`).
KNOWN_KINDS = {spec.schedule_kind for spec in BY_KEY.values()}


def _source() -> str | None:
    return FLOWS_TS.read_text(encoding="utf-8") if FLOWS_TS.is_file() else None


def test_every_button_points_at_an_existing_flow() -> None:
    source = _source()
    if source is None:
        return  # админка не выкачана — не наше дело
    keys = set(re.findall(r"^\s*key: '([a-z0-9-]+)',", source, flags=re.M))
    assert keys, "в flows.ts не нашлось ни одного ключа флоу"
    unknown = sorted(keys - set(BY_KEY))
    assert not unknown, "Кнопки ссылаются на несуществующие флоу: " + ", ".join(unknown)


def test_stage_kinds_exist_in_the_catalog() -> None:
    """Иначе группа в таблице расписаний останется вечно пустой."""

    source = _source()
    if source is None:
        return
    blocks = re.findall(r"kinds: \[([^\]]*)\]", source)
    kinds = {
        value.strip().strip("'") for block in blocks for value in block.split(",") if value.strip()
    }
    unknown = sorted(kinds - KNOWN_KINDS)
    assert not unknown, "Этапы ссылаются на несуществующие виды расписаний: " + ", ".join(unknown)


def test_every_flow_is_reachable_from_the_ui() -> None:
    """Флоу, которого нет ни в одной группе, не виден администратору вовсе."""

    source = _source()
    if source is None:
        return
    blocks = re.findall(r"kinds: \[([^\]]*)\]", source)
    covered = {
        value.strip().strip("'") for block in blocks for value in block.split(",") if value.strip()
    }
    missing = sorted(KNOWN_KINDS - covered)
    assert not missing, (
        "Эти виды расписаний не попадают ни в один этап админки и выпадут "
        "в «Прочее»: " + ", ".join(missing)
    )
