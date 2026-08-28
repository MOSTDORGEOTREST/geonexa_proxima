"""Prefect flow for one independently ranked digest per enabled profile."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from prefect import flow

from geonexa_proxima.services.container import load_container
from geonexa_proxima.telegram.bot import _item_keyboard


@flow(name="geonexa-personal-digests", log_prints=True)
async def personal_digests_flow(
    *,
    bootstrap_target: str | None = None,
    deliver: bool = True,
) -> dict[str, int]:
    """Build every enabled profile separately and optionally deliver via Telegram."""

    container = load_container(target=bootstrap_target)
    bot = (
        Bot(
            token=container.settings.telegram_bot_token.get_secret_value(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        if deliver
        else None
    )
    stats = {"profiles": 0, "digests": 0, "sent": 0, "failed": 0, "items": 0}
    try:
        profiles = await container.profile_repository.list_digest_enabled_profiles()
        stats["profiles"] = len(profiles)
        for profile in profiles:
            digest_id = None
            try:
                user = await container.profile_repository.get_user(profile.user_id)
                if user is None:
                    raise LookupError(f"user {profile.user_id} not found")
                lookback_hours = _integer_setting(
                    profile.digest_settings,
                    "lookback_hours",
                    24,
                    minimum=1,
                    maximum=720,
                )
                limit = _integer_setting(
                    profile.digest_settings,
                    "limit",
                    20,
                    minimum=1,
                    maximum=100,
                )
                period_end = datetime.now(UTC)
                period_start = period_end - timedelta(hours=lookback_hours)
                candidates = await container.digest_builder().list_personalized(
                    profile,
                    limit=limit,
                    since=period_start,
                    minimum_global_score=container.settings.digest_score_threshold,
                )
                digest_id = await container.profile_repository.create_digest(
                    user.id,
                    profile.id,
                    period_start=period_start,
                    period_end=period_end,
                    items=[
                        (
                            candidate.item.id,
                            candidate.personal_score,
                            {
                                "profile_score_id": str(candidate.profile_score_id),
                                "reason": candidate.explanation,
                            },
                        )
                        for candidate in candidates
                    ],
                    payload={
                        "profile_name": profile.name,
                        "profile_version": profile.version,
                        "candidate_count": len(candidates),
                    },
                )
                stats["digests"] += 1
                stats["items"] += len(candidates)
                if bot is None:
                    continue
                await bot.send_message(
                    user.telegram_id,
                    f"<b>GeoNexa digest · {escape(profile.name)}</b>",
                )
                if not candidates:
                    await bot.send_message(user.telegram_id, "No matching items yet.")
                for candidate in candidates:
                    await bot.send_message(
                        user.telegram_id,
                        container.digest_builder().formatter.format_personalized_item(candidate),
                        reply_markup=_item_keyboard(candidate.profile_score_id),
                    )
                await container.profile_repository.mark_digest_status(digest_id, "sent")
                stats["sent"] += 1
            except Exception as error:
                stats["failed"] += 1
                if digest_id is not None:
                    await container.profile_repository.mark_digest_status(
                        digest_id,
                        "failed",
                    )
                print(f"Profile digest failed for {profile.id}: {error}")
        return stats
    finally:
        if bot is not None:
            await bot.session.close()
        await container.close()


def _integer_setting(
    settings: dict[str, object],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value: Any = settings.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))
