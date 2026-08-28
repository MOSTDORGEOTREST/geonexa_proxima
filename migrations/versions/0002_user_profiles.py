"""Add user profiles and profile-scoped personalization.

Revision ID: 0002
Revises: 0001
Created: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
EMPTY_JSON = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.add_column("users", sa.Column("telegram_username", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("display_name", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("language_code", sa.String(length=16), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "status",
        "users",
        "status IN ('active', 'inactive', 'blocked')",
    )
    op.create_index("ix_users_status", "users", ["status"], unique=False)

    op.create_table(
        "user_profiles",
        sa.Column("id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("compiled_text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("digest_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("digest_settings", JSONB, server_default=EMPTY_JSON, nullable=False),
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
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_profiles_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_profiles"),
        sa.UniqueConstraint(
            "user_id",
            "normalized_name",
            name="uq_user_profiles_user_id_normalized_name",
        ),
    )
    op.create_index("ix_user_profiles_user_id", "user_profiles", ["user_id"], unique=False)
    op.create_index(
        "uq_user_profiles_active_user",
        "user_profiles",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "ix_user_profiles_digest_enabled",
        "user_profiles",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("digest_enabled"),
    )

    # Reusing the user UUID is safe across tables and avoids relying on pgcrypto.
    op.execute(
        """
        INSERT INTO user_profiles (
            id, user_id, name, normalized_name, compiled_text, version, is_active,
            digest_enabled, digest_settings, created_at, updated_at
        )
        SELECT
            id, id, 'Default', 'default', '', 1, true, false, '{}'::jsonb,
            created_at, now()
        FROM users
        """
    )

    op.create_table(
        "profile_interests",
        sa.Column("id", UUID, nullable=False),
        sa.Column("profile_id", UUID, nullable=False),
        sa.Column("topic_id", UUID, nullable=True),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column(
            "polarity",
            sa.String(length=16),
            server_default=sa.text("'positive'"),
            nullable=False,
        ),
        sa.Column("weight", sa.Float(), nullable=False),
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
            "(topic_id IS NOT NULL AND query IS NULL) OR (topic_id IS NULL AND query IS NOT NULL)",
            name="single_interest_target",
        ),
        sa.CheckConstraint(
            "polarity IN ('positive', 'negative')",
            name="polarity",
        ),
        sa.CheckConstraint(
            "weight >= 0 AND weight <= 10",
            name="weight_range",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["user_profiles.id"],
            name="fk_profile_interests_profile_id_user_profiles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name="fk_profile_interests_topic_id_topics",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_profile_interests"),
        sa.UniqueConstraint(
            "profile_id",
            "query",
            name="uq_profile_interests_profile_id_query",
        ),
        sa.UniqueConstraint(
            "profile_id",
            "topic_id",
            name="uq_profile_interests_profile_id_topic_id",
        ),
    )
    op.create_index(
        "ix_profile_interests_profile_id",
        "profile_interests",
        ["profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_profile_interests_topic_id",
        "profile_interests",
        ["topic_id"],
        unique=False,
    )
    op.execute(
        """
        INSERT INTO profile_interests (
            id, profile_id, topic_id, query, polarity, weight, created_at, updated_at
        )
        SELECT
            ui.id, up.id, ui.topic_id, ui.query, 'positive', ui.weight,
            ui.created_at, now()
        FROM user_interests AS ui
        JOIN user_profiles AS up ON up.user_id = ui.user_id AND up.is_active
        """
    )

    op.create_table(
        "profile_item_scores",
        sa.Column("id", UUID, nullable=False),
        sa.Column("profile_id", UUID, nullable=False),
        sa.Column("item_id", UUID, nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("semantic_score", sa.Float(), nullable=False),
        sa.Column("reranker_score", sa.Float(), nullable=False),
        sa.Column("global_score", sa.Float(), nullable=False),
        sa.Column("interest_score", sa.Float(), nullable=False),
        sa.Column("personal_score", sa.Float(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
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
            "profile_version >= 1",
            name="profile_version_positive",
        ),
        sa.CheckConstraint(
            "semantic_score BETWEEN 0 AND 1",
            name="semantic_score_range",
        ),
        sa.CheckConstraint(
            "reranker_score BETWEEN 0 AND 1",
            name="reranker_score_range",
        ),
        sa.CheckConstraint(
            "global_score BETWEEN 0 AND 1",
            name="global_score_range",
        ),
        sa.CheckConstraint(
            "interest_score BETWEEN 0 AND 1",
            name="interest_score_range",
        ),
        sa.CheckConstraint(
            "personal_score BETWEEN 0 AND 1",
            name="personal_score_range",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_profile_item_scores_item_id_items",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["user_profiles.id"],
            name="fk_profile_item_scores_profile_id_user_profiles",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_profile_item_scores"),
        sa.UniqueConstraint(
            "profile_id",
            "item_id",
            "profile_version",
            name="uq_profile_item_scores_profile_id_item_id_profile_version",
        ),
    )
    op.create_index(
        "ix_profile_item_scores_profile_ranking",
        "profile_item_scores",
        ["profile_id", sa.text("personal_score DESC")],
        unique=False,
    )
    op.create_index(
        "ix_profile_item_scores_item_id",
        "profile_item_scores",
        ["item_id"],
        unique=False,
    )

    op.add_column("feedback", sa.Column("profile_id", UUID, nullable=True))
    op.create_foreign_key(
        "fk_feedback_profile_id_user_profiles",
        "feedback",
        "user_profiles",
        ["profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_feedback_profile_id_created_at",
        "feedback",
        ["profile_id", "created_at"],
        unique=False,
    )
    op.execute(
        """
        UPDATE feedback AS f
        SET profile_id = up.id
        FROM user_profiles AS up
        WHERE up.user_id = f.user_id AND up.is_active
        """
    )

    op.add_column("digests", sa.Column("profile_id", UUID, nullable=True))
    op.create_foreign_key(
        "fk_digests_profile_id_user_profiles",
        "digests",
        "user_profiles",
        ["profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_digests_profile_id_created_at",
        "digests",
        ["profile_id", "created_at"],
        unique=False,
    )
    op.execute(
        """
        UPDATE digests AS d
        SET profile_id = up.id
        FROM user_profiles AS up
        WHERE up.user_id = d.user_id AND up.is_active
        """
    )

    op.create_table(
        "profile_interest_signals",
        sa.Column("id", UUID, nullable=False),
        sa.Column("profile_id", UUID, nullable=False),
        sa.Column("topic_id", UUID, nullable=True),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("polarity", sa.String(length=16), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column(
            "source",
            sa.String(length=24),
            server_default=sa.text("'feedback'"),
            nullable=False,
        ),
        sa.Column("source_feedback_id", UUID, nullable=True),
        sa.Column("evidence_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("details", JSONB, server_default=EMPTY_JSON, nullable=False),
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
            "(topic_id IS NOT NULL AND query IS NULL) OR (topic_id IS NULL AND query IS NOT NULL)",
            name="single_signal_target",
        ),
        sa.CheckConstraint(
            "polarity IN ('positive', 'negative')",
            name="polarity",
        ),
        sa.CheckConstraint(
            "source IN ('feedback', 'system')",
            name="source",
        ),
        sa.CheckConstraint(
            "weight >= 0 AND weight <= 10",
            name="weight_range",
        ),
        sa.CheckConstraint(
            "evidence_count >= 1",
            name="evidence_count_positive",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["user_profiles.id"],
            name="fk_profile_interest_signals_profile_id_user_profiles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_feedback_id"],
            ["feedback.id"],
            name="fk_profile_interest_signals_source_feedback_id_feedback",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name="fk_profile_interest_signals_topic_id_topics",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_profile_interest_signals"),
        sa.UniqueConstraint(
            "profile_id",
            "query",
            name="uq_profile_interest_signals_profile_id_query",
        ),
        sa.UniqueConstraint(
            "profile_id",
            "topic_id",
            name="uq_profile_interest_signals_profile_id_topic_id",
        ),
    )
    op.create_index(
        "ix_profile_interest_signals_profile_id",
        "profile_interest_signals",
        ["profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_profile_interest_signals_topic_id",
        "profile_interest_signals",
        ["topic_id"],
        unique=False,
    )
    op.create_index(
        "ix_profile_interest_signals_source_feedback_id",
        "profile_interest_signals",
        ["source_feedback_id"],
        unique=False,
    )

    op.drop_index("ix_user_interests_topic_id", table_name="user_interests")
    op.drop_table("user_interests")


def downgrade() -> None:
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
            name="single_interest_target",
        ),
        sa.CheckConstraint(
            "weight > 0 AND weight <= 10",
            name="weight_range",
        ),
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
        sa.UniqueConstraint(
            "user_id",
            "query",
            name="uq_user_interests_user_id_query",
        ),
        sa.UniqueConstraint(
            "user_id",
            "topic_id",
            name="uq_user_interests_user_id_topic_id",
        ),
    )
    op.create_index(
        "ix_user_interests_topic_id",
        "user_interests",
        ["topic_id"],
        unique=False,
    )
    op.execute(
        """
        INSERT INTO user_interests (id, user_id, topic_id, query, weight, created_at)
        SELECT DISTINCT ON (up.user_id, pi.topic_id)
            pi.id, up.user_id, pi.topic_id, NULL, pi.weight, pi.created_at
        FROM profile_interests AS pi
        JOIN user_profiles AS up ON up.id = pi.profile_id
        WHERE up.is_active
          AND pi.polarity = 'positive'
          AND pi.topic_id IS NOT NULL
          AND pi.weight > 0
        ORDER BY up.user_id, pi.topic_id, pi.updated_at DESC, pi.id
        """
    )
    op.execute(
        """
        INSERT INTO user_interests (id, user_id, topic_id, query, weight, created_at)
        SELECT DISTINCT ON (up.user_id, pi.query)
            pi.id, up.user_id, NULL, pi.query, pi.weight, pi.created_at
        FROM profile_interests AS pi
        JOIN user_profiles AS up ON up.id = pi.profile_id
        WHERE up.is_active
          AND pi.polarity = 'positive'
          AND pi.query IS NOT NULL
          AND pi.weight > 0
        ORDER BY up.user_id, pi.query, pi.updated_at DESC, pi.id
        """
    )

    op.drop_index(
        "ix_profile_interest_signals_source_feedback_id",
        table_name="profile_interest_signals",
    )
    op.drop_index(
        "ix_profile_interest_signals_topic_id",
        table_name="profile_interest_signals",
    )
    op.drop_index(
        "ix_profile_interest_signals_profile_id",
        table_name="profile_interest_signals",
    )
    op.drop_table("profile_interest_signals")

    op.drop_index("ix_digests_profile_id_created_at", table_name="digests")
    op.drop_constraint(
        "fk_digests_profile_id_user_profiles",
        "digests",
        type_="foreignkey",
    )
    op.drop_column("digests", "profile_id")

    op.drop_index("ix_feedback_profile_id_created_at", table_name="feedback")
    op.drop_constraint(
        "fk_feedback_profile_id_user_profiles",
        "feedback",
        type_="foreignkey",
    )
    op.drop_column("feedback", "profile_id")

    op.drop_index("ix_profile_item_scores_item_id", table_name="profile_item_scores")
    op.drop_index(
        "ix_profile_item_scores_profile_ranking",
        table_name="profile_item_scores",
    )
    op.drop_table("profile_item_scores")

    op.drop_index("ix_profile_interests_topic_id", table_name="profile_interests")
    op.drop_index("ix_profile_interests_profile_id", table_name="profile_interests")
    op.drop_table("profile_interests")

    op.drop_index("ix_user_profiles_digest_enabled", table_name="user_profiles")
    op.drop_index("uq_user_profiles_active_user", table_name="user_profiles")
    op.drop_index("ix_user_profiles_user_id", table_name="user_profiles")
    op.drop_table("user_profiles")

    op.drop_index("ix_users_status", table_name="users")
    op.drop_constraint(op.f("ck_users_status"), "users", type_="check")
    op.drop_column("users", "updated_at")
    op.drop_column("users", "last_seen_at")
    op.drop_column("users", "status")
    op.drop_column("users", "language_code")
    op.drop_column("users", "display_name")
    op.drop_column("users", "telegram_username")
