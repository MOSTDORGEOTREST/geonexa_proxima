"""Диспетчер дайджестов и флоу одного подписчика.

Диспетчер ничего не считает: он решает, кому пора, и запускает отдельные флоу
параллельно. Флоу подписчика строит дайджест и **ставит задания в очередь**, но
не отправляет — доставка живёт в своих воркерах со своими лимитами.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from prefect import flow, get_run_logger

from geonexa_proxima.config import zone_of
from geonexa_proxima.services.container import load_container
from geonexa_proxima.services.delivery import DeliveryQueue, target_channel
from geonexa_proxima.services.dispatch_queries import (
    DUE_PROFILES,
    PROFILE_CONTEXT,
    SCHEDULE_NEXT,
    digest_interval_hours,
    resolve_kinds,
)
from geonexa_proxima.services.rendering import RenderedItem, render_digest

DEFAULT_HEADING = "Проксима: свежее по геотехнике и ИИ"


@flow(name="geonexa-digest-dispatch", log_prints=True)
async def digest_dispatch_flow(
    *,
    bootstrap_target: str | None = None,
    kinds: Sequence[str] | None = None,
    limit: int = 500,
    concurrency: int = 8,
    deliver: bool = True,
    deliver_at_hour: int | None = None,
) -> dict[str, int]:
    """Найти профили, которым пора, и запустить их флоу параллельно.

    ``kinds`` задаёт вид подписчиков: по умолчанию берутся все, но в
    расписании удобно держать отдельный прогон для личек и отдельный для
    групп с каналами — у них разная частота и разная цена ошибки.

    ``deliver=False`` строит дайджесты и оставляет их в статусе ``ready``, не
    ставя в очередь: smoke test без единого сообщения в Telegram.

    ``deliver_at_hour`` разводит сборку и отправку: дайджест собирается ночью,
    а уходит в чаты в названный час по поясу платформы. Час назначается
    заданию, а не крону воркера, — иначе повторная попытка после сбоя ждала бы
    следующего запуска воркера, то есть неделю, и протухла бы по TTL.
    """

    import asyncio

    logger = get_run_logger()
    selected = resolve_kinds(kinds)
    container = load_container(target=bootstrap_target)
    try:
        grace = container.settings.digest_due_grace_minutes
        async with container.require_engine().connect() as connection:
            rows = (
                (
                    await connection.execute(
                        DUE_PROFILES,
                        {"limit": limit, "kinds": selected, "grace": grace},
                    )
                )
                .mappings()
                .all()
            )
        deliver_at = _deliver_at(container.settings.timezone, deliver_at_hour)
        logger.info(
            "К отправке профилей (%s): %s%s",
            ",".join(selected),
            len(rows),
            f"; отправка не раньше {deliver_at:%d.%m %H:%M} UTC" if deliver_at else "",
        )
        if not rows:
            return {"due": 0, "queued": 0, "failed": 0}

        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def run_one(row: dict[str, Any]) -> int:
            async with semaphore:
                return await subscriber_digest_flow(
                    profile_id=row["profile_id"],
                    bootstrap_target=bootstrap_target,
                    deliver=deliver,
                    deliver_at=deliver_at,
                )

        results = await asyncio.gather(*(run_one(dict(r)) for r in rows), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Флоу подписчика упал: %s", result)
        failed = sum(1 for r in results if isinstance(r, BaseException))
        queued = sum(int(r) for r in results if not isinstance(r, BaseException))
        return {"due": len(rows), "queued": queued, "failed": failed}
    finally:
        await container.close()


@flow(name="geonexa-subscriber-digest", log_prints=True)
async def subscriber_digest_flow(
    *,
    profile_id: UUID,
    bootstrap_target: str | None = None,
    deliver: bool = True,
    deliver_at: datetime | None = None,
) -> int:
    """Построить дайджест одного профиля и поставить задание в очередь.

    Возвращает число поставленных заданий — ноль означает, что материалов не
    набралось, и это нормальный исход, а не ошибка.
    """

    logger = get_run_logger()
    container = load_container(target=bootstrap_target)
    settings = container.settings
    try:
        async with container.require_engine().connect() as connection:
            row = (
                (
                    await connection.execute(
                        PROFILE_CONTEXT,
                        {
                            "id": str(profile_id),
                            "default_interval": settings.digest_default_interval_hours,
                        },
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise LookupError(f"Профиль {profile_id} не найден")

        settings_blob = row["digest_settings"]
        if not isinstance(settings_blob, dict):
            # JSONB допускает и массив, и число. `.get` на них падает, а падать
            # тут — значит не собрать дайджест из-за одной кривой записи.
            settings_blob = {}
        lookback = int(settings_blob.get("lookback_hours", 168))
        period_end = datetime.now(UTC)
        period_start = period_end - timedelta(hours=lookback)
        # Окно допуска берёт профиль раньше срока, поэтому окно материалов
        # прижимаем к прошлому выпуску: иначе последние полтора часа перед ним
        # попадают и в тот дайджест, и в этот.
        previous = row.get("last_digest_at")
        if previous is not None:
            period_start = max(period_start, previous)

        profile = await container.profile_repository.get_profile(profile_id)
        # Порог личной оценки уходит внутрь отбора, а не применяется к готовой
        # выдаче. Снаружи он вычитал материалы уже после квоты разнообразия:
        # место, зарезервированное слабой гранью, пропадало, и дайджест
        # приходил короче, чем мог бы, — вытесненный материал уже не вернуть.
        # Тариф ограничивает не только частоту, но и объём. Раньше
        # `plan_max_items` считался в запросе и не использовался: профиль с
        # max_items=100 на тарифе с потолком 10 получал сто карточек, то есть
        # сто сообщений в чат и обход ограничения плана.
        limit = min(int(row["max_items"]), int(row["plan_max_items"] or row["max_items"]))
        candidates = await container.digest_builder().list_personalized(
            profile,
            limit=limit,
            since=period_start,
            minimum_global_score=settings.digest_score_threshold,
            minimum_personal_score=float(row["min_personal_score"] or 0),
        )

        digest_id = await container.profile_repository.create_digest(
            row["subscriber_id"],
            profile_id,
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
            payload={"profile_name": row["name"], "profile_version": row["version"]},
        )

        interval = digest_interval_hours(
            plan_minimum=row["min_interval_hours"],
            profile_settings=row["digest_settings"],
        )

        if not candidates:
            logger.info("Профиль %s: подходящих материалов нет", row["name"])
            await container.profile_repository.mark_digest_status(digest_id, "skipped")
            # Пустой дайджест — тоже состоявшийся прогон: без переноса срока
            # диспетчер вернётся к этому профилю через минуту и будет
            # пересчитывать его вхолостую до появления первой статьи.
            await _schedule_next(container, profile_id, interval)
            return 0
        if not deliver:
            await container.profile_repository.mark_digest_status(digest_id, "ready")
            # Сухой прогон не сдвигает срок: он ничего не отправил.
            return 0

        # Формат берётся из профиля: каналу нужен один пост, личке — карточки
        # с кнопками. Раньше оба канала получали карточки, и `digest_post`
        # существовал только в списке допустимых значений.
        blocks = render_digest(
            [RenderedItem.from_candidate(candidate) for candidate in candidates],
            fmt=row["delivery_format"],
            heading=(row["digest_settings"] or {}).get("heading") or DEFAULT_HEADING,
            period_start=period_start,
            period_end=period_end,
        )
        queue = DeliveryQueue(
            container.require_engine(),
            retry_backoff_seconds=settings.delivery_retry_backoff_seconds,
        )
        job_id = await queue.enqueue(
            digest_id,
            row["subscriber_id"],
            target_channel(row["kind"]),
            row["telegram_chat_id"],
            payload={"blocks": blocks, "profile_name": row["name"]},
            max_attempts=settings.delivery_max_attempts,
            # Без времени задание уезжает ближайшим прогоном воркера — так
            # ведёт себя ручной запуск, и это правильно: администратор нажал
            # кнопку сейчас.
            scheduled_at=deliver_at,
        )
        await container.profile_repository.mark_digest_status(digest_id, "queued")
        next_at = await _schedule_next(container, profile_id, interval)
        logger.info(
            "Профиль %s: %s сообщений в очереди, следующий дайджест %s",
            row["name"],
            len(blocks),
            next_at,
        )
        return 1 if job_id else 0
    finally:
        await container.close()


def _deliver_at(timezone: str, hour: int | None, now: datetime | None = None) -> datetime | None:
    """Во сколько задание можно слать. ``None`` — «как только воркер увидит».

    Час считается в поясе платформы и берётся сегодняшний. Если он уже прошёл
    (диспетчер запустили вручную вечером), задание уходит сразу: сдвигать его
    на сутки вперёд означало бы, что нажатие кнопки ничего не сделало.

    ``now`` существует ради тестов: без него проверка «час ещё впереди»
    зависела бы от времени суток, в которое её запустили.
    """

    if hour is None:
        return None
    if not 0 <= int(hour) <= 23:
        raise ValueError(f"Час отправки вне суток: {hour}")
    zone = zone_of(timezone)
    local = (now or datetime.now(UTC)).astimezone(zone)
    planned = local.replace(hour=int(hour), minute=0, second=0, microsecond=0)
    if planned <= local:
        return None
    return planned.astimezone(UTC)


async def _schedule_next(container: Any, profile_id: UUID, hours: int) -> Any:
    """Назначить следующий дайджест профилю.

    Срок двигается сразу после постановки в очередь, а не после успешной
    отправки: между ними стоят ретраи, и профиль, чья доставка временно
    падает, иначе попадал бы в выборку на каждом прогоне диспетчера и копил
    бы дубликаты заданий.
    """

    async with container.require_engine().begin() as connection:
        result = await connection.execute(
            SCHEDULE_NEXT, {"id": str(profile_id), "hours": int(hours)}
        )
        row = result.first()
    return row[0] if row else None
