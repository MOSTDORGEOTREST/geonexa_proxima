"""Российские источники и двуязычный профиль интересов.

Два новых источника сбора — КиберЛенинка и любой архив по OAI-PMH — не
проходят CHECK-ограничения на имя источника: они перечисляют допустимые
значения буквально, и добавить источник без миграции нельзя. Ограничения
пересобираются с расширенным списком в каждой таблице, где есть колонка
``source`` с этим словарём.

Профиль интересов получает английскую сторону: перевод описания, сделанный
LLM, и отпечаток исходника, по которому видно, что перевод отстал от правки.

Revision ID: 0009
Revises: 0008
Created: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'arxiv', 'openalex', 'crossref', 'semantic_scholar', 'github', 'huggingface'"
_NEW = _OLD + ", 'cyberleninka', 'oai'"

#: таблица → допускает ли колонка NULL. Имена ограничений — по соглашению
#: ``ck_<таблица>_source`` из ``db/base.py``.
_TABLES: tuple[tuple[str, bool], ...] = (
    ("item_sources", False),
    ("repositories", True),
    ("datasets", True),
    ("harvest_queries", False),
    ("harvest_decisions", False),
    ("metrics_harvest_daily", False),
)


def _rebuild(values: str) -> None:
    for table, nullable in _TABLES:
        name = f"ck_{table}_source"
        condition = f"source IN ({values})"
        if nullable:
            condition = f"source IS NULL OR {condition}"
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.create_check_constraint("source", table, condition)


def upgrade() -> None:
    _rebuild(_NEW)
    op.add_column("subscriber_profiles", sa.Column("description_en", sa.Text(), nullable=True))
    op.add_column(
        "subscriber_profiles",
        sa.Column("translation_source_hash", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriber_profiles", "translation_source_hash")
    op.drop_column("subscriber_profiles", "description_en")
    # Строки новых источников нарушили бы прежнее ограничение — убираем их
    # первыми, иначе ALTER откажет.
    op.execute("DELETE FROM item_sources WHERE source IN ('cyberleninka', 'oai')")
    op.execute("DELETE FROM harvest_queries WHERE source IN ('cyberleninka', 'oai')")
    op.execute("DELETE FROM harvest_decisions WHERE source IN ('cyberleninka', 'oai')")
    op.execute("DELETE FROM metrics_harvest_daily WHERE source IN ('cyberleninka', 'oai')")
    op.execute("UPDATE repositories SET source = NULL WHERE source IN ('cyberleninka', 'oai')")
    op.execute("UPDATE datasets SET source = NULL WHERE source IN ('cyberleninka', 'oai')")
    _rebuild(_OLD)
