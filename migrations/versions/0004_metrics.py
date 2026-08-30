"""Metrics: subscriber activity log, daily rollups, retention cohorts.

Revision ID: 0004
Revises: 0003
Created: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
EMPTY_JSON = sa.text("'{}'::jsonb")
NOW = sa.text("now()")
ZERO = sa.text("0")

ACTIVITY_KINDS = (
    "'registered', 'command', 'search', 'feedback', 'digest_received', "
    "'link_click', 'profile_edit', 'deep_dive', 'subscription_changed', "
    "'blocked_bot', 'chat_joined', 'chat_left'"
)
SUBSCRIBER_KINDS = "'user', 'group', 'channel'"
SOURCE_NAMES = "'arxiv', 'openalex', 'crossref', 'semantic_scholar', 'github', 'huggingface'"


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default=ZERO)


def upgrade() -> None:
    _create_activity()
    _create_daily_rollups()
    _create_retention()
    _create_rollup_runs()


# --- сырьё активности -------------------------------------------------------
def _create_activity() -> None:
    """Без событийного лога не посчитать ни DAU, ни удержание когорт."""

    op.create_table(
        "subscriber_activity",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "subscriber_id",
            UUID,
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "profile_id",
            UUID,
            sa.ForeignKey("subscriber_profiles.id", ondelete="SET NULL"),
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("item_id", UUID, sa.ForeignKey("items.id", ondelete="SET NULL")),
        sa.Column("digest_id", UUID, sa.ForeignKey("digests.id", ondelete="SET NULL")),
        sa.Column("payload", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(f"kind IN ({ACTIVITY_KINDS})", name="kind"),
    )
    op.create_index(
        "ix_subscriber_activity_subscriber",
        "subscriber_activity",
        ["subscriber_id", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_subscriber_activity_kind",
        "subscriber_activity",
        ["kind", sa.text("occurred_at DESC")],
    )
    # Роллап всегда идёт диапазоном дат и считает уникальных подписчиков.
    # Составной индекс закрывает и range scan, и distinct без похода в таблицу.
    # Индекс по выражению-дате намеренно не заводим: день считается в
    # METRICS_TIMEZONE — настраиваемой зоне, так что при её смене такой индекс
    # молча перестал бы использоваться.
    op.create_index(
        "ix_subscriber_activity_occurred_subscriber",
        "subscriber_activity",
        ["occurred_at", "subscriber_id"],
    )


# --- суточные агрегаты ------------------------------------------------------
def _create_daily_rollups() -> None:
    op.create_table(
        "metrics_harvest_daily",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        _counter("fetched"),
        _counter("accepted"),
        _counter("borderline"),
        _counter("rejected"),
        _counter("duplicates"),
        _counter("rescued_by_semantic"),
        _counter("ranked"),
        _counter("analyzed"),
        _counter("stored"),
        sa.Column("avg_keyword_score", sa.Float()),
        sa.Column("avg_global_score", sa.Float()),
        sa.Column("top_blocked_by", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("day", "source", name="uq_metrics_harvest_daily"),
        sa.CheckConstraint(f"source IN ({SOURCE_NAMES})", name="source"),
    )
    op.create_index("ix_metrics_harvest_daily_day", "metrics_harvest_daily", [sa.text("day DESC")])

    op.create_table(
        "metrics_subscribers_daily",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        _counter("registered"),
        _counter("activated"),
        _counter("churned"),
        _counter("blocked"),
        _counter("total"),
        _counter("total_active"),
        _counter("with_subscription"),
        _counter("dau"),
        _counter("wau"),
        _counter("mau"),
        _counter("digest_enabled_profiles"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("day", "kind", name="uq_metrics_subscribers_daily"),
        sa.CheckConstraint(f"kind IN ({SUBSCRIBER_KINDS})", name="kind"),
    )
    op.create_index(
        "ix_metrics_subscribers_daily_day", "metrics_subscribers_daily", [sa.text("day DESC")]
    )

    op.create_table(
        "metrics_delivery_daily",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        _counter("jobs_created"),
        _counter("jobs_sent"),
        _counter("jobs_failed"),
        _counter("jobs_skipped"),
        _counter("messages_sent"),
        _counter("messages_failed"),
        _counter("rate_limited"),
        _counter("recipients"),
        sa.Column("avg_queue_seconds", sa.Float()),
        sa.Column("p95_queue_seconds", sa.Float()),
        sa.Column("top_errors", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("day", "channel", name="uq_metrics_delivery_daily"),
        sa.CheckConstraint("channel IN ('personal', 'group')", name="channel"),
    )
    op.create_index(
        "ix_metrics_delivery_daily_day", "metrics_delivery_daily", [sa.text("day DESC")]
    )

    op.create_table(
        "metrics_engagement_daily",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        _counter("digests_sent"),
        _counter("items_delivered"),
        _counter("feedback_total"),
        _counter("feedback_very_interesting"),
        _counter("feedback_useful"),
        _counter("feedback_not_interesting"),
        _counter("feedback_saved"),
        _counter("feedback_deeper"),
        _counter("unique_reactors"),
        _counter("empty_digests"),
        sa.Column("engagement_rate", sa.Float()),
        sa.Column("avg_items_per_digest", sa.Float()),
        sa.Column("avg_personal_score", sa.Float()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("day", name="uq_metrics_engagement_daily"),
        sa.CheckConstraint(
            "engagement_rate IS NULL OR engagement_rate BETWEEN 0 AND 1",
            name="engagement_rate_range",
        ),
    )


# --- когорты ----------------------------------------------------------------
def _create_retention() -> None:
    op.create_table(
        "metrics_retention",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("cohort_week", sa.Date(), nullable=False),
        sa.Column("week_offset", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        _counter("cohort_size"),
        _counter("retained"),
        sa.Column("retention_rate", sa.Float()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("cohort_week", "week_offset", "kind", name="uq_metrics_retention"),
        sa.CheckConstraint("week_offset >= 0", name="week_offset_non_negative"),
        sa.CheckConstraint("retained <= cohort_size", name="retained_within_cohort"),
        sa.CheckConstraint(f"kind IN ({SUBSCRIBER_KINDS})", name="kind"),
    )
    op.create_index(
        "ix_metrics_retention_cohort", "metrics_retention", [sa.text("cohort_week DESC")]
    )


# --- наблюдаемость самой агрегации -----------------------------------------
def _create_rollup_runs() -> None:
    op.create_table(
        "metrics_rollup_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("day_from", sa.Date(), nullable=False),
        sa.Column("day_to", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'running'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("rows_written", sa.Integer(), nullable=False, server_default=ZERO),
        sa.Column("duration_seconds", sa.Float()),
        sa.Column("prefect_flow_run_id", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="status"),
        sa.CheckConstraint("day_to >= day_from", name="day_range"),
        sa.CheckConstraint(
            "scope IN ('harvest', 'subscribers', 'delivery', 'engagement', "
            "'retention', 'llm', 'all')",
            name="scope",
        ),
    )
    op.create_index(
        "ix_metrics_rollup_runs_started",
        "metrics_rollup_runs",
        ["scope", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    for table in (
        "metrics_rollup_runs",
        "metrics_retention",
        "metrics_engagement_daily",
        "metrics_delivery_daily",
        "metrics_subscribers_daily",
        "metrics_harvest_daily",
        "subscriber_activity",
    ):
        op.drop_table(table)
