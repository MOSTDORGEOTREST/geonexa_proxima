"""Приведение схемы к текущей ревизии при старте контейнера.

Контейнер поднимается против трёх разных состояний базы, и путать их нельзя:

* **пустая** — накатываем миграции с нуля;
* **отстающая** — накатываем недостающие ревизии;
* **актуальная** — не делаем ничего.

Схема создаётся именно миграциями, а не ``metadata.create_all``: часть объектов
(exclusion-ограничение подписок, индексы pgvector с operator class, починка
имён из 0001) описана только в SQL, и ``create_all`` тихо создал бы базу, в
которой этих гарантий нет.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True, slots=True)
class SchemaState:
    is_empty: bool
    current_revision: str | None
    head_revision: str | None
    tables: int

    @property
    def is_current(self) -> bool:
        return (
            not self.is_empty
            and self.current_revision is not None
            and self.current_revision == self.head_revision
        )

    @property
    def summary(self) -> str:
        if self.is_empty:
            return "база пустая"
        if self.current_revision is None:
            return f"{self.tables} таблиц без alembic_version"
        if self.is_current:
            return f"{self.tables} таблиц, ревизия {self.current_revision} — актуальна"
        return f"{self.tables} таблиц, ревизия {self.current_revision} → {self.head_revision}"


def alembic_config(project_root: Path | None = None) -> Config:
    root = project_root or Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    return config


def head_revision(project_root: Path | None = None) -> str | None:
    script = ScriptDirectory.from_config(alembic_config(project_root))
    return script.get_current_head()


async def inspect_schema(engine: AsyncEngine, project_root: Path | None = None) -> SchemaState:
    """Понять, в каком состоянии база, не меняя её."""

    async with engine.connect() as connection:
        tables = int(
            await connection.scalar(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
            )
            or 0
        )
        has_alembic = bool(
            await connection.scalar(
                text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            )
        )
        current = None
        if has_alembic:
            current = await connection.run_sync(
                lambda sync_connection: MigrationContext.configure(
                    sync_connection
                ).get_current_revision()
            )
    return SchemaState(
        is_empty=tables == 0,
        current_revision=current,
        head_revision=head_revision(project_root),
        tables=tables,
    )


def _upgrade_sync(project_root: Path | None) -> None:
    command.upgrade(alembic_config(project_root), "head")


async def upgrade_to_head(project_root: Path | None = None) -> None:
    """Накатить миграции. Alembic синхронный, поэтому уводим его в отдельный поток."""

    await asyncio.to_thread(_upgrade_sync, project_root)


async def ensure_schema(
    engine: AsyncEngine,
    *,
    project_root: Path | None = None,
    allow_upgrade: bool = True,
) -> SchemaState:
    """Привести схему к head и вернуть состояние ДО изменений.

    ``allow_upgrade=False`` превращает функцию в проверку: удобно для
    production, где миграции катят отдельным шагом деплоя, а сервис должен
    только убедиться, что база готова, и внятно отказаться, если нет.
    """

    state = await inspect_schema(engine, project_root)
    if state.is_current:
        return state
    if not allow_upgrade:
        raise RuntimeError(
            f"Схема не соответствует коду ({state.summary}). "
            f"Запусти alembic upgrade head или включи DB_AUTO_MIGRATE."
        )
    await upgrade_to_head(project_root)
    return state
