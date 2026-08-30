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


async def _send_job(
    container: Any, queue: DeliveryQueue, job: DeliveryJob, pause: float, dry_run: bool
) -> bool:
    """Отправить один дайджест. Возвращает True при успехе."""

    logger = get_run_logger()
    await queue.mark_sending(job.id)
    blocks: list[dict[str, Any]] = list(job.payload.get("blocks") or [])
    try:
        for position, block in enumerate(blocks):
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
        await queue.log_message(
            job.id,
            job.target_chat_id,
            status="failed",
            attempt=job.attempts + 1,
            error=str(error),
            retry_after=retry_after,
        )
        state = await queue.mark_failed(job.id, str(error), retry_after_seconds=retry_after)
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
        stats["released"] = await queue.release_stale(older_than_minutes=30)
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
        released = await queue.release_stale(older_than_minutes=30)
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
