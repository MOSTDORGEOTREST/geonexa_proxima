"""Platform v2: harvest scope, subscribers, subscriptions, delivery, LLM registry, admin.

Revision ID: 0003
Revises: 0002
Created: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
EMPTY_JSON = sa.text("'{}'::jsonb")
NOW = sa.text("now()")

SUBSCRIBER_KINDS = "'user', 'group', 'channel'"
SUBSCRIBER_STATUSES = "'pending', 'active', 'paused', 'blocked', 'left'"
SOURCE_NAMES = "'arxiv', 'openalex', 'crossref', 'semantic_scholar', 'github', 'huggingface'"
GROUP_MODES = "'any_of', 'all_of', 'none_of'"
MATCH_TYPES = "'phrase', 'token', 'prefix', 'regex'"
RUN_STATUSES = "'running', 'succeeded', 'failed', 'cancelled'"
DECISIONS = "'accepted', 'borderline', 'rejected', 'duplicate'"
GATE_STAGES = "'keyword', 'semantic', 'llm', 'dedup'"
SUBSCRIPTION_STATUSES = "'pending', 'trial', 'active', 'expired', 'cancelled'"
BOT_STATUSES = "'creator', 'administrator', 'member', 'restricted', 'left', 'kicked'"
DELIVERY_STATUSES = "'queued', 'claimed', 'sending', 'sent', 'failed', 'skipped', 'cancelled'"
MESSAGE_STATUSES = "'sent', 'failed', 'skipped', 'deleted', 'edited'"
SCHEDULE_KINDS = (
    "'global_harvest', 'digest_dispatch', 'subscriber_digest', "
    "'delivery_personal', 'delivery_group', 'chat_monitor', 'maintenance'"
)
LLM_PROTOCOLS = "'openai_compatible', 'anthropic', 'custom'"
REASONING_STYLES = "'none', 'openai_effort', 'anthropic_effort', 'thinking_budget'"
REASONING_LEVELS = "'none', 'low', 'high', 'max'"
LLM_ROLES = (
    "'ranker', 'explainer', 'profile_compiler', 'query_expander', "
    "'digest_writer', 'analyzer', 'deep_dive', 'chat'"
)
VALUE_TYPES = "'string', 'int', 'float', 'bool', 'json', 'secret'"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=NOW,
            onupdate=NOW,
        ),
    ]


def _normalize_legacy_constraint_names() -> None:
    """Убрать двойной префикс имён CHECK, появившийся в миграции 0001.

    Там имена уже содержали префикс, поверх которого правило именования
    добавило ещё один: получилось ck_digests_ck_digests_status. ORM-модели
    ожидают короткое имя, поэтому приводим базу именно к нему.
    """

    op.execute(
        # Строка сырая: \_ — экранирование подчёркивания для SQL LIKE, а не
        # управляющая последовательность Python.
        r"""
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN
                SELECT c.conrelid::regclass::text AS tbl, c.conname
                FROM pg_constraint c
                WHERE c.contype = 'c'
                  AND c.conname LIKE 'ck\_' || c.conrelid::regclass::text
                                   || '\_ck\_' || c.conrelid::regclass::text || '\_%'
            LOOP
                EXECUTE format(
                    'ALTER TABLE %s RENAME CONSTRAINT %I TO %I',
                    r.tbl,
                    r.conname,
                    substr(r.conname, length('ck_' || r.tbl || '_') + 1)
                );
            END LOOP;
        END $$;
        """
    )


def _rename_constraint(table: str, old: str, new: str) -> None:
    """Переименовать ограничение, если оно есть: RENAME TABLE их не трогает."""

    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{old}' AND conrelid = '{table}'::regclass
            ) THEN
                EXECUTE 'ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new}';
            END IF;
        END $$;
        """
    )


def _rename_index(old: str, new: str) -> None:
    op.execute(f"ALTER INDEX IF EXISTS {old} RENAME TO {new}")


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------
def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    _normalize_legacy_constraint_names()

    _rename_users_to_subscribers()
    _rename_profiles()
    _extend_items()
    _create_harvest()
    _create_subscriptions()
    _create_chats()
    _create_schedules_and_runs()
    _create_delivery()
    _create_llm_registry()
    _create_settings_and_audit()
    op.drop_table("collection_runs")


# --- subscribers ------------------------------------------------------------
def _rename_users_to_subscribers() -> None:
    op.rename_table("users", "subscribers")
    op.alter_column("subscribers", "external_user_id", new_column_name="telegram_chat_id")
    op.alter_column("subscribers", "display_name", new_column_name="title")

    # RENAME TABLE не переименовывает ограничения и индексы — делаем это явно,
    # иначе autogenerate будет вечно предлагать их пересоздать.
    _rename_constraint("subscribers", "pk_users", "pk_subscribers")
    _rename_constraint(
        "subscribers", "uq_users_external_user_id", "uq_subscribers_telegram_chat_id"
    )
    _rename_index("ix_users_status", "ix_subscribers_status")

    op.add_column("subscribers", sa.Column("kind", sa.String(16), nullable=True))
    op.add_column("subscribers", sa.Column("telegram_user_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "subscribers",
        sa.Column("is_owner", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("subscribers", sa.Column("added_by_subscriber_id", UUID, nullable=True))
    op.add_column(
        "subscribers",
        sa.Column("timezone", sa.Text(), nullable=False, server_default=sa.text("'Europe/Moscow'")),
    )
    op.add_column("subscribers", sa.Column("notes", sa.Text(), nullable=True))
    op.add_column(
        "subscribers",
        sa.Column("meta", JSONB, nullable=False, server_default=EMPTY_JSON),
    )
    op.add_column(
        "subscribers",
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )

    # Старый CHECK допускает только active/inactive/blocked — снимаем ДО UPDATE.
    op.execute("ALTER TABLE subscribers DROP CONSTRAINT ck_users_status")
    # Отрицательный chat_id — всегда группа/канал; в v1-базе таких строк нет,
    # но так повторный upgrade после downgrade остаётся корректным.
    op.execute(
        "UPDATE subscribers SET "
        "kind = CASE WHEN telegram_chat_id > 0 THEN 'user' ELSE 'group' END, "
        "telegram_user_id = CASE WHEN telegram_chat_id > 0 THEN telegram_chat_id END"
    )
    op.execute("UPDATE subscribers SET status = 'paused' WHERE status = 'inactive'")
    op.alter_column("subscribers", "kind", nullable=False)

    op.create_check_constraint("kind", "subscribers", f"kind IN ({SUBSCRIBER_KINDS})")
    op.create_check_constraint("status", "subscribers", f"status IN ({SUBSCRIBER_STATUSES})")
    op.create_check_constraint(
        "chat_id_sign",
        "subscribers",
        "(kind = 'user' AND telegram_chat_id > 0) OR (kind <> 'user')",
    )
    op.create_foreign_key(
        "fk_subscribers_added_by_subscriber_id_subscribers",
        "subscribers",
        "subscribers",
        ["added_by_subscriber_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_subscribers_kind_status", "subscribers", ["kind", "status"])
    op.create_index(
        "ix_subscribers_active",
        "subscribers",
        ["status"],
        postgresql_where=sa.text("status = 'active'"),
    )


def _rename_profiles() -> None:
    op.rename_table("user_profiles", "subscriber_profiles")
    op.alter_column("subscriber_profiles", "user_id", new_column_name="subscriber_id")

    _rename_constraint("subscriber_profiles", "pk_user_profiles", "pk_subscriber_profiles")
    _rename_constraint(
        "subscriber_profiles",
        "uq_user_profiles_user_id_normalized_name",
        "uq_subscriber_profiles_subscriber_id_normalized_name",
    )
    _rename_constraint(
        "subscriber_profiles",
        "fk_user_profiles_user_id_users",
        "fk_subscriber_profiles_subscriber_id_subscribers",
    )
    _rename_constraint(
        "subscriber_profiles",
        "ck_user_profiles_version_positive",
        "ck_subscriber_profiles_version_positive",
    )
    _rename_index("ix_user_profiles_user_id", "ix_subscriber_profiles_subscriber_id")
    _rename_index("uq_user_profiles_active_user", "uq_subscriber_profiles_active_subscriber")
    _rename_index("ix_user_profiles_digest_enabled", "ix_subscriber_profiles_digest_enabled")
    for table in ("profile_interests", "profile_interest_signals", "profile_item_scores"):
        _rename_constraint(
            table,
            f"fk_{table}_profile_id_user_profiles",
            f"fk_{table}_profile_id_subscriber_profiles",
        )
    _rename_constraint(
        "digests",
        "fk_digests_profile_id_user_profiles",
        "fk_digests_profile_id_subscriber_profiles",
    )
    _rename_constraint(
        "feedback",
        "fk_feedback_profile_id_user_profiles",
        "fk_feedback_profile_id_subscriber_profiles",
    )

    op.add_column("subscriber_profiles", sa.Column("schedule_id", UUID, nullable=True))
    op.add_column("subscriber_profiles", sa.Column("timezone", sa.Text(), nullable=True))
    op.add_column(
        "subscriber_profiles",
        sa.Column(
            "delivery_format", sa.String(32), nullable=False, server_default=sa.text("'cards'")
        ),
    )
    op.add_column(
        "subscriber_profiles",
        sa.Column("max_items", sa.Integer(), nullable=False, server_default=sa.text("20")),
    )
    op.add_column(
        "subscriber_profiles",
        sa.Column("min_personal_score", sa.Float(), nullable=False, server_default=sa.text("0.5")),
    )
    op.add_column("subscriber_profiles", sa.Column("min_global_score", sa.Float(), nullable=True))
    op.add_column(
        "subscriber_profiles",
        sa.Column(
            "kinds",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{paper,method,software,dataset}'::text[]"),
        ),
    )
    op.add_column(
        "subscriber_profiles",
        sa.Column("quiet_hours", JSONB, nullable=False, server_default=EMPTY_JSON),
    )
    op.add_column(
        "subscriber_profiles",
        sa.Column("last_digest_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriber_profiles",
        sa.Column("next_digest_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriber_profiles", sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "delivery_format",
        "subscriber_profiles",
        "delivery_format IN ('cards', 'compact', 'single_message', 'digest_post')",
    )
    op.create_check_constraint(
        "max_items_range", "subscriber_profiles", "max_items BETWEEN 1 AND 100"
    )
    op.create_check_constraint(
        "min_personal_score_range",
        "subscriber_profiles",
        "min_personal_score BETWEEN 0 AND 1",
    )
    op.create_index(
        "ix_subscriber_profiles_next_digest",
        "subscriber_profiles",
        ["next_digest_at"],
        postgresql_where=sa.text("digest_enabled"),
    )

    for table in ("digests", "feedback"):
        op.alter_column(table, "user_id", new_column_name="subscriber_id")
        _rename_constraint(
            table, f"fk_{table}_user_id_users", f"fk_{table}_subscriber_id_subscribers"
        )
    _rename_index("ix_feedback_user_id_created_at", "ix_feedback_subscriber_id_created_at")


def _extend_items() -> None:
    op.add_column("items", sa.Column("keyword_score", sa.Float(), nullable=True))
    op.add_column(
        "items", sa.Column("matched_terms", JSONB, nullable=False, server_default=EMPTY_JSON)
    )
    op.add_column("items", sa.Column("harvest_profile_id", UUID, nullable=True))
    op.add_column("items", sa.Column("gate_stage", sa.String(16), nullable=True))
    op.add_column("items", sa.Column("language", sa.String(8), nullable=True))
    op.add_column(
        "items",
        sa.Column("is_preprint", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("items", sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("items", sa.Column("content_hash", sa.String(64), nullable=True))
    op.create_index("ix_items_content_hash", "items", ["content_hash"])
    op.create_index(
        "ix_items_keyword_score",
        "items",
        [sa.text("keyword_score DESC")],
        postgresql_where=sa.text("keyword_score IS NOT NULL"),
    )


# --- harvest ----------------------------------------------------------------
def _create_harvest() -> None:
    op.create_table(
        "harvest_profiles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("key", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("satisfy_expr", sa.Text(), nullable=False),
        sa.Column(
            "keyword_score_threshold", sa.Float(), nullable=False, server_default=sa.text("0.35")
        ),
        sa.Column(
            "borderline_semantic_threshold",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.52"),
        ),
        sa.Column(
            "languages",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{en,ru}'::text[]"),
        ),
        sa.Column(
            "item_kinds",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{paper,method,software,dataset}'::text[]"),
        ),
        sa.Column("config", JSONB, nullable=False, server_default=EMPTY_JSON),
        *_timestamps(),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.CheckConstraint(
            "keyword_score_threshold BETWEEN 0 AND 1", name="keyword_threshold_range"
        ),
    )
    op.create_index(
        "uq_harvest_profiles_active",
        "harvest_profiles",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_foreign_key(
        "fk_items_harvest_profile",
        "items",
        "harvest_profiles",
        ["harvest_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "harvest_term_groups",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "harvest_profile_id",
            UUID,
            sa.ForeignKey("harvest_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("min_matches", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "fields",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{title,abstract,keywords}'::text[]"),
        ),
        sa.Column("weight", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_hard", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("penalty", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("affects_satisfy", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("comment", sa.Text()),
        *_timestamps(),
        sa.UniqueConstraint("harvest_profile_id", "key", name="uq_harvest_group_key"),
        sa.CheckConstraint(f"mode IN ({GROUP_MODES})", name="mode"),
        sa.CheckConstraint("min_matches >= 0", name="min_matches_non_negative"),
        sa.CheckConstraint("weight BETWEEN 0 AND 1", name="weight_range"),
    )

    op.create_table(
        "harvest_terms",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "group_id",
            UUID,
            sa.ForeignKey("harvest_term_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("normalized_term", sa.Text(), nullable=False),
        sa.Column("match_type", sa.String(16), nullable=False),
        sa.Column("lang", sa.String(8)),
        sa.Column("weight", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("hit_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_hit_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint("group_id", "normalized_term", "match_type", name="uq_harvest_term"),
        sa.CheckConstraint(f"match_type IN ({MATCH_TYPES})", name="match_type"),
        sa.CheckConstraint("weight >= 0 AND weight <= 10", name="weight_range"),
    )
    op.create_index(
        "ix_harvest_terms_enabled",
        "harvest_terms",
        ["group_id"],
        postgresql_where=sa.text("enabled"),
    )

    op.create_table(
        "harvest_queries",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "harvest_profile_id",
            UUID,
            sa.ForeignKey("harvest_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("params", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column("max_items", sa.Integer(), nullable=False, server_default=sa.text("200")),
        sa.Column("lookback_hours", sa.Integer()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_stats", JSONB, nullable=False, server_default=EMPTY_JSON),
        *_timestamps(),
        sa.UniqueConstraint("harvest_profile_id", "source", "key", name="uq_harvest_query"),
        sa.CheckConstraint(f"source IN ({SOURCE_NAMES})", name="source"),
        sa.CheckConstraint("max_items BETWEEN 1 AND 5000", name="max_items_range"),
    )
    op.create_index(
        "ix_harvest_queries_enabled",
        "harvest_queries",
        ["source", "priority"],
        postgresql_where=sa.text("enabled"),
    )

    op.create_table(
        "source_cursors",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "harvest_query_id",
            UUID,
            sa.ForeignKey("harvest_queries.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("cursor", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("last_external_id", sa.Text()),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )

    op.create_table(
        "harvest_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "harvest_profile_id",
            UUID,
            sa.ForeignKey("harvest_profiles.id", ondelete="SET NULL"),
        ),
        sa.Column("prefect_flow_run_id", sa.Text()),
        sa.Column("trigger", sa.String(16), nullable=False, server_default=sa.text("'schedule'")),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'running'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("since", sa.DateTime(timezone=True)),
        sa.Column("stats", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("error", sa.Text()),
        sa.Column("triggered_by", sa.Text()),
        sa.CheckConstraint(f"status IN ({RUN_STATUSES})", name="status"),
        sa.CheckConstraint("trigger IN ('schedule', 'manual', 'api', 'backfill')", name="trigger"),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finish_after_start"
        ),
    )
    op.create_index(
        "ix_harvest_runs_status_started",
        "harvest_runs",
        ["status", sa.text("started_at DESC")],
    )
    op.create_index(
        "uq_harvest_runs_running",
        "harvest_runs",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "harvest_decisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "harvest_run_id",
            UUID,
            sa.ForeignKey("harvest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(512), nullable=False),
        sa.Column("item_id", UUID, sa.ForeignKey("items.id", ondelete="SET NULL")),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("keyword_score", sa.Float()),
        sa.Column("semantic_score", sa.Float()),
        sa.Column("matched_terms", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("blocked_by", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(f"stage IN ({GATE_STAGES})", name="stage"),
        sa.CheckConstraint(f"decision IN ({DECISIONS})", name="decision"),
        sa.CheckConstraint(f"source IN ({SOURCE_NAMES})", name="source"),
    )
    op.create_index("ix_harvest_decisions_run", "harvest_decisions", ["harvest_run_id", "decision"])
    op.create_index(
        "ix_harvest_decisions_created",
        "harvest_decisions",
        ["decision", sa.text("created_at DESC")],
    )


# --- subscriptions ----------------------------------------------------------
def _create_subscriptions() -> None:
    op.create_table(
        "subscription_plans",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("key", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("max_profiles", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "max_items_per_digest", sa.Integer(), nullable=False, server_default=sa.text("20")
        ),
        sa.Column(
            "min_interval_hours", sa.Integer(), nullable=False, server_default=sa.text("168")
        ),
        sa.Column(
            "deep_analysis_quota_per_month",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "allow_group_chats", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("features", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamps(),
        sa.CheckConstraint("max_profiles >= 1", name="max_profiles_positive"),
        sa.CheckConstraint("min_interval_hours >= 1", name="min_interval_positive"),
    )
    op.create_index(
        "uq_subscription_plans_default",
        "subscription_plans",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "subscriber_id",
            UUID,
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            UUID,
            sa.ForeignKey("subscription_plans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("grace_until", sa.DateTime(timezone=True)),
        sa.Column("auto_renew", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("source", sa.String(16), nullable=False, server_default=sa.text("'admin'")),
        sa.Column("price_amount", sa.Numeric(12, 2)),
        sa.Column("price_currency", sa.String(8)),
        sa.Column("external_payment_id", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by", sa.Text()),
        *_timestamps(),
        sa.CheckConstraint(f"status IN ({SUBSCRIPTION_STATUSES})", name="status"),
        sa.CheckConstraint("source IN ('admin', 'trial', 'import', 'payment')", name="source"),
        sa.CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="period"),
    )
    op.create_index("ix_subscriptions_subscriber", "subscriptions", ["subscriber_id", "status"])
    op.create_index(
        "ix_subscriptions_expiring",
        "subscriptions",
        ["ends_at"],
        postgresql_where=sa.text("status IN ('active', 'trial')"),
    )
    # Одна действующая подписка на подписчика: пересечение периодов запрещено БД.
    op.execute(
        """
        ALTER TABLE subscriptions ADD CONSTRAINT ex_subscriptions_no_overlap
        EXCLUDE USING gist (
            subscriber_id WITH =,
            tstzrange(starts_at, coalesce(ends_at, 'infinity'::timestamptz)) WITH &&
        ) WHERE (status IN ('active', 'trial'))
        """
    )

    op.create_table(
        "subscription_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "subscription_id",
            UUID,
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event", sa.String(24), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("actor", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "event IN ('created', 'activated', 'extended', 'downgraded', 'upgraded', "
            "'expired', 'cancelled', 'reminded')",
            name="event",
        ),
    )
    op.create_index(
        "ix_subscription_events_subscription",
        "subscription_events",
        ["subscription_id", sa.text("created_at DESC")],
    )


# --- chats ------------------------------------------------------------------
def _create_chats() -> None:
    op.create_table(
        "chat_memberships",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "subscriber_id",
            UUID,
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("bot_status", sa.String(24), nullable=False),
        sa.Column("can_post_messages", sa.Boolean()),
        sa.Column("can_edit_messages", sa.Boolean()),
        sa.Column("can_delete_messages", sa.Boolean()),
        sa.Column("member_count", sa.Integer()),
        sa.Column("chat_type", sa.String(24)),
        sa.Column("invite_link", sa.Text()),
        sa.Column("added_by_user_id", sa.BigInteger()),
        sa.Column("added_at", sa.DateTime(timezone=True)),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        *_timestamps(),
        sa.CheckConstraint(f"bot_status IN ({BOT_STATUSES})", name="bot_status"),
    )
    op.create_index("ix_chat_memberships_status", "chat_memberships", ["bot_status"])

    op.create_table(
        "chat_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "subscriber_id",
            UUID,
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("old_value", JSONB),
        sa.Column("new_value", JSONB),
        sa.Column("raw_update", JSONB),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_index(
        "ix_chat_events_subscriber",
        "chat_events",
        ["subscriber_id", sa.text("occurred_at DESC")],
    )


# --- schedules / flow runs --------------------------------------------------
def _create_schedules_and_runs() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("key", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "subscriber_profile_id",
            UUID,
            sa.ForeignKey("subscriber_profiles.id", ondelete="CASCADE"),
        ),
        sa.Column("prefect_deployment_id", sa.Text()),
        sa.Column("prefect_schedule_id", sa.Text()),
        sa.Column("cron", sa.Text()),
        sa.Column("interval_seconds", sa.Integer()),
        sa.Column("timezone", sa.Text(), nullable=False, server_default=sa.text("'Europe/Moscow'")),
        sa.Column("anchor_date", sa.DateTime(timezone=True)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("parameters", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("sync_pending", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint(f"kind IN ({SCHEDULE_KINDS})", name="kind"),
        sa.CheckConstraint(
            "cron IS NOT NULL OR interval_seconds IS NOT NULL", name="schedule_defined"
        ),
        sa.CheckConstraint(
            "interval_seconds IS NULL OR interval_seconds >= 60", name="interval_min"
        ),
    )
    op.create_index("ix_schedules_kind_enabled", "schedules", ["kind", "enabled"])
    op.create_index(
        "ix_schedules_next_run",
        "schedules",
        ["next_run_at"],
        postgresql_where=sa.text("enabled"),
    )
    op.create_foreign_key(
        "fk_subscriber_profiles_schedule",
        "subscriber_profiles",
        "schedules",
        ["schedule_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "flow_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("prefect_flow_run_id", sa.Text(), nullable=False, unique=True),
        sa.Column("flow_name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(32)),
        sa.Column("schedule_id", UUID, sa.ForeignKey("schedules.id", ondelete="SET NULL")),
        sa.Column("subscriber_id", UUID, sa.ForeignKey("subscribers.id", ondelete="SET NULL")),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_seconds", sa.Float()),
        sa.Column("stats", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("error", sa.Text()),
        *_timestamps(),
    )
    op.create_index("ix_flow_runs_kind_started", "flow_runs", ["kind", sa.text("started_at DESC")])
    op.create_index("ix_flow_runs_state", "flow_runs", ["state"])


# --- delivery ---------------------------------------------------------------
def _create_delivery() -> None:
    op.add_column("digests", sa.Column("kind", sa.String(16), nullable=True))
    op.execute("UPDATE digests SET kind = 'personal'")
    op.alter_column("digests", "kind", nullable=False, server_default=sa.text("'personal'"))
    op.add_column("digests", sa.Column("schedule_id", UUID, nullable=True))
    op.add_column("digests", sa.Column("prefect_flow_run_id", sa.Text(), nullable=True))
    op.add_column(
        "digests",
        sa.Column("item_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("digests", sa.Column("error", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_digests_schedule", "digests", "schedules", ["schedule_id"], ["id"], ondelete="SET NULL"
    )
    op.create_check_constraint("kind", "digests", "kind IN ('personal', 'group', 'broadcast')")
    op.drop_constraint("status", "digests", type_="check")
    op.create_check_constraint(
        "status",
        "digests",
        "status IN ('pending', 'building', 'ready', 'queued', 'sent', 'partial', "
        "'failed', 'skipped')",
    )

    op.add_column("digest_items", sa.Column("personal_score", sa.Float(), nullable=True))
    op.add_column("digest_items", sa.Column("global_score", sa.Float(), nullable=True))
    op.add_column("digest_items", sa.Column("profile_score_id", UUID, nullable=True))
    op.create_foreign_key(
        "fk_digest_items_profile_score",
        "digest_items",
        "profile_item_scores",
        ["profile_score_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "delivery_jobs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "digest_id", UUID, sa.ForeignKey("digests.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "subscriber_id",
            UUID,
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("target_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_by", sa.Text()),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("payload", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("prefect_flow_run_id", sa.Text()),
        *_timestamps(),
        sa.UniqueConstraint("digest_id", "target_chat_id", name="uq_delivery_job_target"),
        sa.CheckConstraint("channel IN ('personal', 'group')", name="channel"),
        sa.CheckConstraint(f"status IN ({DELIVERY_STATUSES})", name="status"),
        sa.CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )
    op.create_index(
        "ix_delivery_jobs_queue",
        "delivery_jobs",
        ["channel", "scheduled_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_delivery_jobs_subscriber",
        "delivery_jobs",
        ["subscriber_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "delivery_messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "delivery_job_id",
            UUID,
            sa.ForeignKey("delivery_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_id", UUID, sa.ForeignKey("items.id", ondelete="SET NULL")),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("error_code", sa.Integer()),
        sa.Column("error", sa.Text()),
        sa.Column("retry_after", sa.Integer()),
        sa.Column("text_preview", sa.Text()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(f"status IN ({MESSAGE_STATUSES})", name="status"),
    )
    op.create_index("ix_delivery_messages_job", "delivery_messages", ["delivery_job_id"])
    op.create_index(
        "ix_delivery_messages_chat", "delivery_messages", ["chat_id", sa.text("sent_at DESC")]
    )
    op.create_index(
        "ix_delivery_messages_status", "delivery_messages", ["status", sa.text("sent_at DESC")]
    )


# --- LLM registry -----------------------------------------------------------
def _create_llm_registry() -> None:
    op.create_table(
        "llm_providers",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("key", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("protocol", sa.String(24), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("api_key_encrypted", sa.LargeBinary()),
        sa.Column("api_key_env_var", sa.Text()),
        sa.Column("default_headers", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "is_managed_by_env", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        *_timestamps(),
        sa.CheckConstraint(f"protocol IN ({LLM_PROTOCOLS})", name="protocol"),
    )

    op.create_table(
        "llm_models",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "provider_id",
            UUID,
            sa.ForeignKey("llm_providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text()),
        sa.Column("tier", sa.String(16)),
        sa.Column(
            "supports_reasoning", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "reasoning_style", sa.String(24), nullable=False, server_default=sa.text("'none'")
        ),
        sa.Column(
            "reasoning_levels",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{low,high,max}'::text[]"),
        ),
        sa.Column(
            "supports_json_mode", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("supports_tools", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("context_window", sa.Integer()),
        sa.Column("max_output_tokens", sa.Integer()),
        sa.Column("input_price_per_1m", sa.Numeric(10, 4)),
        sa.Column("output_price_per_1m", sa.Numeric(10, 4)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamps(),
        sa.UniqueConstraint("provider_id", "key", name="uq_llm_model_key"),
        sa.CheckConstraint("tier IS NULL OR tier IN ('light', 'heavy', 'both')", name="tier"),
        sa.CheckConstraint(f"reasoning_style IN ({REASONING_STYLES})", name="reasoning_style"),
    )

    op.create_table(
        "llm_role_bindings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("role", sa.String(32), nullable=False, unique=True),
        sa.Column(
            "model_id",
            UUID,
            sa.ForeignKey("llm_models.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("fallback_model_id", UUID, sa.ForeignKey("llm_models.id", ondelete="SET NULL")),
        sa.Column("temperature", sa.Float(), nullable=False, server_default=sa.text("0.1")),
        sa.Column("top_p", sa.Float()),
        sa.Column("max_tokens", sa.Integer()),
        sa.Column("reasoning_effort", sa.String(16)),
        sa.Column("json_mode", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default=sa.text("180")),
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default=sa.text("4")),
        sa.Column("system_prompt_override", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("updated_by", sa.Text()),
        *_timestamps(),
        sa.CheckConstraint(f"role IN ({LLM_ROLES})", name="role"),
        sa.CheckConstraint("temperature BETWEEN 0 AND 2", name="temperature_range"),
        sa.CheckConstraint(
            f"reasoning_effort IS NULL OR reasoning_effort IN ({REASONING_LEVELS})",
            name="reasoning_effort",
        ),
        sa.CheckConstraint("concurrency BETWEEN 1 AND 64", name="concurrency_range"),
    )

    op.create_table(
        "llm_call_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("model_id", UUID, sa.ForeignKey("llm_models.id", ondelete="SET NULL")),
        sa.Column("item_id", UUID, sa.ForeignKey("items.id", ondelete="SET NULL")),
        sa.Column("subscriber_id", UUID, sa.ForeignKey("subscribers.id", ondelete="SET NULL")),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column("reasoning_tokens", sa.Integer()),
        sa.Column("cost_usd", sa.Numeric(12, 6)),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("status IN ('ok', 'error', 'timeout', 'rate_limited')", name="status"),
    )
    op.create_index("ix_llm_call_log_role", "llm_call_log", ["role", sa.text("created_at DESC")])
    op.create_index("ix_llm_call_log_created", "llm_call_log", ["created_at"])

    op.create_table(
        "llm_usage_daily",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("model_id", UUID, sa.ForeignKey("llm_models.id", ondelete="SET NULL")),
        sa.Column("calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "completion_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("errors", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("day", "role", "model_id", name="uq_llm_usage_daily"),
    )


# --- settings / audit -------------------------------------------------------
def _create_settings_and_audit() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("value_type", sa.String(16), nullable=False),
        sa.Column("env_default", JSONB),
        sa.Column("scope", sa.String(24), nullable=False, server_default=sa.text("'general'")),
        sa.Column("description", sa.Text()),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_env_only", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_by", sa.Text()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=NOW,
            onupdate=NOW,
        ),
        sa.CheckConstraint(f"value_type IN ({VALUE_TYPES})", name="value_type"),
    )
    op.create_index("ix_app_settings_scope", "app_settings", ["scope"])

    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text()),
        sa.Column("before", JSONB),
        sa.Column("after", JSONB),
        sa.Column("ip", postgresql.INET()),
        sa.Column("user_agent", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_index("ix_admin_audit_created", "admin_audit_log", [sa.text("created_at DESC")])
    op.create_index("ix_admin_audit_entity", "admin_audit_log", ["entity_type", "entity_id"])


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------
def downgrade() -> None:
    for table in (
        "admin_audit_log",
        "app_settings",
        "llm_usage_daily",
        "llm_call_log",
        "llm_role_bindings",
        "llm_models",
        "llm_providers",
        "delivery_messages",
        "delivery_jobs",
        "chat_events",
        "chat_memberships",
        "subscription_events",
        "subscriptions",
        "subscription_plans",
        "harvest_decisions",
        "harvest_runs",
        "source_cursors",
        "harvest_queries",
        "harvest_terms",
        "harvest_term_groups",
    ):
        op.drop_table(table)

    op.execute("ALTER TABLE digest_items DROP CONSTRAINT fk_digest_items_profile_score")
    for column in ("profile_score_id", "global_score", "personal_score"):
        op.drop_column("digest_items", column)

    op.execute("ALTER TABLE digests DROP CONSTRAINT fk_digests_schedule")
    op.drop_constraint("kind", "digests", type_="check")
    op.drop_constraint("status", "digests", type_="check")
    op.create_check_constraint(
        "status",
        "digests",
        "status IN ('pending', 'building', 'ready', 'sent', 'failed')",
    )
    for column in ("error", "item_count", "prefect_flow_run_id", "schedule_id", "kind"):
        op.drop_column("digests", column)

    op.execute("ALTER TABLE subscriber_profiles DROP CONSTRAINT fk_subscriber_profiles_schedule")
    op.drop_table("flow_runs")
    op.drop_table("schedules")

    op.execute("ALTER TABLE items DROP CONSTRAINT fk_items_harvest_profile")
    op.drop_table("harvest_profiles")
    op.drop_index("ix_items_keyword_score", table_name="items")
    op.drop_index("ix_items_content_hash", table_name="items")
    for column in (
        "content_hash",
        "retracted_at",
        "is_preprint",
        "language",
        "gate_stage",
        "harvest_profile_id",
        "matched_terms",
        "keyword_score",
    ):
        op.drop_column("items", column)

    _rename_index("ix_feedback_subscriber_id_created_at", "ix_feedback_user_id_created_at")
    for table in ("digests", "feedback"):
        _rename_constraint(
            table, f"fk_{table}_subscriber_id_subscribers", f"fk_{table}_user_id_users"
        )
        op.alter_column(table, "subscriber_id", new_column_name="user_id")

    op.drop_index("ix_subscriber_profiles_next_digest", table_name="subscriber_profiles")
    for name in ("delivery_format", "max_items_range", "min_personal_score_range"):
        op.drop_constraint(name, "subscriber_profiles", type_="check")
    for column in (
        "paused_until",
        "next_digest_at",
        "last_digest_at",
        "quiet_hours",
        "kinds",
        "min_global_score",
        "min_personal_score",
        "max_items",
        "delivery_format",
        "timezone",
        "schedule_id",
    ):
        op.drop_column("subscriber_profiles", column)
    for table in (
        "profile_interests",
        "profile_interest_signals",
        "profile_item_scores",
        "digests",
        "feedback",
    ):
        _rename_constraint(
            table,
            f"fk_{table}_profile_id_subscriber_profiles",
            f"fk_{table}_profile_id_user_profiles",
        )
    _rename_constraint("subscriber_profiles", "pk_subscriber_profiles", "pk_user_profiles")
    _rename_constraint(
        "subscriber_profiles",
        "uq_subscriber_profiles_subscriber_id_normalized_name",
        "uq_user_profiles_user_id_normalized_name",
    )
    _rename_constraint(
        "subscriber_profiles",
        "fk_subscriber_profiles_subscriber_id_subscribers",
        "fk_user_profiles_user_id_users",
    )
    _rename_index("ix_subscriber_profiles_subscriber_id", "ix_user_profiles_user_id")
    _rename_index("uq_subscriber_profiles_active_subscriber", "uq_user_profiles_active_user")
    _rename_index("ix_subscriber_profiles_digest_enabled", "ix_user_profiles_digest_enabled")
    op.alter_column("subscriber_profiles", "subscriber_id", new_column_name="user_id")
    op.rename_table("subscriber_profiles", "user_profiles")

    op.drop_index("ix_subscribers_active", table_name="subscribers")
    op.drop_index("ix_subscribers_kind_status", table_name="subscribers")
    op.execute(
        "ALTER TABLE subscribers DROP CONSTRAINT fk_subscribers_added_by_subscriber_id_subscribers"
    )
    for name in ("chat_id_sign", "status", "kind"):
        op.drop_constraint(name, "subscribers", type_="check")
    op.execute(
        "UPDATE subscribers SET status = 'inactive' WHERE status IN ('paused', 'pending', 'left')"
    )
    for column in (
        "first_seen_at",
        "meta",
        "notes",
        "timezone",
        "added_by_subscriber_id",
        "is_owner",
        "telegram_user_id",
        "kind",
    ):
        op.drop_column("subscribers", column)
    op.alter_column("subscribers", "title", new_column_name="display_name")
    op.alter_column("subscribers", "telegram_chat_id", new_column_name="external_user_id")
    _rename_constraint("subscribers", "pk_subscribers", "pk_users")
    _rename_constraint(
        "subscribers", "uq_subscribers_telegram_chat_id", "uq_users_external_user_id"
    )
    _rename_index("ix_subscribers_status", "ix_users_status")
    op.rename_table("subscribers", "users")
    op.create_check_constraint("status", "users", "status IN ('active', 'inactive', 'blocked')")

    op.create_table(
        "collection_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default=sa.text("'running'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("since", sa.DateTime(timezone=True)),
        sa.Column("cursor", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("statistics", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint(f"source IN ({SOURCE_NAMES})", name="source"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')", name="status"
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finish_after_start"
        ),
    )
    op.create_index(
        "ix_collection_runs_source_started_at", "collection_runs", ["source", "started_at"]
    )
    op.create_index(
        "uq_collection_runs_running_source",
        "collection_runs",
        ["source"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
