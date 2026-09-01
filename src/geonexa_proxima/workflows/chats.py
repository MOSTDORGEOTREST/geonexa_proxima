"""Мониторинг чатов, куда добавили бота, и обслуживание подписок.

Telegram сообщает о выходе бота ровно один раз — апдейтом `my_chat_member`.
Если сервис в этот момент лежал, чат навсегда остаётся в базе «живым», и
рассылка каждый раз бьётся о `Forbidden`. Поэтому состояние чатов ещё и
опрашивается по расписанию: правда о правах бота живёт в Telegram, а не у нас.
"""

from __future__ import annotations

import asyncio
from typing import Any

from prefect import flow, get_run_logger

from geonexa_proxima.db.subscriber_repository import SubscriberRepository
from geonexa_proxima.services.container import load_container

#: Что Bot API возвращает, когда бота в чате уже нет.
_GONE = ("left", "kicked")


def _bot_status(member: Any) -> str:
    """Достать статус из ChatMember любого подтипа."""

    status = getattr(member, "status", None)
    return str(getattr(status, "value", status) or "left")


@flow(name="geonexa-chat-monitor", log_prints=True)
async def chat_monitor_flow(
    *,
    bootstrap_target: str | None = None,
    limit: int = 200,
    pause_seconds: float = 0.2,
) -> dict[str, int]:
    """Сверить с Telegram права бота во всех известных группах и каналах."""

    logger = get_run_logger()
    container = load_container(target=bootstrap_target, require_ready=False)
    stats = {"checked": 0, "changed": 0, "lost": 0, "errors": 0}
    try:
        repository = SubscriberRepository(container.session_factory)
        # Постранично, а не первые `limit` записей. Список отсортирован по дате
        # добавления, и с ростом числа чатов самые старые группы переставали
        # проверяться вовсе: бота оттуда выгнали, `my_chat_member` потерялся, а
        # диспетчер каждую неделю продолжал слать туда дайджест.
        chats = []
        page = 0
        while True:
            batch = await repository.list_chats(limit=200, offset=page * 200)
            chats.extend(batch)
            if len(batch) < 200 or len(chats) >= limit:
                break
            page += 1
        chats = chats[:limit]
        if not chats:
            return stats
        bot = container.telegram_bot()
        me = await bot.get_me()
        for record in chats:
            stats["checked"] += 1
            try:
                member = await bot.get_chat_member(record.telegram_chat_id, me.id)
                status = _bot_status(member)
                # get_chat даёт число участников и права канала — то, чего нет
                # в ChatMember, но что решает, можно ли туда вообще слать.
                chat = await bot.get_chat(record.telegram_chat_id)
                can_post = getattr(member, "can_post_messages", None)
                if record.kind != "channel":
                    can_post = True
                updated = await repository.update_bot_status(
                    record.telegram_chat_id,
                    status,
                    can_post_messages=can_post,
                    member_count=getattr(chat, "member_count", None),
                    error=None,
                )
            except Exception as error:
                stats["errors"] += 1
                logger.warning("Чат %s недоступен: %s", record.telegram_chat_id, error)
                text = str(error).lower()
                if "forbidden" in text or "chat not found" in text or "kicked" in text:
                    await repository.update_bot_status(
                        record.telegram_chat_id, "kicked", error=str(error)[:500]
                    )
                    stats["lost"] += 1
                continue
            if updated.bot_status != record.bot_status:
                stats["changed"] += 1
                if updated.bot_status in _GONE:
                    stats["lost"] += 1
            await asyncio.sleep(pause_seconds)
        logger.info("Мониторинг чатов: %s", stats)
        return stats
    finally:
        await container.close()


@flow(name="geonexa-subscription-maintenance", log_prints=True)
async def subscription_maintenance_flow(
    *, bootstrap_target: str | None = None, remind_within_days: int = 3
) -> dict[str, int]:
    """Погасить просроченные подписки и собрать тех, кому пора напомнить.

    Напоминание не отправляется здесь: оно ставится в ту же очередь доставки,
    что и дайджесты, — иначе лимиты Bot API считались бы в двух местах.
    """

    from datetime import timedelta

    logger = get_run_logger()
    container = load_container(target=bootstrap_target, require_ready=False)
    try:
        repository = SubscriberRepository(container.session_factory)
        expired = await repository.expire_due()
        expiring = await repository.list_expiring(within=timedelta(days=remind_within_days))
        logger.info("Погашено %s подписок, скоро истекают %s", expired, len(expiring))
        return {"expired": expired, "expiring": len(expiring)}
    finally:
        await container.close()
