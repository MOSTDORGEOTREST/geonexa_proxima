"""Навигация админки: каждая страница живёт в разделе, каждый пункт ведёт на страницу.

Разделы описаны в `admin-ui/src/lib/nav.ts` и собираются в шапку из этого
одного места. Разъехаться с реальными маршрутами они могут молча: страница,
которой нет ни в одном разделе, не появляется в шапке и достижима только по
прямой ссылке, а пункт без страницы даёт 404 при клике. Ни то, ни другое не
ловится сборкой.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
UI = ROOT / "admin-ui" / "src"
NAV = UI / "lib" / "nav.ts"
PAGES = UI / "routes" / "(app)"

#: Страницы вне разделов — осознанные исключения.
#:
#: `/guide` открывают по ссылке из редактора профиля: отдельный пункт в шапке
#: ей не нужен. `/schedules` — прежний адрес расписаний, он только уводит на
#: «Запуски» и существует ради закладок.
OUTSIDE = {"/guide", "/schedules"}


def _nav_hrefs() -> set[str]:
    return set(re.findall(r"href: '([^']+)'", NAV.read_text(encoding="utf-8")))


def _routes() -> set[str]:
    found: set[str] = set()
    for page in PAGES.rglob("+page.svelte"):
        relative = page.parent.relative_to(PAGES).as_posix()
        # Детальные страницы (`/subscribers/[id]`) принадлежат разделу родителя.
        if "[" in relative:
            continue
        found.add("/" if relative == "." else f"/{relative}")
    return found


def test_every_page_belongs_to_a_section() -> None:
    if not NAV.is_file():
        return  # админка не выкачана — не наше дело
    orphans = sorted(_routes() - _nav_hrefs() - OUTSIDE)
    assert not orphans, "Страницы не попали ни в один раздел и не видны в шапке: " + ", ".join(
        orphans
    )


def test_every_nav_item_has_a_page() -> None:
    if not NAV.is_file():
        return
    broken = sorted(_nav_hrefs() - _routes())
    assert not broken, "Пункты меню ведут в никуда: " + ", ".join(broken)


def test_pending_badge_lives_on_one_item_only() -> None:
    """Счётчик заявок один: два счётчика в шапке — это два разных числа на вид."""

    if not NAV.is_file():
        return
    assert NAV.read_text(encoding="utf-8").count("pending: true") == 1
