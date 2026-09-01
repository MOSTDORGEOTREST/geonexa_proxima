"""Отпечаток текста грани в кэше векторов и уборка старых версий.

Ключ кэша — (профиль, версия, номер грани), но номер грани позиционный: какой
именно текст стоит под номером 2, зависит ещё и от `PROFILE_FACET_MIN_CHARS`,
`PROFILE_FACET_LIMIT` и самого алгоритма разбиения. Версия профиля отслеживает
только `compiled_text` и про эти три вещи ничего не знает.

Поэтому смена настройки или правка разбиения молча оставляли под старым номером
чужой вектор: поиск шёл по прошлому тексту, а реранкер и объяснение — по новому.
Отпечаток закрывает дыру: не совпал — считаем промахом кэша и пересчитываем.

Revision ID: 0007
Revises: 0006
Created: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "profile_vectors",
        sa.Column("text_hash", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    # Существующие строки посчитаны неизвестно из какого текста: пустой
    # отпечаток не совпадёт ни с чем, и они честно пересчитаются при первом
    # обращении. Выбрасывать их сразу незачем — уборка ниже сделает это сама.
    op.create_index(
        "ix_profile_vectors_profile_id",
        "profile_vectors",
        ["profile_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_profile_vectors_profile_id", table_name="profile_vectors")
    op.drop_column("profile_vectors", "text_hash")
