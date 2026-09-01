"""Два воркера рассылки: в личные чаты и в группы с каналами.

Разведены намеренно. У Bot API разные лимиты на личку и на группы, а затык в
групповой рассылке не должен останавливать личные дайджесты.

Флоу не строит дайджесты — он только развозит то, что уже лежит в очереди.
"""

from __future__ import annotations

import asyncio
from typing import Any

from prefect import flow, get_run_logger

from geonexa_proxima.services.container import load_container
from geonexa_proxima.services.delivery import (
    GROUP,
    PERSONAL,
    DeliveryJob,
    DeliveryQueue,
    rate_limit_delay,
)

#: Ошибки Telegram, которые не лечатся повторной попыткой. Их пять попыток с
#: нарастающей паузой — это пятнадцать минут удержания очереди и пятнадцать
#: минут неверного вывода «доставка тормозит», хотя доставлять уже некуда.
_PERMANENT = (
    "bot was blocked",
    "bot was kicked",
    "user is deactivated",
    "chat not found",
    "chat_id is empty",
    "have no rights to send",
    "not enough rights",
    "topic_closed",
    "peer_id_invalid",
)


def is_permanent(error: BaseException) -> bool:
    """Правда ли, что повторять эту отправку бессмысленно.

    Разбор по тексту, а не по классу исключения, намеренно: aiogram кладёт в
    `TelegramForbiddenError` и `TelegramBadRequest` десяток разных причин, и
    различает их именно описание. Ошибку сети или 429 сюда не попадёт —
    у них другой текст, и они честно ретраятся.
    """

    if getattr(error, "retry_after", None):
        return False
    message = str(error).casefold()
    return any(marker in message for marker in _PERMANENT)


async def _send_job(
    container: Any, queue: DeliveryQueue, job: DeliveryJob, pause: float, dry_run: bool
) -> bool:
    """Отправить один дайджест. Возвращает True при успехе."""

    logger = get_run_logger()
    if not await queue.mark_sending(job.id):
        # Задание уже не наше: пока мы разгребали пачку, `release_stale` вернул
        # его в очередь и его забрал другой воркер. Молча уходим — иначе оба
        # отправят одно и то же.
        logger.warning("Задание %s перехвачено другим воркером — пропускаю", job.id)
        return False
    blocks: list[dict[str, Any]] = list(job.payload.get("blocks") or [])
    # Что уже ушло в чат при прошлой попытке — не отправляем второй раз.
    already = await queue.sent_positions(job.id) if job.attempts else set()
    if already:
        logger.info(
            "Задание %s: продолжаю с блока %s из %s — %s уже отправлено",
            job.id,
            len(already),
            len(blocks),
            len(already),
        )
    try:
        for position, block in enumerate(blocks):
            if position in already:
                continue
            body = str(block.get("text", ""))
            if dry_run:
                await queue.log_message(
                    job.id,
                    job.target_chat_id,
                    status="skipped",
                    position=position,
                    attempt=job.attempts + 1,
                    text_preview=body,
                )
                continue
            message = await container.telegram_bot().send_message(
                job.target_chat_id,
                body,
                reply_markup=block.get("reply_markup"),
                disable_web_page_preview=True,
            )
            await queue.log_message(
                job.id,
                job.target_chat_id,
                status="sent",
                item_id=block.get("item_id"),
                telegram_message_id=getattr(message, "message_id", None),
                position=position,
                attempt=job.attempts + 1,
                text_preview=body,
            )
            await asyncio.sleep(pause)
        await queue.mark_sent(job.id)
        return True
    except Exception as error:
        retry_after = getattr(error, "retry_after", None)
        permanent = is_permanent(error)
        await queue.log_message(
            job.id,
            job.target_chat_id,
            status="failed",
            attempt=job.attempts + 1,
            error=str(error),
            retry_after=retry_after,
        )
        state = await queue.mark_failed(
            job.id, str(error), retry_after_seconds=retry_after, permanent=permanent
        )
        if permanent:
            # Это не «попробуем позже», а «доставлять больше некуда». Строка
            # должна быть заметной: чат надо снимать с рассылки руками или
            # ждать, пока его пересчитает мониторинг прав бота.
            logger.error(
                "Задание %s: доставка невозможна (%s) — чат %s снят с этой рассылки",
                job.id,
                error,
                job.target_chat_id,
            )
        else:
            logger.warning("Задание %s: %s → %s", job.id, error, state)
        return False


async def _run_channel(channel: str, bootstrap_target: str | None) -> dict[str, int]:
    logger = get_run_logger()
    container = load_container(target=bootstrap_target)
    settings = container.settings
    queue = DeliveryQueue(
        container.require_engine(),
        retry_backoff_seconds=settings.delivery_retry_backoff_seconds,
    )
    stats = {"claimed": 0, "sent": 0, "failed": 0, "released": 0}
    try:
        # Воркер мог умереть между claim и отправкой — вернём такие строки в очередь.
        # Порог заведомо больше времени отправки самой длинной пачки: пятьдесят
        # постов по три части при паузе в 3,3 с — это почти девять минут, и
        # тридцатиминутный порог задание живого воркера ещё не задевал бы, но
        # запас должен быть виден и считаться, а не подразумеваться.
        stats["released"] = await queue.release_stale(
            older_than_minutes=settings.delivery_stale_minutes
        )
        jobs = await queue.claim(channel, batch_size=settings.delivery_batch_size)
        stats["claimed"] = len(jobs)
        if not jobs:
            return stats
        pause = rate_limit_delay(
            channel,
            settings.telegram_chat_rate_per_second,
            settings.telegram_group_rate_per_minute,
            settings.telegram_global_rate_per_second,
        )
        for job in jobs:
            ok = await _send_job(container, queue, job, pause, settings.delivery_dry_run)
            stats["sent" if ok else "failed"] += 1
        logger.info("Канал %s: %s", channel, stats)
        return stats
    finally:
        await container.close()


@flow(name="geonexa-delivery-personal", log_prints=True)
async def delivery_personal_flow(*, bootstrap_target: str | None = None) -> dict[str, int]:
    return await _run_channel(PERSONAL, bootstrap_target)


@flow(name="geonexa-delivery-group", log_prints=True)
async def delivery_group_flow(*, bootstrap_target: str | None = None) -> dict[str, int]:
    return await _run_channel(GROUP, bootstrap_target)


@flow(name="geonexa-delivery-maintenance", log_prints=True)
async def delivery_maintenance_flow(
    *, bootstrap_target: str | None = None, purge_old_rows: bool = True
) -> dict[str, Any]:
    """Убрать протухшие задания и пережившие свой срок сырые события.

    Дайджест недельной давности слать некому, а `harvest_decisions` при
    включённом журнале отказов растёт быстрее всех таблиц вместе взятых.
    """

    logger = get_run_logger()
    container = load_container(target=bootstrap_target)
    try:
        queue = DeliveryQueue(
            container.require_engine(),
            retry_backoff_seconds=container.settings.delivery_retry_backoff_seconds,
        )
        expired = await queue.expire_old(container.settings.delivery_job_ttl_hours)
        released = await queue.release_stale(
            older_than_minutes=container.settings.delivery_stale_minutes
        )
        report: dict[str, Any] = {"expired": expired, "released": released}
        if purge_old_rows:
            from geonexa_proxima.metrics.purge import purge

            removed = await purge(container.require_engine(), container.settings)
            report["purged"] = removed
            if removed:
                logger.info("Удалено устаревших строк: %s", removed)
        return report
    finally:
        await container.close()
