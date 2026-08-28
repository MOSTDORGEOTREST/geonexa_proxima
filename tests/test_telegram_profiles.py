from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from geonexa_proxima.domain import UserProfile
from geonexa_proxima.telegram.bot import _item_keyboard, _resolve_profile


class FakeProfileService:
    def __init__(self, profiles: list[UserProfile]) -> None:
        self.profiles = profiles

    async def list_profiles(self, _: object) -> list[UserProfile]:
        return self.profiles


def _profile(name: str) -> UserProfile:
    now = datetime.now(UTC)
    return UserProfile(
        id=uuid4(),
        user_id=uuid4(),
        name=name,
        normalized_name=name.casefold(),
        compiled_text="profile",
        version=1,
        is_active=True,
        digest_enabled=False,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_profile_selector_accepts_normalized_name_and_uuid() -> None:
    profile = _profile("Liquefaction")
    container = SimpleNamespace(profile_service=lambda: FakeProfileService([profile]))

    assert await _resolve_profile(container, profile.user_id, "liquefaction") == profile
    assert await _resolve_profile(container, profile.user_id, str(profile.id)) == profile


def test_feedback_keyboard_uses_compact_profile_score_reference() -> None:
    score_id = uuid4()
    keyboard = _item_keyboard(score_id)
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]

    assert all(str(score_id) in callback for callback in callbacks)
    assert all(len(callback.encode()) <= 64 for callback in callbacks)
