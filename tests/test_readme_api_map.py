"""README обещает конкретные адреса — они должны существовать.

Таблица эндпоинтов в README устаревает первой: переименовали путь, а документ
остался. Тест сверяет её со схемой приложения, чтобы читатель не тратил время
на несуществующий адрес.
"""

from __future__ import annotations

import pathlib
import re

from geonexa_proxima.api.application import create_app
from geonexa_proxima.config import Settings

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Сокращения внутри строки таблицы: «`/subscribers/{subscriber_id}` и его
#: `/activity`». Полный путь рядом, повторять его целиком незачем.
SHORTHAND = re.compile(r"^/[a-z-]+$")


def _known_paths() -> set[str]:
    app = create_app(settings=Settings(_env_file=None, admin_password="test-password"))
    return set(app.openapi()["paths"])


def _documented() -> set[str]:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("### Admin API") : readme.index("### Что смотреть")]
    return set(re.findall(r"`(?:[A-Z]+ )?(/[a-z0-9/{}_-]+)`", section))


def test_every_documented_endpoint_exists() -> None:
    known = _known_paths()
    unknown = sorted(
        path
        for path in _documented()
        if path != "/api/admin"
        and not SHORTHAND.match(path)
        and path not in known
        and f"/api/admin{path}" not in known
    )
    assert not unknown, "В README есть адреса, которых нет в API: " + ", ".join(unknown)


def test_public_endpoints_are_documented() -> None:
    """Публичных всего три, и каждый должен быть назван."""

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for path in ("/health", "/ready", "/telegram/webhook"):
        assert f"`{path}`" in readme, f"{path} не описан в README"


def test_readme_names_the_schema_endpoints() -> None:
    """Живая схема — единственный источник, который не устаревает."""

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for path in ("/docs", "/openapi.json"):
        assert path in readme
