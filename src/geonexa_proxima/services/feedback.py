"""Profile-attributed feedback and explicit, reversible learning signals."""

from __future__ import annotations

from uuid import UUID

from geonexa_proxima.domain import FeedbackKind, InterestPolarity
from geonexa_proxima.ports import ItemRepository, UserProfileRepository
from geonexa_proxima.services.profiles import UserProfileService

_FEEDBACK_EFFECTS: dict[FeedbackKind, tuple[InterestPolarity, float] | None] = {
    FeedbackKind.VERY_INTERESTING: (InterestPolarity.POSITIVE, 2.0),
    FeedbackKind.USEFUL: (InterestPolarity.POSITIVE, 1.0),
    FeedbackKind.NOT_INTERESTING: (InterestPolarity.NEGATIVE, 2.0),
    FeedbackKind.SAVE: None,
    FeedbackKind.DEEPER: (InterestPolarity.POSITIVE, 1.0),
}


class ProfileFeedbackService:
    def __init__(
        self,
        *,
        item_repository: ItemRepository,
        profile_repository: UserProfileRepository,
        profile_service: UserProfileService,
    ) -> None:
        self.item_repository = item_repository
        self.profile_repository = profile_repository
        self.profile_service = profile_service

    async def record(
        self,
        *,
        user_id: UUID,
        profile_id: UUID,
        item_id: UUID,
        kind: FeedbackKind,
        context: dict[str, object] | None = None,
    ) -> UUID:
        feedback_id = await self.profile_repository.save_feedback(
            user_id,
            item_id,
            kind,
            profile_id=profile_id,
            context=context,
        )
        effect = _FEEDBACK_EFFECTS[kind]
        if effect is None:
            return feedback_id

        item = await self.item_repository.get(item_id)
        if item is None or item.rank is None:
            return feedback_id
        polarity, increment = effect
        existing = await self.profile_repository.list_profile_signals(user_id, profile_id)
        by_query = {signal.query.casefold(): signal for signal in existing if signal.query}
        for category in item.rank.categories[:5]:
            query = category.strip()
            if not query:
                continue
            signal = by_query.get(query.casefold())
            await self.profile_repository.upsert_profile_signal(
                user_id,
                profile_id,
                query=query,
                polarity=polarity,
                weight=min(10.0, (signal.weight if signal else 0) + increment),
                source_feedback_id=feedback_id,
                evidence_count=(signal.evidence_count if signal else 0) + 1,
                details={"last_item_id": str(item_id), "last_feedback_kind": kind.value},
            )
        await self.profile_service.compile_profile(user_id, profile_id)
        return feedback_id
