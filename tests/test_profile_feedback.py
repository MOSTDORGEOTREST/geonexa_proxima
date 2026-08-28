from types import SimpleNamespace
from uuid import uuid4

import pytest

from geonexa_proxima.domain import FeedbackKind, ItemKind, RankResult, StoredItem
from geonexa_proxima.services.feedback import ProfileFeedbackService


class FakeFeedbackRepository:
    def __init__(self) -> None:
        self.saved: dict[str, object] = {}
        self.signal: dict[str, object] = {}

    async def save_feedback(
        self,
        user_id: object,
        item_id: object,
        kind: object,
        **values: object,
    ) -> object:
        self.saved = {
            "user_id": user_id,
            "item_id": item_id,
            "kind": kind,
            **values,
        }
        return uuid4()

    async def list_profile_signals(self, *_: object) -> list[object]:
        return []

    async def upsert_profile_signal(self, *_: object, **values: object) -> object:
        self.signal = values
        return SimpleNamespace()


class FakeProfileService:
    def __init__(self) -> None:
        self.compiled = False

    async def compile_profile(self, *_: object) -> None:
        self.compiled = True


@pytest.mark.asyncio
async def test_feedback_is_attributed_to_message_profile_and_learns_signal() -> None:
    item = StoredItem(
        kind=ItemKind.PAPER,
        title="Paper",
        rank=RankResult(
            relevance=9,
            novelty=8,
            scientific_quality=8,
            practical_value=8,
            importance_for_geotechnics=9,
            importance_for_ai=8,
            reason="Relevant",
            categories=["liquefaction"],
        ),
    )
    item_repository = SimpleNamespace(get=lambda _: None)

    async def get_item(_: object) -> StoredItem:
        return item

    item_repository.get = get_item
    repository = FakeFeedbackRepository()
    profile_service = FakeProfileService()
    service = ProfileFeedbackService(
        item_repository=item_repository,
        profile_repository=repository,  # type: ignore[arg-type]
        profile_service=profile_service,  # type: ignore[arg-type]
    )
    user_id = uuid4()
    message_profile_id = uuid4()

    await service.record(
        user_id=user_id,
        profile_id=message_profile_id,
        item_id=item.id,
        kind=FeedbackKind.VERY_INTERESTING,
    )

    assert repository.saved["profile_id"] == message_profile_id
    assert repository.signal["query"] == "liquefaction"
    assert repository.signal["weight"] == 2
    assert profile_service.compiled is True
