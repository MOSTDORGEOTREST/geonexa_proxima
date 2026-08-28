"""Доменные типы, независимые от БД, Telegram и внешних API."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, computed_field, model_validator


class ItemKind(StrEnum):
    PAPER = "paper"
    METHOD = "method"
    SOFTWARE = "software"
    DATASET = "dataset"


class SourceName(StrEnum):
    ARXIV = "arxiv"
    OPENALEX = "openalex"
    CROSSREF = "crossref"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    GITHUB = "github"
    HUGGINGFACE = "huggingface"


class FeedbackKind(StrEnum):
    VERY_INTERESTING = "very_interesting"
    USEFUL = "useful"
    NOT_INTERESTING = "not_interesting"
    SAVE = "save"
    DEEPER = "deeper"


class UserStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    BLOCKED = "blocked"


class InterestPolarity(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class InterestSignalSource(StrEnum):
    FEEDBACK = "feedback"
    SYSTEM = "system"


class TelegramIdentity(BaseModel):
    telegram_id: int = Field(gt=0)
    username: str | None = None
    display_name: str | None = None
    language_code: str | None = None


class User(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    external_user_id: int
    telegram_username: str | None = None
    display_name: str | None = None
    language_code: str | None = None
    status: UserStatus = UserStatus.ACTIVE
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def telegram_id(self) -> int:
        return self.external_user_id


class UserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    name: str
    normalized_name: str
    description: str | None = None
    compiled_text: str
    version: int = Field(ge=1)
    is_active: bool
    digest_enabled: bool
    digest_settings: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ProfileInterest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    profile_id: UUID
    topic_id: UUID | None = None
    topic_name: str | None = None
    query: str | None = None
    polarity: InterestPolarity
    weight: float = Field(ge=0, le=10)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_target(self) -> ProfileInterest:
        if (self.topic_id is None) == (not self.query):
            raise ValueError("exactly one of topic_id or query is required")
        return self

    @computed_field
    @property
    def target_text(self) -> str:
        return self.topic_name or self.query or str(self.topic_id)


class ProfileInterestSignal(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    profile_id: UUID
    topic_id: UUID | None = None
    topic_name: str | None = None
    query: str | None = None
    polarity: InterestPolarity
    weight: float = Field(ge=0, le=10)
    source: InterestSignalSource = InterestSignalSource.FEEDBACK
    source_feedback_id: UUID | None = None
    evidence_count: int = Field(default=1, ge=1)
    details: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_target(self) -> ProfileInterestSignal:
        if (self.topic_id is None) == (not self.query):
            raise ValueError("exactly one of topic_id or query is required")
        return self

    @computed_field
    @property
    def target_text(self) -> str:
        return self.topic_name or self.query or str(self.topic_id)


class ProfileItemScore(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    profile_id: UUID
    item_id: UUID
    profile_version: int = Field(ge=1)
    semantic_score: float = Field(ge=0, le=1)
    reranker_score: float = Field(ge=0, le=1)
    global_score: float = Field(ge=0, le=1)
    interest_score: float = Field(ge=0, le=1)
    personal_score: float = Field(ge=0, le=1)
    explanation: str | None = None
    created_at: datetime
    updated_at: datetime


class Author(BaseModel):
    name: str
    orcid: str | None = None


class CollectedItem(BaseModel):
    """Нормализованный элемент потока до сохранения и дедупликации."""

    source: SourceName
    external_id: str
    kind: ItemKind = ItemKind.PAPER
    title: str
    abstract: str | None = None
    authors: list[Author] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    doi: str | None = None
    arxiv_id: str | None = None
    publication_date: date | None = None
    venue: str | None = None
    citation_count: int | None = None
    url: HttpUrl | None = None
    code_url: HttpUrl | None = None
    dataset_url: HttpUrl | None = None
    raw: dict[str, object] = Field(default_factory=dict)

    @computed_field
    @property
    def embedding_text(self) -> str:
        parts = [self.title]
        if self.abstract:
            parts.append(self.abstract)
        if self.keywords:
            parts.append("Keywords: " + ", ".join(self.keywords))
        return "\n\n".join(parts)


class RankResult(BaseModel):
    relevance: float = Field(ge=0, le=10)
    novelty: float = Field(ge=0, le=10)
    scientific_quality: float = Field(ge=0, le=10)
    practical_value: float = Field(ge=0, le=10)
    importance_for_geotechnics: float = Field(ge=0, le=10)
    importance_for_ai: float = Field(ge=0, le=10)
    recommend_deep_analysis: bool = False
    categories: list[str] = Field(default_factory=list)
    reason: str

    @computed_field
    @property
    def total_score(self) -> float:
        """Формула MVP: 0.30R + 0.20N + 0.15Q + 0.20P + 0.15A."""

        return round(
            0.30 * self.relevance
            + 0.20 * self.novelty
            + 0.15 * self.scientific_quality
            + 0.20 * self.practical_value
            + 0.15 * self.importance_for_ai,
            3,
        )


class DeepAnalysis(BaseModel):
    summary: str
    novelty: str
    method: str
    data: str | None = None
    architecture: str | None = None
    results: str | None = None
    prior_art: str | None = None
    physics_assessment: str | None = None
    limitations: list[str] = Field(default_factory=list)
    geotechnical_transfer: str
    research_ideas: list[str] = Field(default_factory=list)
    code_available: bool = False
    dataset_available: bool = False


class StoredItem(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    kind: ItemKind
    title: str
    abstract: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    canonical_url: str | None = None
    publication_date: date | None = None
    semantic_score: float | None = None
    rank: RankResult | None = None
    analysis: DeepAnalysis | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SearchHit(BaseModel):
    item_id: UUID
    score: float
    title: str
    snippet: str | None = None
