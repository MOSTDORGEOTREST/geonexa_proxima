"""pgvector: векторы материалов и профилей рядом с корпусом.

Revision ID: 0005
Revises: 0004
Created: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from geonexa_proxima.config import (
    PGVECTOR_INDEX_LIMITS,
    VectorColumnType,
    VectorIndexKind,
    get_settings,
)
from geonexa_proxima.vector.types import Vector

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
NOW = sa.text("now()")

settings = get_settings()
DIMENSIONS = settings.embedding_dimensions
COLUMN_TYPE = settings.vector_column_type
INDEX_KIND = settings.vector_index_kind
EMBEDDING = Vector(DIMENSIONS, COLUMN_TYPE.value)
OPS = "vector_cosine_ops" if COLUMN_TYPE is VectorColumnType.VECTOR else "halfvec_cosine_ops"


def _guard() -> None:
    """Проверить окружение до того, как упадёт CREATE INDEX."""

    if INDEX_KIND is not VectorIndexKind.NONE:
        limit = PGVECTOR_INDEX_LIMITS[(COLUMN_TYPE, INDEX_KIND)]
        if DIMENSIONS > limit:
            raise RuntimeError(
                f"{DIMENSIONS} измерений не индексируются как {INDEX_KIND.value} "
                f"на {COLUMN_TYPE.value} (потолок {limit})"
            )
    if COLUMN_TYPE is VectorColumnType.HALFVEC:
        version = op.get_bind().scalar(
            sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        if version and tuple(int(p) for p in str(version).split(".")[:2]) < (0, 7):
            raise RuntimeError(
                f"halfvec появился в pgvector 0.7, а установлена {version}: "
                f"обнови расширение или используй VECTOR_COLUMN_TYPE=vector"
            )


def _index(table: str) -> None:
    if INDEX_KIND is VectorIndexKind.NONE:
        return
    name = f"ix_{table}_embedding_{INDEX_KIND.value}"
    if INDEX_KIND is VectorIndexKind.HNSW:
        options = (
            f"m = {settings.vector_hnsw_m}, "
            f"ef_construction = {settings.vector_hnsw_ef_construction}"
        )
    else:
        options = f"lists = {settings.vector_ivfflat_lists}"
    op.execute(
        f"CREATE INDEX {name} ON {table} USING {INDEX_KIND.value} "
        f"(embedding {OPS}) WITH ({options})"
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    _guard()

    op.create_table(
        "item_vectors",
        sa.Column(
            "item_id",
            UUID,
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("embedding", EMBEDDING, nullable=False),
        sa.Column("model", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("dimensions > 0", name="dimensions_positive"),
    )
    op.create_index("ix_item_vectors_model", "item_vectors", ["model"])
    _index("item_vectors")

    op.create_table(
        "profile_vectors",
        sa.Column(
            "profile_id",
            UUID,
            sa.ForeignKey("subscriber_profiles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("embedding", EMBEDDING, nullable=False),
        sa.Column("model", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("version >= 1", name="version_positive"),
    )
    _index("profile_vectors")

    # Что именно лежит в колонках — чтобы расхождение с настройками было видно
    # запросом, а не по странной выдаче поиска.
    op.execute(
        sa.text(
            "INSERT INTO app_settings (key, value, value_type, scope, description, updated_by) "
            "VALUES ('VECTOR_SCHEMA', :value, 'json', 'general', "
            "'Размерность и тип колонок pgvector на момент миграции', 'migration:0005') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        ).bindparams(
            sa.bindparam(
                "value",
                value={
                    "dimensions": DIMENSIONS,
                    "column_type": COLUMN_TYPE.value,
                    "index_kind": INDEX_KIND.value,
                    "model": settings.embedding_model,
                },
                type_=postgresql.JSONB(),
            )
        )
    )


def downgrade() -> None:
    op.execute("DELETE FROM app_settings WHERE key = 'VECTOR_SCHEMA'")
    op.drop_table("profile_vectors")
    op.drop_table("item_vectors")
