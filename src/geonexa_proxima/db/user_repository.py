"""Async PostgreSQL repository for users and personalized profiles."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from geonexa_proxima.db.models import (
    DigestItemModel,
    DigestModel,
    FeedbackModel,
    ItemModel,
    ProfileInterestModel,
    ProfileInterestSignalModel,
    ProfileItemScoreModel,
    TopicModel,
    UserModel,
    UserProfileModel,
)
from geonexa_proxima.db.session import SessionFactory
from geonexa_proxima.domain import (
    FeedbackKind,
    InterestPolarity,
    InterestSignalSource,
    ProfileInterest,
    ProfileInterestSignal,
    ProfileItemScore,
    TelegramIdentity,
    User,
    UserProfile,
    UserStatus,
)

_WHITESPACE = re.compile(r"\s+")


class UserNotFoundError(LookupError):
    """The requested application user does not exist."""


class ProfileNotFoundError(LookupError):
    """The requested profile does not belong to the requested user."""


class InterestNotFoundError(LookupError):
    """The requested explicit or learned interest does not exist."""


class FinalProfileDeletionError(ValueError):
    """A user must always retain at least one profile."""


def normalize_profile_name(value: str) -> str:
    """Normalize profile names for stable per-user uniqueness."""

    return _WHITESPACE.sub(
        " ",
        unicodedata.normalize("NFKC", value).strip(),
    ).casefold()


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).strip())
    return cleaned or None


def _validate_target(topic_id: UUID | None, query: str | None) -> str | None:
    cleaned_query = _clean_optional(query)
    if (topic_id is None) == (cleaned_query is None):
        raise ValueError("exactly one of topic_id or query is required")
    return cleaned_query


def _validate_weight(value: float) -> float:
    if not math.isfinite(value) or not 0 <= value <= 10:
        raise ValueError("interest weight must be finite and between 0 and 10")
    return value


def _validate_score(value: float, name: str) -> float:
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be finite and between 0 and 1")
    return value


def _json_payload(value: dict[str, object] | None) -> dict[str, object]:
    return dict(value or {})


class SQLAlchemyUserProfileRepository:
    """Transactional profile repository backed by the application's PostgreSQL DB."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get_or_register(self, identity: TelegramIdentity) -> tuple[User, bool]:
        values = {
            "external_user_id": identity.telegram_id,
            "telegram_username": _clean_optional(identity.username),
            "display_name": _clean_optional(identity.display_name),
            "language_code": _clean_optional(identity.language_code),
        }
        async with self._session_factory() as session, session.begin():
            statement = (
                insert(UserModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[UserModel.external_user_id])
                .returning(UserModel)
            )
            model = (await session.scalars(statement)).one_or_none()
            created = model is not None
            if model is None:
                model = await session.scalar(
                    select(UserModel)
                    .where(UserModel.external_user_id == identity.telegram_id)
                    .with_for_update()
                )
                if model is None:
                    raise UserNotFoundError(
                        f"Telegram user {identity.telegram_id} disappeared during registration"
                    )
                model.telegram_username = values["telegram_username"]
                model.display_name = values["display_name"]
                model.language_code = values["language_code"]
                now = datetime.now(UTC)
                model.last_seen_at = now
                model.updated_at = now
                await session.flush()
        return self._to_user(model), created

    async def get_by_telegram(self, telegram_id: int) -> User | None:
        if telegram_id <= 0:
            raise ValueError("telegram_id must be positive")
        async with self._session_factory() as session:
            model = await session.scalar(
                select(UserModel).where(UserModel.external_user_id == telegram_id)
            )
            return self._to_user(model) if model else None

    async def get_user(self, user_id: UUID) -> User | None:
        async with self._session_factory() as session:
            model = await session.get(UserModel, user_id)
            return self._to_user(model) if model else None

    async def get_active_profile(self, user_id: UUID) -> UserProfile | None:
        async with self._session_factory() as session:
            model = await session.scalar(
                select(UserProfileModel).where(
                    UserProfileModel.user_id == user_id,
                    UserProfileModel.is_active.is_(True),
                )
            )
            return self._to_profile(model) if model else None

    async def list_profiles(self, user_id: UUID) -> list[UserProfile]:
        async with self._session_factory() as session:
            models = (
                await session.scalars(
                    select(UserProfileModel)
                    .where(UserProfileModel.user_id == user_id)
                    .order_by(
                        UserProfileModel.is_active.desc(),
                        UserProfileModel.created_at,
                        UserProfileModel.id,
                    )
                )
            ).all()
            return [self._to_profile(model) for model in models]

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
    ) -> UserProfile:
        cleaned_name = _clean_optional(name)
        normalized_name = normalize_profile_name(name)
        if not cleaned_name or not normalized_name:
            raise ValueError("profile name cannot be empty")

        async with self._session_factory() as session, session.begin():
            await self._lock_user(session, user_id)
            profile_count = await session.scalar(
                select(func.count())
                .select_from(UserProfileModel)
                .where(UserProfileModel.user_id == user_id)
            )
            activate = is_active or profile_count == 0
            if activate:
                await self._deactivate_profiles(session, user_id)
            model = UserProfileModel(
                user_id=user_id,
                name=cleaned_name,
                normalized_name=normalized_name,
                description=_clean_optional(description),
                compiled_text=compiled_text.strip(),
                version=1,
                is_active=activate,
                digest_enabled=digest_enabled,
                digest_settings=_json_payload(digest_settings),
            )
            session.add(model)
            await session.flush()
        return self._to_profile(model)

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
    ) -> UserProfile:
        async with self._session_factory() as session, session.begin():
            model = await self._owned_profile(
                session,
                user_id,
                profile_id,
                for_update=True,
            )
            changed = False
            if name is not None:
                cleaned_name = _clean_optional(name)
                if not cleaned_name:
                    raise ValueError("profile name cannot be empty")
                normalized_name = normalize_profile_name(cleaned_name)
                if model.name != cleaned_name or model.normalized_name != normalized_name:
                    model.name = cleaned_name
                    model.normalized_name = normalized_name
                    changed = True
            if description is not None:
                cleaned_description = _clean_optional(description)
                if model.description != cleaned_description:
                    model.description = cleaned_description
                    changed = True
            if compiled_text is not None:
                cleaned_compiled_text = compiled_text.strip()
                if model.compiled_text != cleaned_compiled_text:
                    model.compiled_text = cleaned_compiled_text
                    model.version += 1
                    changed = True
            if digest_enabled is not None and model.digest_enabled != digest_enabled:
                model.digest_enabled = digest_enabled
                changed = True
            if digest_settings is not None:
                settings = _json_payload(digest_settings)
                if model.digest_settings != settings:
                    model.digest_settings = settings
                    changed = True
            if changed:
                model.updated_at = datetime.now(UTC)
                await session.flush()
        return self._to_profile(model)

    async def delete_profile(self, user_id: UUID, profile_id: UUID) -> UserProfile:
        async with self._session_factory() as session, session.begin():
            await self._lock_user(session, user_id)
            profiles = (
                await session.scalars(
                    select(UserProfileModel)
                    .where(UserProfileModel.user_id == user_id)
                    .order_by(UserProfileModel.created_at, UserProfileModel.id)
                    .with_for_update()
                )
            ).all()
            target = next((profile for profile in profiles if profile.id == profile_id), None)
            if target is None:
                raise ProfileNotFoundError(f"profile {profile_id} not found for user {user_id}")
            if len(profiles) == 1:
                raise FinalProfileDeletionError("cannot delete a user's final profile")

            remaining = [profile for profile in profiles if profile.id != profile_id]
            active = next((profile for profile in remaining if profile.is_active), None)
            if target.is_active or active is None:
                target.is_active = False
                await session.flush()
                active = remaining[0]
                active.is_active = True
                active.updated_at = datetime.now(UTC)
            await session.delete(target)
            await session.flush()
        return self._to_profile(active)

    async def activate_profile(self, user_id: UUID, profile_id: UUID) -> UserProfile:
        async with self._session_factory() as session, session.begin():
            await self._lock_user(session, user_id)
            model = await self._owned_profile(
                session,
                user_id,
                profile_id,
                for_update=True,
            )
            await self._deactivate_profiles(session, user_id)
            await session.execute(
                update(UserProfileModel)
                .where(UserProfileModel.id == profile_id)
                .values(is_active=True, updated_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
            await session.refresh(model)
        return self._to_profile(model)

    async def add_interest(
        self,
        user_id: UUID,
        profile_id: UUID,
        *,
        topic_id: UUID | None = None,
        query: str | None = None,
        polarity: InterestPolarity = InterestPolarity.POSITIVE,
        weight: float = 1,
    ) -> ProfileInterest:
        cleaned_query = _validate_target(topic_id, query)
        _validate_weight(weight)
        async with self._session_factory() as session, session.begin():
            await self._owned_profile(session, user_id, profile_id, for_update=True)
            model = await self._find_interest(session, profile_id, topic_id, cleaned_query)
            if model is None:
                model = ProfileInterestModel(
                    profile_id=profile_id,
                    topic_id=topic_id,
                    query=cleaned_query,
                    polarity=polarity.value,
                    weight=weight,
                )
                session.add(model)
            else:
                model.polarity = polarity.value
                model.weight = weight
                model.updated_at = datetime.now(UTC)
            await session.flush()
            topic_name = await self._topic_name(session, topic_id)
        return self._to_interest(model, topic_name)

    async def remove_interest(
        self,
        user_id: UUID,
        profile_id: UUID,
        interest_id: UUID,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await self._owned_profile(session, user_id, profile_id, for_update=True)
            model = await session.scalar(
                select(ProfileInterestModel).where(
                    ProfileInterestModel.id == interest_id,
                    ProfileInterestModel.profile_id == profile_id,
                )
            )
            if model is None:
                raise InterestNotFoundError(f"interest {interest_id} not found")
            await session.delete(model)

    async def list_interests(
        self,
        user_id: UUID,
        profile_id: UUID,
    ) -> list[ProfileInterest]:
        async with self._session_factory() as session:
            await self._owned_profile(session, user_id, profile_id)
            rows = (
                await session.execute(
                    select(ProfileInterestModel, TopicModel.name)
                    .outerjoin(TopicModel, TopicModel.id == ProfileInterestModel.topic_id)
                    .where(ProfileInterestModel.profile_id == profile_id)
                    .order_by(
                        ProfileInterestModel.polarity,
                        ProfileInterestModel.weight.desc(),
                        ProfileInterestModel.created_at,
                        ProfileInterestModel.id,
                    )
                )
            ).all()
            return [self._to_interest(model, topic_name) for model, topic_name in rows]

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
    ) -> ProfileInterestSignal:
        cleaned_query = _validate_target(topic_id, query)
        _validate_weight(weight)
        if evidence_count < 1:
            raise ValueError("evidence_count must be positive")
        async with self._session_factory() as session, session.begin():
            await self._owned_profile(session, user_id, profile_id, for_update=True)
            model = await self._find_signal(session, profile_id, topic_id, cleaned_query)
            if model is None:
                model = ProfileInterestSignalModel(
                    profile_id=profile_id,
                    topic_id=topic_id,
                    query=cleaned_query,
                    polarity=polarity.value,
                    weight=weight,
                    source=source.value,
                    source_feedback_id=source_feedback_id,
                    evidence_count=evidence_count,
                    details=_json_payload(details),
                )
                session.add(model)
            else:
                model.polarity = polarity.value
                model.weight = weight
                model.source = source.value
                model.source_feedback_id = source_feedback_id
                model.evidence_count = evidence_count
                model.details = _json_payload(details)
                model.updated_at = datetime.now(UTC)
            await session.flush()
            topic_name = await self._topic_name(session, topic_id)
        return self._to_signal(model, topic_name)

    async def remove_profile_signal(
        self,
        user_id: UUID,
        profile_id: UUID,
        signal_id: UUID,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await self._owned_profile(session, user_id, profile_id, for_update=True)
            model = await session.scalar(
                select(ProfileInterestSignalModel).where(
                    ProfileInterestSignalModel.id == signal_id,
                    ProfileInterestSignalModel.profile_id == profile_id,
                )
            )
            if model is None:
                raise InterestNotFoundError(f"profile signal {signal_id} not found")
            await session.delete(model)

    async def list_profile_signals(
        self,
        user_id: UUID,
        profile_id: UUID,
    ) -> list[ProfileInterestSignal]:
        async with self._session_factory() as session:
            await self._owned_profile(session, user_id, profile_id)
            rows = (
                await session.execute(
                    select(ProfileInterestSignalModel, TopicModel.name)
                    .outerjoin(TopicModel, TopicModel.id == ProfileInterestSignalModel.topic_id)
                    .where(ProfileInterestSignalModel.profile_id == profile_id)
                    .order_by(
                        ProfileInterestSignalModel.polarity,
                        ProfileInterestSignalModel.weight.desc(),
                        ProfileInterestSignalModel.created_at,
                        ProfileInterestSignalModel.id,
                    )
                )
            ).all()
            return [self._to_signal(model, topic_name) for model, topic_name in rows]

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
    ) -> ProfileItemScore:
        scores = {
            "semantic_score": _validate_score(semantic_score, "semantic_score"),
            "reranker_score": _validate_score(reranker_score, "reranker_score"),
            "global_score": _validate_score(global_score, "global_score"),
            "interest_score": _validate_score(interest_score, "interest_score"),
            "personal_score": _validate_score(personal_score, "personal_score"),
        }
        if profile_version < 1:
            raise ValueError("profile_version must be positive")

        async with self._session_factory() as session, session.begin():
            profile = await self._owned_profile(session, user_id, profile_id)
            if profile_version > profile.version:
                raise ValueError("profile_version cannot be newer than the stored profile")
            if await session.get(ItemModel, item_id) is None:
                raise LookupError(f"item {item_id} not found")
            statement = insert(ProfileItemScoreModel).values(
                profile_id=profile_id,
                item_id=item_id,
                profile_version=profile_version,
                explanation=_clean_optional(explanation),
                **scores,
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_profile_item_scores_profile_id_item_id_profile_version",
                set_={
                    **scores,
                    "explanation": statement.excluded.explanation,
                    "updated_at": func.now(),
                },
            ).returning(ProfileItemScoreModel)
            model = (await session.scalars(statement)).one()
        return self._to_item_score(model)

    async def list_profile_item_scores(
        self,
        user_id: UUID,
        profile_id: UUID,
        *,
        profile_version: int | None = None,
        limit: int = 100,
    ) -> list[ProfileItemScore]:
        if limit < 1:
            raise ValueError("limit must be positive")
        async with self._session_factory() as session:
            await self._owned_profile(session, user_id, profile_id)
            statement = select(ProfileItemScoreModel).where(
                ProfileItemScoreModel.profile_id == profile_id
            )
            if profile_version is not None:
                statement = statement.where(
                    ProfileItemScoreModel.profile_version == profile_version
                )
            models = (
                await session.scalars(
                    statement.order_by(
                        ProfileItemScoreModel.personal_score.desc(),
                        ProfileItemScoreModel.updated_at.desc(),
                        ProfileItemScoreModel.item_id,
                    ).limit(limit)
                )
            ).all()
            return [self._to_item_score(model) for model in models]

    async def get_profile_item_score(
        self,
        user_id: UUID,
        score_id: UUID,
    ) -> ProfileItemScore | None:
        async with self._session_factory() as session:
            model = await session.scalar(
                select(ProfileItemScoreModel)
                .join(
                    UserProfileModel,
                    UserProfileModel.id == ProfileItemScoreModel.profile_id,
                )
                .where(
                    ProfileItemScoreModel.id == score_id,
                    UserProfileModel.user_id == user_id,
                )
            )
            return self._to_item_score(model) if model else None

    async def list_digest_enabled_profiles(self) -> list[UserProfile]:
        async with self._session_factory() as session:
            models = (
                await session.scalars(
                    select(UserProfileModel)
                    .join(UserModel, UserModel.id == UserProfileModel.user_id)
                    .where(
                        UserProfileModel.digest_enabled.is_(True),
                        UserModel.status == UserStatus.ACTIVE.value,
                    )
                    .order_by(UserProfileModel.user_id, UserProfileModel.id)
                )
            ).all()
            return [self._to_profile(model) for model in models]

    async def create_digest(
        self,
        user_id: UUID,
        profile_id: UUID,
        *,
        period_start: datetime,
        period_end: datetime,
        items: Sequence[tuple[UUID, float, dict[str, object]]],
        payload: dict[str, object] | None = None,
    ) -> UUID:
        if period_end <= period_start:
            raise ValueError("digest period_end must be after period_start")
        async with self._session_factory() as session, session.begin():
            await self._owned_profile(session, user_id, profile_id)
            digest = DigestModel(
                user_id=user_id,
                profile_id=profile_id,
                period_start=period_start,
                period_end=period_end,
                status="ready",
                payload=_json_payload(payload),
            )
            session.add(digest)
            await session.flush()
            session.add_all(
                [
                    DigestItemModel(
                        digest_id=digest.id,
                        item_id=item_id,
                        position=position,
                        score_snapshot=score,
                        explanation=explanation,
                    )
                    for position, (item_id, score, explanation) in enumerate(items)
                ]
            )
            await session.flush()
            return digest.id

    async def mark_digest_status(self, digest_id: UUID, status: str) -> None:
        allowed = {"pending", "building", "ready", "sent", "failed"}
        if status not in allowed:
            raise ValueError(f"unsupported digest status: {status}")
        values: dict[str, object] = {"status": status}
        if status == "sent":
            values["sent_at"] = func.now()
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(DigestModel).where(DigestModel.id == digest_id).values(**values)
            )
            if result.rowcount != 1:
                raise LookupError(f"digest {digest_id} not found")

    async def save_feedback(
        self,
        user_id: UUID,
        item_id: UUID,
        kind: FeedbackKind,
        *,
        profile_id: UUID | None = None,
        context: dict[str, object] | None = None,
    ) -> UUID:
        async with self._session_factory() as session, session.begin():
            await self._lock_user(session, user_id)
            if await session.get(ItemModel, item_id) is None:
                raise LookupError(f"item {item_id} not found")
            if profile_id is None:
                profile = await session.scalar(
                    select(UserProfileModel).where(
                        UserProfileModel.user_id == user_id,
                        UserProfileModel.is_active.is_(True),
                    )
                )
                if profile is None:
                    raise ProfileNotFoundError(f"user {user_id} has no active profile")
            else:
                profile = await self._owned_profile(session, user_id, profile_id)
            model = FeedbackModel(
                user_id=user_id,
                profile_id=profile.id,
                item_id=item_id,
                kind=kind.value,
                context=_json_payload(context),
            )
            session.add(model)
            await session.flush()
            return model.id

    @staticmethod
    async def _lock_user(session: AsyncSession, user_id: UUID) -> UserModel:
        model = await session.get(UserModel, user_id, with_for_update=True)
        if model is None:
            raise UserNotFoundError(f"user {user_id} not found")
        return model

    @staticmethod
    async def _owned_profile(
        session: AsyncSession,
        user_id: UUID,
        profile_id: UUID,
        *,
        for_update: bool = False,
    ) -> UserProfileModel:
        statement = select(UserProfileModel).where(
            UserProfileModel.id == profile_id,
            UserProfileModel.user_id == user_id,
        )
        if for_update:
            statement = statement.with_for_update()
        model = await session.scalar(statement)
        if model is None:
            raise ProfileNotFoundError(f"profile {profile_id} not found for user {user_id}")
        return model

    @staticmethod
    async def _deactivate_profiles(session: AsyncSession, user_id: UUID) -> None:
        await session.execute(
            update(UserProfileModel)
            .where(
                UserProfileModel.user_id == user_id,
                UserProfileModel.is_active.is_(True),
            )
            .values(is_active=False, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )

    @staticmethod
    async def _find_interest(
        session: AsyncSession,
        profile_id: UUID,
        topic_id: UUID | None,
        query: str | None,
    ) -> ProfileInterestModel | None:
        statement = select(ProfileInterestModel).where(
            ProfileInterestModel.profile_id == profile_id
        )
        if topic_id is not None:
            statement = statement.where(ProfileInterestModel.topic_id == topic_id)
        else:
            statement = statement.where(ProfileInterestModel.query == query)
        return await session.scalar(statement.with_for_update())

    @staticmethod
    async def _find_signal(
        session: AsyncSession,
        profile_id: UUID,
        topic_id: UUID | None,
        query: str | None,
    ) -> ProfileInterestSignalModel | None:
        statement = select(ProfileInterestSignalModel).where(
            ProfileInterestSignalModel.profile_id == profile_id
        )
        if topic_id is not None:
            statement = statement.where(ProfileInterestSignalModel.topic_id == topic_id)
        else:
            statement = statement.where(ProfileInterestSignalModel.query == query)
        return await session.scalar(statement.with_for_update())

    @staticmethod
    async def _topic_name(session: AsyncSession, topic_id: UUID | None) -> str | None:
        if topic_id is None:
            return None
        return await session.scalar(select(TopicModel.name).where(TopicModel.id == topic_id))

    @staticmethod
    def _to_user(model: UserModel) -> User:
        return User.model_validate(model)

    @staticmethod
    def _to_profile(model: UserProfileModel) -> UserProfile:
        return UserProfile.model_validate(model)

    @staticmethod
    def _to_interest(model: ProfileInterestModel, topic_name: str | None) -> ProfileInterest:
        return ProfileInterest(
            id=model.id,
            profile_id=model.profile_id,
            topic_id=model.topic_id,
            topic_name=topic_name,
            query=model.query,
            polarity=model.polarity,
            weight=model.weight,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @staticmethod
    def _to_signal(
        model: ProfileInterestSignalModel,
        topic_name: str | None,
    ) -> ProfileInterestSignal:
        return ProfileInterestSignal(
            id=model.id,
            profile_id=model.profile_id,
            topic_id=model.topic_id,
            topic_name=topic_name,
            query=model.query,
            polarity=model.polarity,
            weight=model.weight,
            source=model.source,
            source_feedback_id=model.source_feedback_id,
            evidence_count=model.evidence_count,
            details=model.details,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @staticmethod
    def _to_item_score(model: ProfileItemScoreModel) -> ProfileItemScore:
        return ProfileItemScore.model_validate(model)


__all__ = [
    "FinalProfileDeletionError",
    "InterestNotFoundError",
    "ProfileNotFoundError",
    "SQLAlchemyUserProfileRepository",
    "UserNotFoundError",
    "normalize_profile_name",
]
