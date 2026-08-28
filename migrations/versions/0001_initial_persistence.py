"""Initial PostgreSQL persistence schema.

Revision ID: 0001
Revises:
Created: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
EMPTY_JSON = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "items",
        sa.Column("id", UUID, nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("normalized_title", sa.Text(), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("doi", sa.String(length=255), nullable=True),
        sa.Column("arxiv_id", sa.String(length=64), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("publication_date", sa.Date(), nullable=True),
        sa.Column("venue", sa.Text(), nullable=True),
        sa.Column("citation_count", sa.Integer(), nullable=True),
        sa.Column("semantic_score", sa.Float(), nullable=True),
        sa.Column("ranking", JSONB, nullable=True),
        sa.Column("rank_total_score", sa.Float(), nullable=True),
        sa.Column("deep_analysis", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('paper', 'method', 'software', 'dataset')",
            name="ck_items_kind",
        ),
        sa.CheckConstraint(
            "rank_total_score IS NULL OR rank_total_score BETWEEN 0 AND 10",
            name="ck_items_rank_total_score_range",
        ),
        sa.CheckConstraint(
            "semantic_score IS NULL OR semantic_score BETWEEN -1 AND 1",
            name="ck_items_semantic_score_range",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_items"),
    )
    op.create_index(
        "ix_items_digest_ranking",
        "items",
        [sa.text("rank_total_score DESC")],
        unique=False,
        postgresql_where=sa.text("rank_total_score IS NOT NULL"),
    )
    op.create_index("ix_items_publication_date", "items", ["publication_date"], unique=False)
    op.create_index(
        "uq_items_arxiv_id",
        "items",
        ["arxiv_id"],
        unique=True,
        postgresql_where=sa.text("arxiv_id IS NOT NULL"),
    )
    op.create_index(
        "uq_items_doi",
        "items",
        ["doi"],
        unique=True,
        postgresql_where=sa.text("doi IS NOT NULL"),
    )
    op.create_index("uq_items_normalized_title", "items", ["normalized_title"], unique=True)

    op.create_table(
        "authors",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("orcid", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_authors"),
    )
    op.create_index("uq_authors_normalized_name", "authors", ["normalized_name"], unique=True)
    op.create_index(
        "uq_authors_orcid",
        "authors",
        ["orcid"],
        unique=True,
        postgresql_where=sa.text("orcid IS NOT NULL"),
    )

    op.create_table(
        "topics",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_topics"),
        sa.UniqueConstraint("normalized_name", name="uq_topics_normalized_name"),
    )
    op.create_table(
        "repositories",
        sa.Column("id", UUID, nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("external_id", sa.String(length=512), nullable=True),
        sa.Column("details", JSONB, server_default=EMPTY_JSON, nullable=False),
        sa.CheckConstraint(
            "source IS NULL OR source IN "
            "('arxiv', 'openalex', 'crossref', 'semantic_scholar', 'github', 'huggingface')",
            name="ck_repositories_source",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_repositories"),
        sa.UniqueConstraint("url", name="uq_repositories_url"),
    )
    op.create_table(
        "datasets",
        sa.Column("id", UUID, nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("external_id", sa.String(length=512), nullable=True),
        sa.Column("details", JSONB, server_default=EMPTY_JSON, nullable=False),
        sa.CheckConstraint(
            "source IS NULL OR source IN "
            "('arxiv', 'openalex', 'crossref', 'semantic_scholar', 'github', 'huggingface')",
            name="ck_datasets_source",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_datasets"),
        sa.UniqueConstraint("url", name="uq_datasets_url"),
    )
    op.create_table(
        "users",
        sa.Column("id", UUID, nullable=False),
        sa.Column("external_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("external_user_id", name="uq_users_external_user_id"),
    )
    op.create_table(
        "collection_runs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor", JSONB, server_default=EMPTY_JSON, nullable=False),
        sa.Column("statistics", JSONB, server_default=EMPTY_JSON, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_collection_runs_finish_after_start",
        ),
        sa.CheckConstraint(
            "source IN "
            "('arxiv', 'openalex', 'crossref', 'semantic_scholar', 'github', 'huggingface')",
            name="ck_collection_runs_source",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')",
            name="ck_collection_runs_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_collection_runs"),
    )
    op.create_index(
        "ix_collection_runs_source_started_at",
        "collection_runs",
        ["source", "started_at"],
        unique=False,
    )
    op.create_index(
        "uq_collection_runs_running_source",
        "collection_runs",
        ["source"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "item_sources",
        sa.Column("id", UUID, nullable=False),
        sa.Column("item_id", UUID, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("raw_payload", JSONB, server_default=EMPTY_JSON, nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN "
            "('arxiv', 'openalex', 'crossref', 'semantic_scholar', 'github', 'huggingface')",
            name="ck_item_sources_source",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name="fk_item_sources_item_id_items", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_item_sources"),
        sa.UniqueConstraint("source", "external_id", name="uq_item_sources_source_external_id"),
    )
    op.create_index("ix_item_sources_item_id", "item_sources", ["item_id"], unique=False)

    op.create_table(
        "item_authors",
        sa.Column("item_id", UUID, nullable=False),
        sa.Column("author_id", UUID, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_item_authors_position_non_negative"),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["authors.id"],
            name="fk_item_authors_author_id_authors",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_item_authors_item_id_items",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("item_id", "author_id", name="pk_item_authors"),
        sa.UniqueConstraint("item_id", "position", name="uq_item_authors_item_id_position"),
    )
    op.create_index("ix_item_authors_author_id", "item_authors", ["author_id"], unique=False)
    op.create_table(
        "item_topics",
        sa.Column("item_id", UUID, nullable=False),
        sa.Column("topic_id", UUID, nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_item_topics_item_id_items",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name="fk_item_topics_topic_id_topics",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("item_id", "topic_id", name="pk_item_topics"),
    )
    op.create_index("ix_item_topics_topic_id", "item_topics", ["topic_id"], unique=False)
    op.create_table(
        "item_repositories",
        sa.Column("item_id", UUID, nullable=False),
        sa.Column("repository_id", UUID, nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_item_repositories_item_id_items",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name="fk_item_repositories_repository_id_repositories",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("item_id", "repository_id", name="pk_item_repositories"),
    )
    op.create_index(
        "ix_item_repositories_repository_id",
        "item_repositories",
        ["repository_id"],
        unique=False,
    )
    op.create_table(
        "item_datasets",
        sa.Column("item_id", UUID, nullable=False),
        sa.Column("dataset_id", UUID, nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="fk_item_datasets_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_item_datasets_item_id_items",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("item_id", "dataset_id", name="pk_item_datasets"),
    )
    op.create_index("ix_item_datasets_dataset_id", "item_datasets", ["dataset_id"], unique=False)
    op.create_table(
        "user_interests",
        sa.Column("id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("topic_id", UUID, nullable=True),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(topic_id IS NOT NULL AND query IS NULL) OR (topic_id IS NULL AND query IS NOT NULL)",
            name="ck_user_interests_single_interest_target",
        ),
        sa.CheckConstraint("weight > 0 AND weight <= 10", name="ck_user_interests_weight_range"),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name="fk_user_interests_topic_id_topics",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_interests_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_interests"),
        sa.UniqueConstraint("user_id", "query", name="uq_user_interests_user_id_query"),
        sa.UniqueConstraint("user_id", "topic_id", name="uq_user_interests_user_id_topic_id"),
    )
    op.create_index("ix_user_interests_topic_id", "user_interests", ["topic_id"], unique=False)
    op.create_table(
        "feedback",
        sa.Column("id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("item_id", UUID, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("context", JSONB, server_default=EMPTY_JSON, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('very_interesting', 'useful', 'not_interesting', 'save', 'deeper')",
            name="ck_feedback_kind",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_feedback_item_id_items",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_feedback_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_feedback"),
    )
    op.create_index("ix_feedback_item_id", "feedback", ["item_id"], unique=False)
    op.create_index(
        "ix_feedback_user_id_created_at",
        "feedback",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "digests",
        sa.Column("id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("payload", JSONB, server_default=EMPTY_JSON, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("period_end > period_start", name="ck_digests_period"),
        sa.CheckConstraint(
            "status IN ('pending', 'building', 'ready', 'sent', 'failed')",
            name="ck_digests_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_digests_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_digests"),
    )
    op.create_index(
        "ix_digests_status_created_at", "digests", ["status", "created_at"], unique=False
    )
    op.create_table(
        "digest_items",
        sa.Column("digest_id", UUID, nullable=False),
        sa.Column("item_id", UUID, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("score_snapshot", sa.Float(), nullable=True),
        sa.Column("explanation", JSONB, server_default=EMPTY_JSON, nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_digest_items_position_non_negative"),
        sa.ForeignKeyConstraint(
            ["digest_id"],
            ["digests.id"],
            name="fk_digest_items_digest_id_digests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_digest_items_item_id_items",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("digest_id", "item_id", name="pk_digest_items"),
        sa.UniqueConstraint("digest_id", "position", name="uq_digest_items_digest_id_position"),
    )
    op.create_index("ix_digest_items_item_id", "digest_items", ["item_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_digest_items_item_id", table_name="digest_items")
    op.drop_table("digest_items")
    op.drop_index("ix_digests_status_created_at", table_name="digests")
    op.drop_table("digests")
    op.drop_index("ix_feedback_user_id_created_at", table_name="feedback")
    op.drop_index("ix_feedback_item_id", table_name="feedback")
    op.drop_table("feedback")
    op.drop_index("ix_user_interests_topic_id", table_name="user_interests")
    op.drop_table("user_interests")
    op.drop_index("ix_item_datasets_dataset_id", table_name="item_datasets")
    op.drop_table("item_datasets")
    op.drop_index("ix_item_repositories_repository_id", table_name="item_repositories")
    op.drop_table("item_repositories")
    op.drop_index("ix_item_topics_topic_id", table_name="item_topics")
    op.drop_table("item_topics")
    op.drop_index("ix_item_authors_author_id", table_name="item_authors")
    op.drop_table("item_authors")
    op.drop_index("ix_item_sources_item_id", table_name="item_sources")
    op.drop_table("item_sources")
    op.drop_index("uq_collection_runs_running_source", table_name="collection_runs")
    op.drop_index("ix_collection_runs_source_started_at", table_name="collection_runs")
    op.drop_table("collection_runs")
    op.drop_table("users")
    op.drop_table("datasets")
    op.drop_table("repositories")
    op.drop_table("topics")
    op.drop_index("uq_authors_orcid", table_name="authors")
    op.drop_index("uq_authors_normalized_name", table_name="authors")
    op.drop_table("authors")
    op.drop_index("uq_items_normalized_title", table_name="items")
    op.drop_index("uq_items_doi", table_name="items")
    op.drop_index("uq_items_arxiv_id", table_name="items")
    op.drop_index("ix_items_publication_date", table_name="items")
    op.drop_index("ix_items_digest_ranking", table_name="items")
    op.drop_table("items")
