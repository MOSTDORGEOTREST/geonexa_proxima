"""Application service and deterministic compiler for user profiles."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from geonexa_proxima.domain import (
    FeedbackKind,
    InterestPolarity,
    InterestSignalSource,
    ProfileInterest,
    ProfileInterestSignal,
    TelegramIdentity,
    User,
    UserProfile,
    UserStatus,
)
from geonexa_proxima.ports import UserProfileRepository


class ProfileCompiler:
    """Compile profile inputs into stable text suitable for embedding and reranking."""

    def __init__(self, base_taxonomy_text: str) -> None:
        self._base_taxonomy_text = base_taxonomy_text.strip()

    def compile_profile(
        self,
        *,
        description: str | None,
        interests: Sequence[ProfileInterest],
        learned_signals: Sequence[ProfileInterestSignal],
    ) -> str:
        sections: list[str] = []
        if self._base_taxonomy_text:
            sections.append(f"Base taxonomy:\n{self._base_taxonomy_text}")
        if description and description.strip():
            sections.append(f"Profile description:\n{description.strip()}")

        explicit_lines = [
            self._explicit_line(interest)
            for interest in sorted(interests, key=self._interest_sort_key)
        ]
        if explicit_lines:
            sections.append("Explicit interests:\n" + "\n".join(explicit_lines))

        learned_lines = [
            self._signal_line(signal)
            for signal in sorted(learned_signals, key=self._signal_sort_key)
        ]
        if learned_lines:
            sections.append("Learned interest signals:\n" + "\n".join(learned_lines))
        return "\n\n".join(sections)

    @staticmethod
    def _explicit_line(interest: ProfileInterest) -> str:
        return f"- {interest.polarity.value}: {interest.target_text} (weight={interest.weight:g})"

    @staticmethod
    def _signal_line(signal: ProfileInterestSignal) -> str:
        return (
            f"- {signal.polarity.value}: {signal.target_text} "
            f"(weight={signal.weight:g}, evidence={signal.evidence_count}, "
            f"source={signal.source.value})"
        )

    @staticmethod
    def _interest_sort_key(interest: ProfileInterest) -> tuple[str, str, float, str]:
        return (
            interest.polarity.value,
            interest.target_text.casefold(),
            -interest.weight,
            str(interest.id),
        )

    @staticmethod
    def _signal_sort_key(
        signal: ProfileInterestSignal,
    ) -> tuple[str, str, float, str]:
        return (
            signal.polarity.value,
            signal.target_text.casefold(),
            -signal.weight,
            str(signal.id),
        )


def compile_profile(
    base_taxonomy_text: str,
    *,
    description: str | None = None,
    interests: Sequence[ProfileInterest] = (),
    learned_signals: Sequence[ProfileInterestSignal] = (),
) -> str:
    """Functional facade for callers that do not need a long-lived compiler."""

    return ProfileCompiler(base_taxonomy_text).compile_profile(
        description=description,
        interests=interests,
        learned_signals=learned_signals,
    )


class UserProfileService:
    """Coordinate registration, profile CRUD and versioned profile compilation."""

    def __init__(
        self,
        repository: UserProfileRepository,
        base_taxonomy_text: str,
        *,
        default_profile_name: str = "Default",
    ) -> None:
        self._repository = repository
        self._compiler = ProfileCompiler(base_taxonomy_text)
        self._default_profile_name = default_profile_name

    async def register_user(
        self,
        telegram_id: int,
        *,
        username: str | None = None,
        display_name: str | None = None,
        language_code: str | None = None,
        initial_status: UserStatus | str = UserStatus.PENDING,
    ) -> tuple[User, UserProfile]:
        return await self.get_or_register(
            TelegramIdentity(
                telegram_id=telegram_id,
                username=username,
                display_name=display_name,
                language_code=language_code,
            ),
            initial_status=initial_status,
        )

    async def get_or_register(
        self,
        identity: TelegramIdentity,
        *,
        initial_status: UserStatus | str = UserStatus.PENDING,
    ) -> tuple[User, UserProfile]:
        """Подписчик и его активный профиль; новый заводится неактивным.

        Профиль создаётся сразу, даже пока подписчик ждёт подтверждения:
        администратору нужно куда-то записать интересы до того, как он нажмёт
        «Подтвердить», а не после.
        """

        user, _ = await self._repository.get_or_register(identity, initial_status=initial_status)
        return user, await self.ensure_profile(user.id)

    async def ensure_profile(self, user_id: UUID) -> UserProfile:
        """Активный профиль подписчика, создав его при необходимости.

        Нужен там, где подписчик появился не через `/start`: группа и канал
        заводятся апдейтом `my_chat_member`, и профиля у них нет. А редактировать
        администратору нужно что-то уже существующее — пустая карточка без
        профиля выглядит как поломка, а не как «ещё не заполнено».
        """

        profile = await self._repository.get_active_profile(user_id)
        if profile is not None:
            return profile
        return await self._repository.create_profile(
            user_id,
            self._default_profile_name,
            compiled_text=self._compiler.compile_profile(
                description=None, interests=(), learned_signals=()
            ),
            is_active=True,
        )

    async def list_profiles(self, user_id: UUID) -> list[UserProfile]:
        return await self._repository.list_profiles(user_id)

    async def create_profile(
        self,
        user_id: UUID,
        name: str,
        *,
        description: str | None = None,
        is_active: bool = False,
        digest_enabled: bool = False,
        digest_settings: dict[str, object] | None = None,
    ) -> UserProfile:
        compiled_text = self._compiler.compile_profile(
            description=description,
            interests=(),
            learned_signals=(),
        )
        return await self._repository.create_profile(
            user_id,
            name,
            description=description,
            compiled_text=compiled_text,
            is_active=is_active,
            digest_enabled=digest_enabled,
            digest_settings=digest_settings,
        )

    async def update_profile(
        self,
        user_id: UUID,
        profile_id: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        digest_enabled: bool | None = None,
        digest_settings: dict[str, object] | None = None,
    ) -> UserProfile:
        current = await self._get_profile(user_id, profile_id)
        effective_description = current.description if description is None else description
        interests = await self._repository.list_interests(user_id, profile_id)
        signals = await self._repository.list_profile_signals(user_id, profile_id)
        compiled_text = self._compiler.compile_profile(
            description=effective_description,
            interests=interests,
            learned_signals=signals,
        )
        return await self._repository.update_profile(
            user_id,
            profile_id,
            name=name,
            description=description,
            compiled_text=compiled_text,
            digest_enabled=digest_enabled,
            digest_settings=digest_settings,
        )

    async def delete_profile(self, user_id: UUID, profile_id: UUID) -> UserProfile:
        return await self._repository.delete_profile(user_id, profile_id)

    async def activate_profile(self, user_id: UUID, profile_id: UUID) -> UserProfile:
        """Delegate the complete atomic switch to the repository transaction."""

        return await self._repository.activate_profile(user_id, profile_id)

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
        interest = await self._repository.add_interest(
            user_id,
            profile_id,
            topic_id=topic_id,
            query=query,
            polarity=polarity,
            weight=weight,
        )
        await self.compile_profile(user_id, profile_id)
        return interest

    async def remove_interest(
        self,
        user_id: UUID,
        profile_id: UUID,
        interest_id: UUID,
    ) -> None:
        await self._repository.remove_interest(user_id, profile_id, interest_id)
        await self.compile_profile(user_id, profile_id)

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
        signal = await self._repository.upsert_profile_signal(
            user_id,
            profile_id,
            topic_id=topic_id,
            query=query,
            polarity=polarity,
            weight=weight,
            source=source,
            source_feedback_id=source_feedback_id,
            evidence_count=evidence_count,
            details=details,
        )
        await self.compile_profile(user_id, profile_id)
        return signal

    async def remove_profile_signal(
        self,
        user_id: UUID,
        profile_id: UUID,
        signal_id: UUID,
    ) -> None:
        await self._repository.remove_profile_signal(user_id, profile_id, signal_id)
        await self.compile_profile(user_id, profile_id)

    async def compile_profile(self, user_id: UUID, profile_id: UUID) -> UserProfile:
        profile = await self._get_profile(user_id, profile_id)
        interests = await self._repository.list_interests(user_id, profile_id)
        signals = await self._repository.list_profile_signals(user_id, profile_id)
        compiled_text = self._compiler.compile_profile(
            description=profile.description,
            interests=interests,
            learned_signals=signals,
        )
        return await self._repository.update_profile(
            user_id,
            profile_id,
            compiled_text=compiled_text,
        )

    async def save_feedback(
        self,
        user_id: UUID,
        item_id: UUID,
        kind: FeedbackKind,
        *,
        profile_id: UUID | None = None,
        context: dict[str, object] | None = None,
    ) -> UUID:
        return await self._repository.save_feedback(
            user_id,
            item_id,
            kind,
            profile_id=profile_id,
            context=context,
        )

    async def _get_profile(self, user_id: UUID, profile_id: UUID) -> UserProfile:
        profiles = await self._repository.list_profiles(user_id)
        for profile in profiles:
            if profile.id == profile_id:
                return profile
        raise LookupError(f"profile {profile_id} not found for user {user_id}")


__all__ = ["ProfileCompiler", "UserProfileService", "compile_profile"]
