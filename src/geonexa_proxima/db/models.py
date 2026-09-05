"""Нормализованная PostgreSQL-схема persistence-слоя."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    ARRAY as SA_ARRAY,  # noqa: F401 — используется через postgresql.ARRAY
)
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from geonexa_proxima.db.base import Base
from geonexa_proxima.domain import ALL_KINDS, BotStatus, sql_literals
from geonexa_proxima.vector.types import Vector

UUID_PK = PGUUID(as_uuid=True)
EMPTY_JSON = text("'{}'::jsonb")

# Дублируют дефолты Settings намеренно: модуль не должен зависеть от того,
# что конфигурация вообще собралась. Расхождение ловит тест.
DEFAULT_EMBEDDING_DIMENSIONS = 1024
DEFAULT_VECTOR_COLUMN_TYPE = "vector"

ITEM_KINDS = "'paper', 'method', 'software', 'dataset'"
SOURCE_NAMES = (
    "'arxiv', 'openalex', 'crossref', 'semantic_scholar', 'github', 'huggingface', "
    "'cyberleninka', 'oai'"
)
FEEDBACK_KINDS = "'very_interesting', 'useful', 'not_interesting', 'save', 'deeper'"
SUBSCRIBER_KINDS = sql_literals(ALL_KINDS)
SUBSCRIBER_STATUSES = "'pending', 'active', 'paused', 'blocked', 'left'"
INTEREST_POLARITIES = "'positive', 'negative'"
INTEREST_SIGNAL_SOURCES = "'feedback', 'system'"
GROUP_MODES = "'any_of', 'all_of', 'none_of'"
MATCH_TYPES = "'phrase', 'token', 'prefix', 'regex'"
RUN_STATUSES = "'running', 'succeeded', 'failed', 'cancelled'"
DECISIONS = "'accepted', 'borderline', 'rejected', 'duplicate'"
GATE_STAGES = "'keyword', 'semantic', 'llm', 'dedup'"
SUBSCRIPTION_STATUSES = "'pending', 'trial', 'active', 'expired', 'cancelled'"
BOT_STATUSES = sql_literals(tuple(status.value for status in BotStatus))
DELIVERY_STATUSES = "'queued', 'claimed', 'sending', 'sent', 'failed', 'skipped', 'cancelled'"
MESSAGE_STATUSES = "'sent', 'failed', 'skipped', 'deleted', 'edited'"
#: Статусы дайджеста. Кортеж — источник правды, строка для CHECK собирается из
#: него: два списка в разных файлах однажды разъедутся, и это уже случалось.
DIGEST_STATUS_VALUES: tuple[str, ...] = (
    "pending",
    "building",
    "ready",
    "queued",
    "sent",
    "partial",
    "failed",
    "skipped",
)
DIGEST_STATUSES = ", ".join(f"'{value}'" for value in DIGEST_STATUS_VALUES)
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
ACTIVITY_KINDS = (
    "'registered', 'command', 'search', 'feedback', 'digest_received', "
    "'link_click', 'profile_edit', 'deep_dive', 'subscription_changed', "
    "'blocked_bot', 'chat_joined', 'chat_left'"
)


class ItemModel(Base):
    """Канонический объект независимо от числа внешних источников."""

    __tablename__ = "items"
    __table_args__ = (
        CheckConstraint(f"kind IN ({ITEM_KINDS})", name="kind"),
        CheckConstraint(
            "semantic_score IS NULL OR semantic_score BETWEEN -1 AND 1",
            name="semantic_score_range",
        ),
        CheckConstraint(
            "rank_total_score IS NULL OR rank_total_score BETWEEN 0 AND 10",
            name="rank_total_score_range",
        ),
        Index("uq_items_normalized_title", "normalized_title", unique=True),
        Index(
            "uq_items_doi",
            "doi",
            unique=True,
            postgresql_where=text("doi IS NOT NULL"),
        ),
        Index(
            "uq_items_arxiv_id",
            "arxiv_id",
            unique=True,
            postgresql_where=text("arxiv_id IS NOT NULL"),
        ),
        Index(
            "ix_items_digest_ranking",
            text("rank_total_score DESC"),
            postgresql_where=text("rank_total_score IS NOT NULL"),
        ),
        Index("ix_items_publication_date", "publication_date"),
        Index("ix_items_content_hash", "content_hash"),
        Index(
            "ix_items_keyword_score",
            text("keyword_score DESC"),
            postgresql_where=text("keyword_score IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_title: Mapped[str] = mapped_column(Text, nullable=False)
    abstract: Mapped[str | None] = mapped_column(Text)
    doi: Mapped[str | None] = mapped_column(String(255))
    arxiv_id: Mapped[str | None] = mapped_column(String(64))
    canonical_url: Mapped[str | None] = mapped_column(Text)
    publication_date: Mapped[date | None] = mapped_column(Date)
    venue: Mapped[str | None] = mapped_column(Text)
    citation_count: Mapped[int | None] = mapped_column(Integer)
    semantic_score: Mapped[float | None] = mapped_column(Float)
    ranking: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    rank_total_score: Mapped[float | None] = mapped_column(Float)
    deep_analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    keyword_score: Mapped[float | None] = mapped_column(Float)
    matched_terms: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    harvest_profile_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("harvest_profiles.id", ondelete="SET NULL")
    )
    gate_stage: Mapped[str | None] = mapped_column(String(16))
    language: Mapped[str | None] = mapped_column(String(8))
    is_preprint: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    retracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class ItemSourceModel(Base):
    """Версия канонического объекта в конкретном внешнем источнике."""

    __tablename__ = "item_sources"
    __table_args__ = (
        CheckConstraint(f"source IN ({SOURCE_NAMES})", name="source"),
        UniqueConstraint("source", "external_id"),
        Index("ix_item_sources_item_id", "item_id"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    item_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AuthorModel(Base):
    __tablename__ = "authors"
    __table_args__ = (
        Index("uq_authors_normalized_name", "normalized_name", unique=True),
        Index(
            "uq_authors_orcid",
            "orcid",
            unique=True,
            postgresql_where=text("orcid IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    orcid: Mapped[str | None] = mapped_column(String(32))


class ItemAuthorModel(Base):
    __tablename__ = "item_authors"
    __table_args__ = (
        UniqueConstraint("item_id", "position"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        Index("ix_item_authors_author_id", "author_id"),
    )

    item_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    author_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("authors.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class TopicModel(Base):
    __tablename__ = "topics"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class ItemTopicModel(Base):
    __tablename__ = "item_topics"
    __table_args__ = (Index("ix_item_topics_topic_id", "topic_id"),)

    item_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True
    )


class RepositoryModel(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        CheckConstraint(f"source IS NULL OR source IN ({SOURCE_NAMES})", name="source"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source: Mapped[str | None] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(512))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )


class ItemRepositoryLinkModel(Base):
    __tablename__ = "item_repositories"
    __table_args__ = (Index("ix_item_repositories_repository_id", "repository_id"),)

    item_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    repository_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )


class DatasetModel(Base):
    __tablename__ = "datasets"
    __table_args__ = (
        CheckConstraint(f"source IS NULL OR source IN ({SOURCE_NAMES})", name="source"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(512))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )


class ItemDatasetModel(Base):
    __tablename__ = "item_datasets"
    __table_args__ = (Index("ix_item_datasets_dataset_id", "dataset_id"),)

    item_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    dataset_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True
    )


class SubscriberModel(Base):
    """Человек, группа или канал: всё, что имеет chat_id и получает дайджест."""

    __tablename__ = "subscribers"
    __table_args__ = (
        CheckConstraint(f"kind IN ({SUBSCRIBER_KINDS})", name="kind"),
        CheckConstraint(f"status IN ({SUBSCRIBER_STATUSES})", name="status"),
        CheckConstraint(
            "(kind = 'user' AND telegram_chat_id > 0) OR (kind <> 'user')",
            name="chat_id_sign",
        ),
        Index("ix_subscribers_status", "status"),
        Index("ix_subscribers_kind_status", "kind", "status"),
        Index(
            "ix_subscribers_active",
            "status",
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_username: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(Text)
    language_code: Mapped[str | None] = mapped_column(String(16))
    is_owner: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    added_by_subscriber_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("subscribers.id", ondelete="SET NULL")
    )
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, default="Europe/Moscow", server_default=text("'Europe/Moscow'")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active", server_default=text("'active'")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class SubscriberProfileModel(Base):
    __tablename__ = "subscriber_profiles"
    __table_args__ = (
        UniqueConstraint("subscriber_id", "normalized_name"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "delivery_format IN ('cards', 'compact', 'single_message', 'digest_post')",
            name="delivery_format",
        ),
        CheckConstraint("max_items BETWEEN 1 AND 100", name="max_items_range"),
        CheckConstraint("min_personal_score BETWEEN 0 AND 1", name="min_personal_score_range"),
        Index("ix_subscriber_profiles_subscriber_id", "subscriber_id"),
        Index(
            "uq_subscriber_profiles_active_subscriber",
            "subscriber_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index(
            "ix_subscriber_profiles_digest_enabled",
            "subscriber_id",
            postgresql_where=text("digest_enabled"),
        ),
        Index(
            "ix_subscriber_profiles_next_digest",
            "next_digest_at",
            postgresql_where=text("digest_enabled"),
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    subscriber_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("subscribers.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    description_en: Mapped[str | None] = mapped_column(Text)
    translation_source_hash: Mapped[str | None] = mapped_column(String(32))
    compiled_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    digest_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    digest_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    schedule_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("schedules.id", ondelete="SET NULL")
    )
    timezone: Mapped[str | None] = mapped_column(Text)
    delivery_format: Mapped[str] = mapped_column(
        String(32), nullable=False, default="cards", server_default=text("'cards'")
    )
    max_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20, server_default=text("20")
    )
    min_personal_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.5, server_default=text("0.5")
    )
    min_global_score: Mapped[float | None] = mapped_column(Float)
    kinds: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{paper,method,software,dataset}'::text[]"),
    )
    quiet_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class ProfileInterestModel(Base):
    __tablename__ = "profile_interests"
    __table_args__ = (
        CheckConstraint(
            "(topic_id IS NOT NULL AND query IS NULL) OR (topic_id IS NULL AND query IS NOT NULL)",
            name="single_interest_target",
        ),
        CheckConstraint(f"polarity IN ({INTEREST_POLARITIES})", name="polarity"),
        CheckConstraint("weight >= 0 AND weight <= 10", name="weight_range"),
        UniqueConstraint("profile_id", "topic_id"),
        UniqueConstraint("profile_id", "query"),
        Index("ix_profile_interests_profile_id", "profile_id"),
        Index("ix_profile_interests_topic_id", "topic_id"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("subscriber_profiles.id", ondelete="CASCADE"), nullable=False
    )
    topic_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("topics.id", ondelete="CASCADE")
    )
    query: Mapped[str | None] = mapped_column(Text)
    polarity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="positive", server_default=text("'positive'")
    )
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class ProfileInterestSignalModel(Base):
    __tablename__ = "profile_interest_signals"
    __table_args__ = (
        CheckConstraint(
            "(topic_id IS NOT NULL AND query IS NULL) OR (topic_id IS NULL AND query IS NOT NULL)",
            name="single_signal_target",
        ),
        CheckConstraint(f"polarity IN ({INTEREST_POLARITIES})", name="polarity"),
        CheckConstraint(f"source IN ({INTEREST_SIGNAL_SOURCES})", name="source"),
        CheckConstraint("weight >= 0 AND weight <= 10", name="weight_range"),
        CheckConstraint("evidence_count >= 1", name="evidence_count_positive"),
        UniqueConstraint("profile_id", "topic_id"),
        UniqueConstraint("profile_id", "query"),
        Index("ix_profile_interest_signals_profile_id", "profile_id"),
        Index("ix_profile_interest_signals_topic_id", "topic_id"),
        Index("ix_profile_interest_signals_source_feedback_id", "source_feedback_id"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("subscriber_profiles.id", ondelete="CASCADE"), nullable=False
    )
    topic_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("topics.id", ondelete="CASCADE")
    )
    query: Mapped[str | None] = mapped_column(Text)
    polarity: Mapped[str] = mapped_column(String(16), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="feedback", server_default=text("'feedback'")
    )
    source_feedback_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("feedback.id", ondelete="SET NULL")
    )
    evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class ProfileItemScoreModel(Base):
    __tablename__ = "profile_item_scores"
    __table_args__ = (
        UniqueConstraint("profile_id", "item_id", "profile_version"),
        CheckConstraint("profile_version >= 1", name="profile_version_positive"),
        CheckConstraint("semantic_score BETWEEN 0 AND 1", name="semantic_score_range"),
        CheckConstraint("reranker_score BETWEEN 0 AND 1", name="reranker_score_range"),
        CheckConstraint("global_score BETWEEN 0 AND 1", name="global_score_range"),
        CheckConstraint("interest_score BETWEEN 0 AND 1", name="interest_score_range"),
        CheckConstraint("personal_score BETWEEN 0 AND 1", name="personal_score_range"),
        Index(
            "ix_profile_item_scores_profile_ranking",
            "profile_id",
            text("personal_score DESC"),
        ),
        Index("ix_profile_item_scores_item_id", "item_id"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("subscriber_profiles.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    semantic_score: Mapped[float] = mapped_column(Float, nullable=False)
    reranker_score: Mapped[float] = mapped_column(Float, nullable=False)
    global_score: Mapped[float] = mapped_column(Float, nullable=False)
    interest_score: Mapped[float] = mapped_column(Float, nullable=False)
    personal_score: Mapped[float] = mapped_column(Float, nullable=False)
    #: Какой гранью профиля материал был найден. Пусто — нашёлся профилем
    #: целиком. Хранится текстом, а не номером: номера граней меняются при
    #: правке описания, и вопрос «почему это показали» после первой же правки
    #: остался бы без ответа.
    matched_facet: Mapped[str | None] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class FeedbackModel(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint(f"kind IN ({FEEDBACK_KINDS})", name="kind"),
        Index("ix_feedback_subscriber_id_created_at", "subscriber_id", "created_at"),
        Index("ix_feedback_profile_id_created_at", "profile_id", "created_at"),
        Index("ix_feedback_item_id", "item_id"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    subscriber_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("subscribers.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("subscriber_profiles.id", ondelete="SET NULL")
    )
    item_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class DigestModel(Base):
    __tablename__ = "digests"
    __table_args__ = (
        CheckConstraint(f"status IN ({DIGEST_STATUSES})", name="status"),
        CheckConstraint("kind IN ('personal', 'group', 'broadcast')", name="kind"),
        CheckConstraint("period_end > period_start", name="period"),
        Index("ix_digests_status_created_at", "status", "created_at"),
        Index("ix_digests_profile_id_created_at", "profile_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    subscriber_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("subscribers.id", ondelete="CASCADE")
    )
    profile_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("subscriber_profiles.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="personal", server_default=text("'personal'")
    )
    schedule_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("schedules.id", ondelete="SET NULL")
    )
    prefect_flow_run_id: Mapped[str | None] = mapped_column(Text)
    item_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    error: Mapped[str | None] = mapped_column(Text)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DigestItemModel(Base):
    __tablename__ = "digest_items"
    __table_args__ = (
        UniqueConstraint("digest_id", "position"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        Index("ix_digest_items_item_id", "item_id"),
    )

    digest_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("digests.id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    score_snapshot: Mapped[float | None] = mapped_column(Float)
    personal_score: Mapped[float | None] = mapped_column(Float)
    global_score: Mapped[float | None] = mapped_column(Float)
    profile_score_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("profile_item_scores.id", ondelete="SET NULL")
    )
    explanation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )


# =========================================================================== #
# Harvest: что платформа вообще ищет                                          #
# =========================================================================== #


class HarvestProfileModel(Base):
    __tablename__ = "harvest_profiles"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("keyword_score_threshold BETWEEN 0 AND 1", name="keyword_threshold_range"),
        Index(
            "uq_harvest_profiles_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    satisfy_expr: Mapped[str] = mapped_column(Text, nullable=False)
    keyword_score_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.35, server_default=text("0.35")
    )
    borderline_semantic_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.52, server_default=text("0.52")
    )
    languages: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{en,ru}'::text[]")
    )
    item_kinds: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{paper,method,software,dataset}'::text[]"),
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class HarvestTermGroupModel(Base):
    __tablename__ = "harvest_term_groups"
    __table_args__ = (
        UniqueConstraint("harvest_profile_id", "key", name="uq_harvest_group_key"),
        CheckConstraint(f"mode IN ({GROUP_MODES})", name="mode"),
        CheckConstraint("min_matches >= 0", name="min_matches_non_negative"),
        CheckConstraint("weight BETWEEN 0 AND 1", name="weight_range"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    harvest_profile_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("harvest_profiles.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    min_matches: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{title,abstract,keywords}'::text[]")
    )
    weight: Mapped[float] = mapped_column(
        Float, nullable=False, default=0, server_default=text("0")
    )
    is_hard: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    penalty: Mapped[float] = mapped_column(
        Float, nullable=False, default=0, server_default=text("0")
    )
    affects_satisfy: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class HarvestTermModel(Base):
    __tablename__ = "harvest_terms"
    __table_args__ = (
        UniqueConstraint("group_id", "normalized_term", "match_type", name="uq_harvest_term"),
        CheckConstraint(f"match_type IN ({MATCH_TYPES})", name="match_type"),
        CheckConstraint("weight >= 0 AND weight <= 10", name="weight_range"),
        Index("ix_harvest_terms_enabled", "group_id", postgresql_where=text("enabled")),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    group_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("harvest_term_groups.id", ondelete="CASCADE"), nullable=False
    )
    term: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_term: Mapped[str] = mapped_column(Text, nullable=False)
    match_type: Mapped[str] = mapped_column(String(16), nullable=False)
    lang: Mapped[str | None] = mapped_column(String(8))
    weight: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default=text("1.0")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # Через месяц работы видно, какие термины не сработали ни разу,
    # а какие тянут шум: чистить список без этих чисел — гадание.
    hit_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class HarvestQueryModel(Base):
    __tablename__ = "harvest_queries"
    __table_args__ = (
        UniqueConstraint("harvest_profile_id", "source", "key", name="uq_harvest_query"),
        CheckConstraint(f"source IN ({SOURCE_NAMES})", name="source"),
        CheckConstraint("max_items BETWEEN 1 AND 5000", name="max_items_range"),
        Index("ix_harvest_queries_enabled", "source", "priority", postgresql_where=text("enabled")),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    harvest_profile_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("harvest_profiles.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default=text("5")
    )
    max_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=200, server_default=text("200")
    )
    lookback_hours: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class SourceCursorModel(Base):
    """Позволяет доливать историю и переживать падения без перезапуска с нуля."""

    __tablename__ = "source_cursors"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    harvest_query_id: Mapped[UUID] = mapped_column(
        UUID_PK,
        ForeignKey("harvest_queries.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    cursor: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    last_external_id: Mapped[str | None] = mapped_column(Text)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class HarvestRunModel(Base):
    __tablename__ = "harvest_runs"
    __table_args__ = (
        CheckConstraint(f"status IN ({RUN_STATUSES})", name="status"),
        CheckConstraint("trigger IN ('schedule', 'manual', 'api', 'backfill')", name="trigger"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finish_after_start"
        ),
        Index("ix_harvest_runs_status_started", "status", text("started_at DESC")),
        Index(
            "uq_harvest_runs_running",
            "status",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        # По этому индексу плановый прогон узнаёт, с каких суток продолжать.
        Index(
            "ix_harvest_runs_succeeded_until",
            text("until DESC"),
            postgresql_where=text("status = 'succeeded'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    harvest_profile_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("harvest_profiles.id", ondelete="SET NULL")
    )
    prefect_flow_run_id: Mapped[str | None] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(
        String(16), nullable=False, default="schedule", server_default=text("'schedule'")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default=text("'running'")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Верхняя граница окна, исключающая. Прогон за 30 августа закрывается
    #: московской полуночью 31-го — и именно она отвечает на вопрос «до какого
    #: дня корпус собран».
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    error: Mapped[str | None] = mapped_column(Text)
    triggered_by: Mapped[str | None] = mapped_column(Text)


class HarvestDecisionModel(Base):
    """Журнал решений гейта: на нём калибруются пороги, а не на ощущениях."""

    __tablename__ = "harvest_decisions"
    __table_args__ = (
        CheckConstraint(f"stage IN ({GATE_STAGES})", name="stage"),
        CheckConstraint(f"decision IN ({DECISIONS})", name="decision"),
        CheckConstraint(f"source IN ({SOURCE_NAMES})", name="source"),
        Index("ix_harvest_decisions_run", "harvest_run_id", "decision"),
        Index("ix_harvest_decisions_created", "decision", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    harvest_run_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("harvest_runs.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    item_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("items.id", ondelete="SET NULL")
    )
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    keyword_score: Mapped[float | None] = mapped_column(Float)
    semantic_score: Mapped[float | None] = mapped_column(Float)
    matched_terms: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    blocked_by: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# =========================================================================== #
# Подписки                                                                    #
# =========================================================================== #


class SubscriptionPlanModel(Base):
    __tablename__ = "subscription_plans"
    __table_args__ = (
        CheckConstraint("max_profiles >= 1", name="max_profiles_positive"),
        CheckConstraint("min_interval_hours >= 1", name="min_interval_positive"),
        Index(
            "uq_subscription_plans_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    max_profiles: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    max_items_per_digest: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20, server_default=text("20")
    )
    min_interval_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=168, server_default=text("168")
    )
    deep_analysis_quota_per_month: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    allow_group_chats: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    features: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class SubscriptionModel(Base):
    """Пересечение активных периодов запрещено exclusion-ограничением в БД.

    Ограничение создаётся сырым SQL в миграции 0003: SQLAlchemy не умеет
    описывать EXCLUDE декларативно, а держать его только в коде значило бы
    доверять инварианту, который никто не проверяет.
    """

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(f"status IN ({SUBSCRIPTION_STATUSES})", name="status"),
        CheckConstraint("source IN ('admin', 'trial', 'import', 'payment')", name="source"),
        CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="period"),
        Index("ix_subscriptions_subscriber", "subscriber_id", "status"),
        Index(
            "ix_subscriptions_expiring",
            "ends_at",
            postgresql_where=text("status IN ('active', 'trial')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    subscriber_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("subscribers.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("subscription_plans.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_renew: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="admin", server_default=text("'admin'")
    )
    price_amount: Mapped[float | None] = mapped_column(Numeric(12, 2))
    price_currency: Mapped[str | None] = mapped_column(String(8))
    external_payment_id: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class SubscriptionEventModel(Base):
    __tablename__ = "subscription_events"
    __table_args__ = (
        CheckConstraint(
            "event IN ('created', 'activated', 'extended', 'downgraded', 'upgraded', "
            "'expired', 'cancelled', 'reminded')",
            name="event",
        ),
        Index(
            "ix_subscription_events_subscription",
            "subscription_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    subscription_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    event: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    actor: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# =========================================================================== #
# Чаты, куда добавили бота                                                    #
# =========================================================================== #


class ChatMembershipModel(Base):
    __tablename__ = "chat_memberships"
    __table_args__ = (
        CheckConstraint(f"bot_status IN ({BOT_STATUSES})", name="bot_status"),
        Index("ix_chat_memberships_status", "bot_status"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    subscriber_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("subscribers.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    bot_status: Mapped[str] = mapped_column(String(24), nullable=False)
    can_post_messages: Mapped[bool | None] = mapped_column(Boolean)
    can_edit_messages: Mapped[bool | None] = mapped_column(Boolean)
    can_delete_messages: Mapped[bool | None] = mapped_column(Boolean)
    member_count: Mapped[int | None] = mapped_column(Integer)
    chat_type: Mapped[str | None] = mapped_column(String(24))
    invite_link: Mapped[str | None] = mapped_column(Text)
    added_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class ChatEventModel(Base):
    __tablename__ = "chat_events"
    __table_args__ = (
        Index("ix_chat_events_subscriber", "subscriber_id", text("occurred_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    subscriber_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("subscribers.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw_update: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# =========================================================================== #
# Расписания и прогоны Prefect                                                #
# =========================================================================== #


class ScheduleModel(Base):
    __tablename__ = "schedules"
    __table_args__ = (
        CheckConstraint(f"kind IN ({SCHEDULE_KINDS})", name="kind"),
        CheckConstraint(
            "cron IS NOT NULL OR interval_seconds IS NOT NULL", name="schedule_defined"
        ),
        CheckConstraint("interval_seconds IS NULL OR interval_seconds >= 60", name="interval_min"),
        Index("ix_schedules_kind_enabled", "kind", "enabled"),
        Index("ix_schedules_next_run", "next_run_at", postgresql_where=text("enabled")),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subscriber_profile_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("subscriber_profiles.id", ondelete="CASCADE")
    )
    prefect_deployment_id: Mapped[str | None] = mapped_column(Text)
    prefect_schedule_id: Mapped[str | None] = mapped_column(Text)
    cron: Mapped[str | None] = mapped_column(Text)
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, default="Europe/Moscow", server_default=text("'Europe/Moscow'")
    )
    anchor_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    sync_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class FlowRunModel(Base):
    """Локальное зеркало Prefect: дашборд не должен ходить в его API на каждый чих."""

    __tablename__ = "flow_runs"
    __table_args__ = (
        Index("ix_flow_runs_kind_started", "kind", text("started_at DESC")),
        Index("ix_flow_runs_state", "state"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    prefect_flow_run_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    flow_name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str | None] = mapped_column(String(32))
    schedule_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("schedules.id", ondelete="SET NULL")
    )
    subscriber_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("subscribers.id", ondelete="SET NULL")
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


# =========================================================================== #
# Доставка: очередь на PostgreSQL и логи рассылок                             #
# =========================================================================== #


class DeliveryJobModel(Base):
    """Очередь берётся SELECT ... FOR UPDATE SKIP LOCKED — Redis не нужен."""

    __tablename__ = "delivery_jobs"
    __table_args__ = (
        UniqueConstraint("digest_id", "target_chat_id", name="uq_delivery_job_target"),
        CheckConstraint("channel IN ('personal', 'group')", name="channel"),
        CheckConstraint(f"status IN ({DELIVERY_STATUSES})", name="status"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        Index(
            "ix_delivery_jobs_queue",
            "channel",
            "scheduled_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_delivery_jobs_subscriber", "subscriber_id", text("created_at DESC")),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    digest_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("digests.id", ondelete="CASCADE"), nullable=False
    )
    subscriber_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("subscribers.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    target_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default=text("'queued'")
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default=text("5")
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    prefect_flow_run_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class DeliveryMessageModel(Base):
    __tablename__ = "delivery_messages"
    __table_args__ = (
        CheckConstraint(f"status IN ({MESSAGE_STATUSES})", name="status"),
        Index("ix_delivery_messages_job", "delivery_job_id"),
        Index("ix_delivery_messages_chat", "chat_id", text("sent_at DESC")),
        Index("ix_delivery_messages_status", "status", text("sent_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    delivery_job_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("delivery_jobs.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("items.id", ondelete="SET NULL")
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    error_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    retry_after: Mapped[int | None] = mapped_column(Integer)
    text_preview: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# =========================================================================== #
# Реестр моделей                                                              #
# =========================================================================== #


class LLMProviderModel(Base):
    __tablename__ = "llm_providers"
    __table_args__ = (CheckConstraint(f"protocol IN ({LLM_PROTOCOLS})", name="protocol"),)

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    protocol: Mapped[str] = mapped_column(String(24), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    api_key_env_var: Mapped[str | None] = mapped_column(Text)
    default_headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    is_managed_by_env: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class LLMModelModel(Base):
    __tablename__ = "llm_models"
    __table_args__ = (
        UniqueConstraint("provider_id", "key", name="uq_llm_model_key"),
        CheckConstraint("tier IS NULL OR tier IN ('light', 'heavy', 'both')", name="tier"),
        CheckConstraint(f"reasoning_style IN ({REASONING_STYLES})", name="reasoning_style"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    provider_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("llm_providers.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[str | None] = mapped_column(String(16))
    supports_reasoning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    reasoning_style: Mapped[str] = mapped_column(
        String(24), nullable=False, default="none", server_default=text("'none'")
    )
    reasoning_levels: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{low,high,max}'::text[]")
    )
    supports_json_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    supports_tools: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    context_window: Mapped[int | None] = mapped_column(Integer)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    input_price_per_1m: Mapped[float | None] = mapped_column(Numeric(10, 4))
    output_price_per_1m: Mapped[float | None] = mapped_column(Numeric(10, 4))
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class LLMRoleBindingModel(Base):
    """Роль — это действие. Ризонинг настраивается на каждое отдельно."""

    __tablename__ = "llm_role_bindings"
    __table_args__ = (
        CheckConstraint(f"role IN ({LLM_ROLES})", name="role"),
        CheckConstraint("temperature BETWEEN 0 AND 2", name="temperature_range"),
        CheckConstraint(
            f"reasoning_effort IS NULL OR reasoning_effort IN ({REASONING_LEVELS})",
            name="reasoning_effort",
        ),
        CheckConstraint("concurrency BETWEEN 1 AND 64", name="concurrency_range"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    role: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    model_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("llm_models.id", ondelete="RESTRICT"), nullable=False
    )
    fallback_model_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("llm_models.id", ondelete="SET NULL")
    )
    temperature: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.1, server_default=text("0.1")
    )
    top_p: Mapped[float | None] = mapped_column(Float)
    max_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16))
    json_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=180, server_default=text("180")
    )
    concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4, server_default=text("4")
    )
    system_prompt_override: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    updated_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class LLMCallLogModel(Base):
    __tablename__ = "llm_call_log"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'error', 'timeout', 'rate_limited')", name="status"),
        Index("ix_llm_call_log_role", "role", text("created_at DESC")),
        Index("ix_llm_call_log_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("llm_models.id", ondelete="SET NULL")
    )
    item_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("items.id", ondelete="SET NULL")
    )
    subscriber_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("subscribers.id", ondelete="SET NULL")
    )
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class LLMUsageDailyModel(Base):
    __tablename__ = "llm_usage_daily"
    __table_args__ = (UniqueConstraint("day", "role", "model_id", name="uq_llm_usage_daily"),)

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("llm_models.id", ondelete="SET NULL")
    )
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    prompt_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    reasoning_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    cost_usd: Mapped[float] = mapped_column(
        Numeric(14, 6), nullable=False, default=0, server_default=text("0")
    )
    errors: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


# =========================================================================== #
# Настройки и аудит                                                           #
# =========================================================================== #


class AppSettingModel(Base):
    """Эффективное значение настройки: env задаёт старт, БД перекрывает."""

    __tablename__ = "app_settings"
    __table_args__ = (
        CheckConstraint(f"value_type IN ({VALUE_TYPES})", name="value_type"),
        Index("ix_app_settings_scope", "scope"),
    )

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)
    env_default: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    scope: Mapped[str] = mapped_column(
        String(24), nullable=False, default="general", server_default=text("'general'")
    )
    description: Mapped[str | None] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_env_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class AdminAuditLogModel(Base):
    __tablename__ = "admin_audit_log"
    __table_args__ = (
        Index("ix_admin_audit_created", text("created_at DESC")),
        Index("ix_admin_audit_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# =========================================================================== #
# Метрики                                                                     #
# =========================================================================== #


class SubscriberActivityModel(Base):
    """Без событийного лога не посчитать ни DAU, ни удержание когорт."""

    __tablename__ = "subscriber_activity"
    __table_args__ = (
        CheckConstraint(f"kind IN ({ACTIVITY_KINDS})", name="kind"),
        Index("ix_subscriber_activity_subscriber", "subscriber_id", text("occurred_at DESC")),
        Index("ix_subscriber_activity_kind", "kind", text("occurred_at DESC")),
        Index("ix_subscriber_activity_occurred_subscriber", "occurred_at", "subscriber_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    subscriber_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("subscribers.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("subscriber_profiles.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    item_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("items.id", ondelete="SET NULL")
    )
    digest_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("digests.id", ondelete="SET NULL")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


def _counter(name: str) -> Mapped[int]:
    return mapped_column(name, Integer, nullable=False, default=0, server_default=text("0"))


class MetricsHarvestDailyModel(Base):
    __tablename__ = "metrics_harvest_daily"
    __table_args__ = (
        UniqueConstraint("day", "source", name="uq_metrics_harvest_daily"),
        CheckConstraint(f"source IN ({SOURCE_NAMES})", name="source"),
        Index("ix_metrics_harvest_daily_day", text("day DESC")),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched: Mapped[int] = _counter("fetched")
    accepted: Mapped[int] = _counter("accepted")
    borderline: Mapped[int] = _counter("borderline")
    rejected: Mapped[int] = _counter("rejected")
    duplicates: Mapped[int] = _counter("duplicates")
    rescued_by_semantic: Mapped[int] = _counter("rescued_by_semantic")
    ranked: Mapped[int] = _counter("ranked")
    analyzed: Mapped[int] = _counter("analyzed")
    stored: Mapped[int] = _counter("stored")
    avg_keyword_score: Mapped[float | None] = mapped_column(Float)
    avg_global_score: Mapped[float | None] = mapped_column(Float)
    top_blocked_by: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MetricsSubscribersDailyModel(Base):
    __tablename__ = "metrics_subscribers_daily"
    __table_args__ = (
        UniqueConstraint("day", "kind", name="uq_metrics_subscribers_daily"),
        CheckConstraint(f"kind IN ({SUBSCRIBER_KINDS})", name="kind"),
        Index("ix_metrics_subscribers_daily_day", text("day DESC")),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    registered: Mapped[int] = _counter("registered")
    activated: Mapped[int] = _counter("activated")
    churned: Mapped[int] = _counter("churned")
    blocked: Mapped[int] = _counter("blocked")
    total: Mapped[int] = _counter("total")
    total_active: Mapped[int] = _counter("total_active")
    with_subscription: Mapped[int] = _counter("with_subscription")
    dau: Mapped[int] = _counter("dau")
    wau: Mapped[int] = _counter("wau")
    mau: Mapped[int] = _counter("mau")
    digest_enabled_profiles: Mapped[int] = _counter("digest_enabled_profiles")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MetricsDeliveryDailyModel(Base):
    __tablename__ = "metrics_delivery_daily"
    __table_args__ = (
        UniqueConstraint("day", "channel", name="uq_metrics_delivery_daily"),
        CheckConstraint("channel IN ('personal', 'group')", name="channel"),
        Index("ix_metrics_delivery_daily_day", text("day DESC")),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    jobs_created: Mapped[int] = _counter("jobs_created")
    jobs_sent: Mapped[int] = _counter("jobs_sent")
    jobs_failed: Mapped[int] = _counter("jobs_failed")
    jobs_skipped: Mapped[int] = _counter("jobs_skipped")
    messages_sent: Mapped[int] = _counter("messages_sent")
    messages_failed: Mapped[int] = _counter("messages_failed")
    rate_limited: Mapped[int] = _counter("rate_limited")
    recipients: Mapped[int] = _counter("recipients")
    avg_queue_seconds: Mapped[float | None] = mapped_column(Float)
    p95_queue_seconds: Mapped[float | None] = mapped_column(Float)
    top_errors: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MetricsEngagementDailyModel(Base):
    __tablename__ = "metrics_engagement_daily"
    __table_args__ = (
        UniqueConstraint("day", name="uq_metrics_engagement_daily"),
        CheckConstraint(
            "engagement_rate IS NULL OR engagement_rate BETWEEN 0 AND 1",
            name="engagement_rate_range",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    digests_sent: Mapped[int] = _counter("digests_sent")
    items_delivered: Mapped[int] = _counter("items_delivered")
    feedback_total: Mapped[int] = _counter("feedback_total")
    feedback_very_interesting: Mapped[int] = _counter("feedback_very_interesting")
    feedback_useful: Mapped[int] = _counter("feedback_useful")
    feedback_not_interesting: Mapped[int] = _counter("feedback_not_interesting")
    feedback_saved: Mapped[int] = _counter("feedback_saved")
    feedback_deeper: Mapped[int] = _counter("feedback_deeper")
    unique_reactors: Mapped[int] = _counter("unique_reactors")
    empty_digests: Mapped[int] = _counter("empty_digests")
    engagement_rate: Mapped[float | None] = mapped_column(Float)
    avg_items_per_digest: Mapped[float | None] = mapped_column(Float)
    avg_personal_score: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MetricsRetentionModel(Base):
    __tablename__ = "metrics_retention"
    __table_args__ = (
        UniqueConstraint("cohort_week", "week_offset", "kind", name="uq_metrics_retention"),
        CheckConstraint("week_offset >= 0", name="week_offset_non_negative"),
        CheckConstraint("retained <= cohort_size", name="retained_within_cohort"),
        CheckConstraint(f"kind IN ({SUBSCRIBER_KINDS})", name="kind"),
        Index("ix_metrics_retention_cohort", text("cohort_week DESC")),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    cohort_week: Mapped[date] = mapped_column(Date, nullable=False)
    week_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    cohort_size: Mapped[int] = _counter("cohort_size")
    retained: Mapped[int] = _counter("retained")
    retention_rate: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MetricsRollupRunModel(Base):
    """Без этого «график встал» и «график честно показывает ноль» неотличимы."""

    __tablename__ = "metrics_rollup_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="status"),
        CheckConstraint("day_to >= day_from", name="day_range"),
        CheckConstraint(
            "scope IN ('harvest', 'subscribers', 'delivery', 'engagement', "
            "'retention', 'llm', 'all')",
            name="scope",
        ),
        Index("ix_metrics_rollup_runs_started", "scope", text("started_at DESC")),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    day_from: Mapped[date] = mapped_column(Date, nullable=False)
    day_to: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default=text("'running'")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows_written: Mapped[int] = _counter("rows_written")
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    prefect_flow_run_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


# =========================================================================== #
# Векторы (pgvector)                                                          #
# =========================================================================== #


def _embedding_column() -> Vector:
    """Тип колонки векторов, не поднимая всю конфигурацию.

    Раньше здесь стоял ``get_settings()``. Из-за этого один неверный путь к
    сертификату делал весь persistence-слой неимпортируемым, а видел
    разработчик не «сертификат не найден», а вторичное «Table 'items' is
    already defined» — pytest пробовал импортировать модуль второй раз уже
    после того, как половина таблиц зарегистрировалась в MetaData.

    ORM-у от настроек нужны ровно два числа, и они читаются напрямую из
    окружения. Настоящая проверка размерности живёт там, где ошибиться
    дороже: в ``Settings`` (согласованность с моделью и с лимитом индекса),
    в векторном хранилище и в миграции.
    """

    raw = os.getenv("EMBEDDING_DIMENSIONS", "").strip()
    try:
        dimensions = int(raw) if raw else DEFAULT_EMBEDDING_DIMENSIONS
    except ValueError:
        dimensions = DEFAULT_EMBEDDING_DIMENSIONS
    if not 0 < dimensions <= 16_000:
        dimensions = DEFAULT_EMBEDDING_DIMENSIONS
    column_type = os.getenv("VECTOR_COLUMN_TYPE", "").strip().lower()
    if column_type not in {"vector", "halfvec"}:
        column_type = DEFAULT_VECTOR_COLUMN_TYPE
    return Vector(dimensions, column_type)


EMBEDDING_COLUMN = _embedding_column()


class ItemVectorModel(Base):
    """Вектор материала лежит рядом с корпусом: запись идёт одной транзакцией."""

    __tablename__ = "item_vectors"
    __table_args__ = (
        CheckConstraint("dimensions > 0", name="dimensions_positive"),
        Index("ix_item_vectors_model", "model"),
    )

    item_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    embedding: Mapped[list[float]] = mapped_column(EMBEDDING_COLUMN, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ProfileVectorModel(Base):
    """Пересобираемый кэш, а не источник истины: версия входит в ключ.

    Грань тоже входит в ключ. Профиль из нескольких тем даёт вектор-центроид
    между ними, и статья, глубоко попадающая в одну тему, проигрывает статье,
    слегка похожей на всё сразу. Поэтому у профиля не один вектор, а набор:
    грань 0 — весь профиль, дальше его отдельные темы.
    """

    __tablename__ = "profile_vectors"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("facet >= 0", name="facet_non_negative"),
        # Уборка старых версий ходит по одному профилю: без индекса это
        # последовательный проход по всей таблице на каждую правку профиля.
        Index("ix_profile_vectors_profile_id", "profile_id"),
    )

    profile_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("subscriber_profiles.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    facet: Mapped[int] = mapped_column(
        Integer, primary_key=True, default=0, server_default=text("0")
    )
    #: Отпечаток текста грани. Номер грани позиционный, а какой текст под ним
    #: окажется, зависит ещё и от настроек разбиения — версия профиля про них
    #: не знает. Без отпечатка смена `PROFILE_FACET_MIN_CHARS` молча оставляла
    #: бы под старым номером чужой вектор.
    text_hash: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    embedding: Mapped[list[float]] = mapped_column(EMBEDDING_COLUMN, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# --- индексы горячих путей -------------------------------------------------
#
# Объявлены здесь, а не в `__table_args__` каждой модели, потому что все они
# про одно: без них PostgreSQL читает таблицу целиком там, где это происходит
# регулярно. Держать их списком проще, чем искать по десяти классам.
#
# Первые четыре — под ночную уборку (`metrics/purge.py`): удаление по возрасту
# шло последовательным чтением по самым быстрорастущим таблицам и под
# `statement_timeout` отваливалось. Уборка не работала, таблица росла, и
# следующая попытка отваливалась быстрее.
#
# Остальные — под внешние ключи с `ON DELETE`. PostgreSQL их не индексирует
# сам: удаление одного материала заставляло его просканировать `harvest_decisions`,
# `llm_call_log`, `subscriber_activity` и `delivery_messages`, и админка
# отвечала пятисоткой на удаление одной строки.
Index("ix_harvest_decisions_created_at", HarvestDecisionModel.created_at)
Index("ix_delivery_messages_created_at", DeliveryMessageModel.created_at)
Index("ix_chat_events_occurred_at", ChatEventModel.occurred_at)
Index("ix_delivery_jobs_created_at", DeliveryJobModel.created_at)
Index("ix_harvest_decisions_item_id", HarvestDecisionModel.item_id)
Index("ix_delivery_messages_item_id", DeliveryMessageModel.item_id)
Index("ix_llm_call_log_item_id", LLMCallLogModel.item_id)
Index("ix_llm_call_log_subscriber_id", LLMCallLogModel.subscriber_id)
Index("ix_subscriber_activity_item_id", SubscriberActivityModel.item_id)
Index("ix_subscriber_activity_digest_id", SubscriberActivityModel.digest_id)
Index("ix_digests_subscriber_id", DigestModel.subscriber_id)
Index("ix_items_harvest_profile_id", ItemModel.harvest_profile_id)
Index("ix_digest_items_profile_score_id", DigestItemModel.profile_score_id)
Index("ix_flow_runs_schedule_id", FlowRunModel.schedule_id)
Index("ix_flow_runs_subscriber_id", FlowRunModel.subscriber_id)
