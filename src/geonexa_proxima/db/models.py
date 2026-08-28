"""Нормализованная PostgreSQL-схема persistence-слоя."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from geonexa_proxima.db.base import Base

UUID_PK = PGUUID(as_uuid=True)
EMPTY_JSON = text("'{}'::jsonb")

ITEM_KINDS = "'paper', 'method', 'software', 'dataset'"
SOURCE_NAMES = "'arxiv', 'openalex', 'crossref', 'semantic_scholar', 'github', 'huggingface'"
FEEDBACK_KINDS = "'very_interesting', 'useful', 'not_interesting', 'save', 'deeper'"
USER_STATUSES = "'active', 'inactive', 'blocked'"
INTEREST_POLARITIES = "'positive', 'negative'"
INTEREST_SIGNAL_SOURCES = "'feedback', 'system'"


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


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(f"status IN ({USER_STATUSES})", name="status"),
        Index("ix_users_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    external_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(Text)
    language_code: Mapped[str | None] = mapped_column(String(16))
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


class UserProfileModel(Base):
    __tablename__ = "user_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", "normalized_name"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_user_profiles_user_id", "user_id"),
        Index(
            "uq_user_profiles_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index(
            "ix_user_profiles_digest_enabled",
            "user_id",
            postgresql_where=text("digest_enabled"),
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
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
        UUID_PK, ForeignKey("user_profiles.id", ondelete="CASCADE"), nullable=False
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
        UUID_PK, ForeignKey("user_profiles.id", ondelete="CASCADE"), nullable=False
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
        UUID_PK, ForeignKey("user_profiles.id", ondelete="CASCADE"), nullable=False
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
        Index("ix_feedback_user_id_created_at", "user_id", "created_at"),
        Index("ix_feedback_profile_id_created_at", "profile_id", "created_at"),
        Index("ix_feedback_item_id", "item_id"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("user_profiles.id", ondelete="SET NULL")
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
        CheckConstraint(
            "status IN ('pending', 'building', 'ready', 'sent', 'failed')",
            name="status",
        ),
        CheckConstraint("period_end > period_start", name="period"),
        Index("ix_digests_status_created_at", "status", "created_at"),
        Index("ix_digests_profile_id_created_at", "profile_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("users.id", ondelete="CASCADE")
    )
    profile_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("user_profiles.id", ondelete="SET NULL")
    )
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
    explanation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )


class CollectionRunModel(Base):
    __tablename__ = "collection_runs"
    __table_args__ = (
        CheckConstraint(f"source IN ({SOURCE_NAMES})", name="source"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')",
            name="status",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="finish_after_start",
        ),
        Index("ix_collection_runs_source_started_at", "source", "started_at"),
        Index(
            "uq_collection_runs_running_source",
            "source",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=uuid4)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    statistics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=EMPTY_JSON
    )
    error: Mapped[str | None] = mapped_column(Text)
