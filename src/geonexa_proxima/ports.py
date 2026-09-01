"""Контракты между ядром и заменяемой инфраструктурой."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from geonexa_proxima.domain import (
    CollectedItem,
    DeepAnalysis,
    FeedbackKind,
    InterestPolarity,
    InterestSignalSource,
    ProfileInterest,
    ProfileInterestSignal,
    ProfileItemScore,
    RankResult,
    SearchHit,
    StoredItem,
    TelegramIdentity,
    User,
    UserProfile,
    UserStatus,
)


class Collector(Protocol):
    """Источник материалов.

    ``until`` — верхняя граница окна, исключающая: сбор идёт сутками, и без
    неё запрос за 30 августа вернул бы всё с 30 августа по сегодня. Параметр
    необязательный: без него источник работает как раньше, «от даты и до
    свежего», и это нормальный режим для разового прогона.
    """

    async def collect(
        self, since: datetime, limit: int, until: datetime | None = None
    ) -> list[CollectedItem]: ...


class Embedder(Protocol):
    @property
    def dimensions(self) -> int: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        """Несколько запросов одной пачкой.

        Грани профиля эмбеддятся вместе: на локальной модели одна пачка из
        пяти коротких текстов заметно дешевле пяти отдельных прогонов, а
        вызывается это на каждый профиль в каждом прогоне диспетчера.
        Реализация по умолчанию честно падает обратно на `embed_query`.
        """
        ...


class Reranker(Protocol):
    async def score(self, query: str, documents: Sequence[str]) -> list[float]: ...


class VectorStore(Protocol):
    async def ensure_collection(self, dimensions: int) -> None: ...

    async def upsert(
        self,
        item_ids: Sequence[UUID],
        vectors: Sequence[Sequence[float]],
        payloads: Sequence[dict[str, object]],
    ) -> None: ...

    async def search(self, vector: Sequence[float], limit: int = 20) -> list[SearchHit]: ...


class ProfileVectorStore(Protocol):
    """Кэш векторов профиля. `facet` — номер грани, 0 — весь профиль.

    Грани версионируются вместе с профилем: правка описания меняет `version`, и
    старые векторы граней перестают находиться, не мешая новым.
    """

    async def ensure_collection(self, dimensions: int) -> None: ...

    async def get(self, profile_id: UUID, version: int, facet: int = 0) -> list[float] | None: ...

    async def upsert(
        self,
        profile_id: UUID,
        version: int,
        vector: Sequence[float],
        facet: int = 0,
    ) -> None: ...

    async def delete(self, profile_id: UUID) -> None: ...


class ProfileExplainer(Protocol):
    async def explain(
        self,
        item: StoredItem,
        *,
        profile_text: str,
        personal_score: float,
    ) -> str: ...


class ItemRepository(Protocol):
    async def save_collected(self, item: CollectedItem) -> tuple[StoredItem, bool]: ...

    async def set_semantic_score(self, item_id: UUID, score: float) -> None: ...

    async def set_rank(self, item_id: UUID, rank: RankResult) -> None: ...

    async def set_analysis(self, item_id: UUID, analysis: DeepAnalysis) -> None: ...

    async def list_digest_candidates(
        self,
        minimum_score: float,
        limit: int,
        since: datetime | None = None,
    ) -> list[StoredItem]: ...

    async def get(self, item_id: UUID) -> StoredItem | None: ...

    async def get_many(self, item_ids: Sequence[UUID]) -> list[StoredItem]:
        """Материалы по списку id одним запросом.

        Реализация по умолчанию честно падает обратно на `get`: адаптеры
        приходят и снаружи, и требовать метод от всех — значит уронить сервис
        на том, у кого его нет.
        """
        ...


class UserProfileRepository(Protocol):
    async def get_or_register(
        self, identity: TelegramIdentity, *, initial_status: UserStatus | str = ...
    ) -> tuple[User, bool]: ...

    async def get_by_telegram(self, telegram_id: int) -> User | None: ...

    async def get_user(self, user_id: UUID) -> User | None: ...

    async def get_active_profile(self, user_id: UUID) -> UserProfile | None: ...

    async def list_profiles(self, user_id: UUID) -> list[UserProfile]: ...

    async def create_profile(
        self,
        user_id: UUID,
        name: str,
        *,
        description: str | None = None,
        compiled_text: str = "",
        is_active: bool = False,
        digest_enabled: bool = False,
        digest_settings: dict[str, object] | None = None,
    ) -> UserProfile: ...

    async def update_profile(
        self,
        user_id: UUID,
        profile_id: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        compiled_text: str | None = None,
        digest_enabled: bool | None = None,
        digest_settings: dict[str, object] | None = None,
    ) -> UserProfile: ...

    async def delete_profile(self, user_id: UUID, profile_id: UUID) -> UserProfile: ...

    async def activate_profile(self, user_id: UUID, profile_id: UUID) -> UserProfile: ...

    async def add_interest(
        self,
        user_id: UUID,
        profile_id: UUID,
        *,
        topic_id: UUID | None = None,
        query: str | None = None,
        polarity: InterestPolarity = InterestPolarity.POSITIVE,
        weight: float = 1,
    ) -> ProfileInterest: ...

    async def remove_interest(self, user_id: UUID, profile_id: UUID, interest_id: UUID) -> None: ...

    async def list_interests(self, user_id: UUID, profile_id: UUID) -> list[ProfileInterest]: ...

    async def upsert_profile_signal(
        self,
        user_id: UUID,
        profile_id: UUID,
        *,
        topic_id: UUID | None = None,
        query: str | None = None,
        polarity: InterestPolarity,
        weight: float,
        source: InterestSignalSource = InterestSignalSource.FEEDBACK,
        source_feedback_id: UUID | None = None,
        evidence_count: int = 1,
        details: dict[str, object] | None = None,
    ) -> ProfileInterestSignal: ...

    async def remove_profile_signal(
        self, user_id: UUID, profile_id: UUID, signal_id: UUID
    ) -> None: ...

    async def list_profile_signals(
        self, user_id: UUID, profile_id: UUID
    ) -> list[ProfileInterestSignal]: ...

    async def upsert_profile_item_score(
        self,
        user_id: UUID,
        profile_id: UUID,
        item_id: UUID,
        *,
        profile_version: int,
        semantic_score: float,
        reranker_score: float,
        global_score: float,
        interest_score: float,
        personal_score: float,
        explanation: str | None = None,
    ) -> ProfileItemScore: ...

    async def list_profile_item_scores(
        self,
        user_id: UUID,
        profile_id: UUID,
        *,
        profile_version: int | None = None,
        limit: int = 100,
    ) -> list[ProfileItemScore]: ...

    async def get_profile_item_score(
        self,
        user_id: UUID,
        score_id: UUID,
    ) -> ProfileItemScore | None: ...

    async def list_digest_enabled_profiles(self) -> list[UserProfile]: ...

    async def create_digest(
        self,
        user_id: UUID,
        profile_id: UUID,
        *,
        period_start: datetime,
        period_end: datetime,
        items: Sequence[tuple[UUID, float, dict[str, object]]],
        payload: dict[str, object] | None = None,
    ) -> UUID: ...

    async def mark_digest_status(
        self,
        digest_id: UUID,
        status: str,
    ) -> None: ...

    async def save_feedback(
        self,
        user_id: UUID,
        item_id: UUID,
        kind: FeedbackKind,
        *,
        profile_id: UUID | None = None,
        context: dict[str, object] | None = None,
    ) -> UUID: ...


class Ranker(Protocol):
    async def rank(self, item: CollectedItem, semantic_score: float) -> RankResult: ...


class Analyzer(Protocol):
    async def analyze(self, item: CollectedItem, rank: RankResult) -> DeepAnalysis: ...
