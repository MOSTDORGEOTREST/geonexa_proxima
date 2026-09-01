"""Грани профиля: несколько векторов на профиль и отметка о попадании.

Профиль из нескольких тем даёт вектор-центроид между ними, и статья, глубоко
попадающая в одну тему, получает средний косинус — центроид оттянут остальными
темами. Поэтому у профиля теперь набор векторов: грань 0 — весь профиль, дальше
его отдельные темы, по которым ищут независимо.

Revision ID: 0006
Revises: 0005
Created: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Снять первичный ключ, не зная его имени.
#:
#: Таблицу создавала миграция 0005, и как именно назвался её PK, зависит от
#: того, добралось ли до неё соглашение об именах из `Base.metadata`:
#: `pk_profile_vectors` или дефолтный `profile_vectors_pkey`. Гадать нельзя —
#: миграция должна пройти на базе, поднятой любой из прошлых версий.
_DROP_PRIMARY_KEY = """
DO $$
DECLARE constraint_name text;
BEGIN
    SELECT conname INTO constraint_name
      FROM pg_constraint
     WHERE conrelid = 'profile_vectors'::regclass AND contype = 'p';
    IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE profile_vectors DROP CONSTRAINT %I', constraint_name);
    END IF;
END $$;
"""


def upgrade() -> None:
    # Существующие строки — это векторы всего профиля, то есть грань 0.
    # server_default оставляем: он же делает миграцию безопасной для строк,
    # которые пишутся конкурентно во время её применения.
    op.add_column(
        "profile_vectors",
        sa.Column("facet", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint("facet_non_negative", "profile_vectors", "facet >= 0")

    op.execute(_DROP_PRIMARY_KEY)
    op.create_primary_key(
        "pk_profile_vectors", "profile_vectors", ["profile_id", "version", "facet"]
    )

    # Чем именно материал попал в выдачу. Текстом, а не номером грани: номера
    # меняются при правке описания, и ответ на «почему это показали» после
    # первой же правки стал бы враньём.
    op.add_column("profile_item_scores", sa.Column("matched_facet", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("profile_item_scores", "matched_facet")
    # Лишние грани уносим до сужения ключа: иначе на них падает создание PK.
    op.execute("DELETE FROM profile_vectors WHERE facet <> 0")
    op.execute(_DROP_PRIMARY_KEY)
    op.create_primary_key("pk_profile_vectors", "profile_vectors", ["profile_id", "version"])
    op.drop_constraint("facet_non_negative", "profile_vectors", type_="check")
    op.drop_column("profile_vectors", "facet")
