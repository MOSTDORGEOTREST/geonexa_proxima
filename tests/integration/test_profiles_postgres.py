import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from geonexa_proxima.config import get_settings
from geonexa_proxima.db import (
    SQLAlchemyItemRepository,
    SQLAlchemyUserProfileRepository,
    create_engine,
    create_session_factory,
)
from geonexa_proxima.db.models import (
    ItemModel,
    SubscriberModel,
    SubscriberProfileModel,
)
from geonexa_proxima.domain import CollectedItem, FeedbackKind, ItemKind, SourceName
from geonexa_proxima.services.profiles import UserProfileService

pytestmark = pytest.mark.skipif(
    os.getenv("GEONEXA_RUN_INTEGRATION") != "1",
    reason="set GEONEXA_RUN_INTEGRATION=1 with PostgreSQL migrated to head",
)


@pytest.mark.asyncio
async def test_profile_crud_active_constraint_scores_and_feedback() -> None:
    # Настройки берём из .env: тест ходит в ту же базу, что и приложение.
    settings = get_settings()
    # Режим TLS берём из настроек, а не из умолчания `create_engine`: локальная
    # база под тестом обычно без TLS, а умолчание `prefer` для asyncpg означает
    # обязательное шифрование — тест падал на соединении, не дойдя до проверки.
    engine = create_engine(
        settings.database_url,
        ssl_mode=settings.database_ssl_mode,
        ssl_root_cert=settings.database_ssl_root_cert or None,
    )
    sessions = create_session_factory(engine)
    profile_repository = SQLAlchemyUserProfileRepository(sessions)
    item_repository = SQLAlchemyItemRepository(sessions)
    service = UserProfileService(profile_repository, "geotechnics")
    telegram_id = 900_000_000 + uuid4().int % 99_999_999
    stored = None
    user = None
    try:
        user, default = await service.register_user(telegram_id)
        focused = await service.create_profile(
            user.id,
            "Liquefaction",
            description="ML for soil liquefaction",
            is_active=True,
        )
        profiles = await service.list_profiles(user.id)
        assert sum(profile.is_active for profile in profiles) == 1
        states = {profile.id: profile.is_active for profile in profiles}
        assert states[focused.id] and not states[default.id]

        stored, _ = await item_repository.save_collected(
            CollectedItem(
                source=SourceName.ARXIV,
                external_id=str(uuid4()),
                kind=ItemKind.PAPER,
                title=f"Profile integration {uuid4()}",
            )
        )
        score = await profile_repository.upsert_profile_item_score(
            user.id,
            focused.id,
            stored.id,
            profile_version=focused.version,
            semantic_score=0.8,
            reranker_score=0.9,
            global_score=0.7,
            interest_score=1.0,
            personal_score=0.835,
        )
        resolved = await profile_repository.get_profile_item_score(user.id, score.id)
        assert resolved and resolved.profile_id == focused.id
        feedback_id = await profile_repository.save_feedback(
            user.id,
            stored.id,
            FeedbackKind.USEFUL,
            profile_id=focused.id,
        )
        assert feedback_id

        async with sessions() as session:
            active_count = await session.scalar(
                select(func.count())
                .select_from(SubscriberProfileModel)
                .where(
                    SubscriberProfileModel.subscriber_id == user.id,
                    SubscriberProfileModel.is_active.is_(True),
                )
            )
        assert active_count == 1
    finally:
        async with sessions() as session:
            if user is not None:
                await session.execute(delete(SubscriberModel).where(SubscriberModel.id == user.id))
            if stored is not None:
                await session.execute(delete(ItemModel).where(ItemModel.id == stored.id))
            await session.commit()
        await engine.dispose()
