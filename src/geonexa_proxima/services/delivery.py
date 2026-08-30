"""Очередь рассылки на PostgreSQL.

Отдельный брокер здесь не окупается: задания рождаются в той же транзакции, что
и дайджест, а ``SELECT ... FOR UPDATE SKIP LOCKED`` даёт ровно то, ради чего
берут очередь — несколько воркеров разбирают строки, не наступая друг другу на
ноги и не блокируясь.

Личная рассылка и групповая разведены по каналам: у них разные лимиты Telegram
и разные последствия падения, поэтому затык в группах не должен останавливать
личные дайджесты.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

#: Потолок задержки: дальше расти бессмысленно — дайджест протухнет раньше.
RETRY_CEILING_SECONDS = 3600
DEFAULT_RETRY_BACKOFF_SECONDS = 60

PERSONAL = "personal"
GROUP = "group"


@dataclass(frozen=True, slots=True)
class DeliveryJob:
    id: UUID
    digest_id: UUID
    subscriber_id: UUID
    channel: str
    target_chat_id: int
    attempts: int
    max_attempts: int
    payload: dict[str, Any]

    @property
    def is_last_attempt(self) -> bool:
        return self.attempts + 1 >= self.max_attempts


class DeliveryQueue:
    """Транзакционная очередь заданий на отправку."""

    def __init__(
        self,
        engine: AsyncEngine,
        worker_id: str | None = None,
        *,
        retry_backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS,
        retry_ceiling_seconds: int = RETRY_CEILING_SECONDS,
    ) -> None:
        self.engine = engine
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        # База backoff настраивается: DELIVERY_RETRY_BACKOFF_SECONDS раньше
        # объявлялась, но нигде не читалась — знать об этом было неоткуда.
        self.retry_backoff_seconds = max(1, int(retry_backoff_seconds))
        self.retry_ceiling_seconds = max(self.retry_backoff_seconds, int(retry_ceiling_seconds))

    async def enqueue(
        self,
        digest_id: UUID,
        subscriber_id: UUID,
        channel: str,
        target_chat_id: int,
        *,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        scheduled_at: datetime | None = None,
        max_attempts: int = 5,
    ) -> UUID | None:
        """Поставить задание. Повторная постановка того же дайджеста — no-op."""

        job_id = uuid.uuid4()
        result = await self._execute(
            """
            INSERT INTO delivery_jobs (
                id, digest_id, subscriber_id, channel, target_chat_id,
                status, priority, max_attempts, scheduled_at, payload
            ) VALUES (
                :id, :digest_id, :subscriber_id, :channel, :target_chat_id,
                'queued', :priority, :max_attempts,
                coalesce(:scheduled_at, now()), CAST(:payload AS jsonb)
            )
            ON CONFLICT (digest_id, target_chat_id) DO NOTHING
            RETURNING id
            """,
            {
                "id": str(job_id),
                "digest_id": str(digest_id),
                "subscriber_id": str(subscriber_id),
                "channel": channel,
                "target_chat_id": int(target_chat_id),
                "priority": int(priority),
                "max_attempts": int(max_attempts),
                "scheduled_at": scheduled_at,
                "payload": _json(payload or {}),
            },
        )
        row = result.first()
        return UUID(str(row[0])) if row else None

    async def claim(self, channel: str, batch_size: int = 50) -> list[DeliveryJob]:
        """Забрать пачку заданий. SKIP LOCKED — второй воркер не ждёт, а идёт дальше."""

        statement = text(
            """
            WITH picked AS (
                SELECT id FROM delivery_jobs
                WHERE channel = :channel
                  AND status = 'queued'
                  AND scheduled_at <= now()
                  AND (next_retry_at IS NULL OR next_retry_at <= now())
                ORDER BY priority DESC, scheduled_at
                LIMIT :batch
                FOR UPDATE SKIP LOCKED
            )
            UPDATE delivery_jobs AS j
               SET status = 'claimed',
                   claimed_at = now(),
                   claimed_by = :worker,
                   updated_at = now()
              FROM picked
             WHERE j.id = picked.id
            RETURNING j.id, j.digest_id, j.subscriber_id, j.channel,
                      j.target_chat_id, j.attempts, j.max_attempts, j.payload
            """
        )
        async with self.engine.begin() as connection:
            result = await connection.execute(
                statement,
                {"channel": channel, "batch": int(batch_size), "worker": self.worker_id},
            )
            rows = result.mappings().all()
        return [
            DeliveryJob(
                id=row["id"],
                digest_id=row["digest_id"],
                subscriber_id=row["subscriber_id"],
                channel=row["channel"],
                target_chat_id=row["target_chat_id"],
                attempts=row["attempts"],
                max_attempts=row["max_attempts"],
                payload=row["payload"] or {},
            )
            for row in rows
        ]

    async def mark_sending(self, job_id: UUID) -> None:
        await self._execute(
            "UPDATE delivery_jobs SET status='sending', started_at=now(), "
            "attempts=attempts+1, updated_at=now() WHERE id=:id",
            {"id": str(job_id)},
        )

    async def mark_sent(self, job_id: UUID) -> None:
        """Закрыть задание и вместе с ним дайджест.

        Дайджест и задание — разные строки, и статус надо двигать в обеих.
        Раньше дайджест навсегда оставался в `queued`: по таблице `digests`
        нельзя было отличить доставленное от застрявшего, а лента активности
        подписчика не получала события вовсе.
        """

        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        "UPDATE delivery_jobs SET status='sent', finished_at=now(), "
                        "last_error=NULL, updated_at=now() WHERE id=:id "
                        "RETURNING digest_id, subscriber_id"
                    ),
                    {"id": str(job_id)},
                )
            ).first()
            if row is None:
                return
            digest_id, subscriber_id = row
            await connection.execute(
                text(
                    "UPDATE digests SET status='sent', sent_at=now(), updated_at=now() "
                    "WHERE id=:id AND status <> 'sent'"
                ),
                {"id": str(digest_id)},
            )
            # Событие ленты: на нём стоят DAU/WAU/MAU и удержание когорт.
            # Начинать писать его позже доставки — значит навсегда потерять
            # статистику за первые недели работы.
            await connection.execute(
                text(
                    "INSERT INTO subscriber_activity (subscriber_id, kind, digest_id, payload) "
                    "VALUES (:subscriber_id, 'digest_received', :digest_id, "
                    "CAST(:payload AS jsonb))"
                ),
                {
                    "subscriber_id": str(subscriber_id),
                    "digest_id": str(digest_id),
                    "payload": _json({"job_id": str(job_id)}),
                },
            )

    async def mark_failed(
        self, job_id: UUID, error: str, *, retry_after_seconds: int | None = None
    ) -> str:
        """Вернуть в очередь с задержкой либо признать провал, если попытки кончились.

        Решение принимается в SQL по актуальному значению attempts, а не по
        снимку в памяти: между claim и завершением задание мог трогать другой
        процесс.
        """

        delay = retry_after_seconds
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        UPDATE delivery_jobs
                           SET status = CASE WHEN attempts >= max_attempts
                                             THEN 'failed' ELSE 'queued' END,
                               last_error = :error,
                               next_retry_at = CASE WHEN attempts >= max_attempts THEN NULL
                                    ELSE now() + make_interval(
                                        secs => coalesce(
                                            :delay,
                                            LEAST(
                                                :backoff * power(
                                                    2, GREATEST(attempts - 1, 0)
                                                ),
                                                :ceiling
                                            )
                                        )
                                    ) END,
                               finished_at = CASE WHEN attempts >= max_attempts
                                                  THEN now() ELSE NULL END,
                               claimed_by = NULL,
                               updated_at = now()
                         WHERE id = :id
                        RETURNING status
                        """
                    ),
                    {
                        "id": str(job_id),
                        "error": error[:2000],
                        "delay": delay,
                        "backoff": self.retry_backoff_seconds,
                        "ceiling": self.retry_ceiling_seconds,
                    },
                )
            ).first()
        return str(row[0]) if row else "unknown"

    async def cancel(self, job_id: UUID, reason: str = "") -> None:
        await self._execute(
            "UPDATE delivery_jobs SET status='cancelled', finished_at=now(), "
            "last_error=:reason, updated_at=now() WHERE id=:id AND status IN "
            "('queued','claimed','sending')",
            {"id": str(job_id), "reason": reason[:2000]},
        )

    async def log_message(
        self,
        job_id: UUID,
        chat_id: int,
        *,
        status: str,
        item_id: UUID | None = None,
        telegram_message_id: int | None = None,
        position: int = 0,
        attempt: int = 1,
        error_code: int | None = None,
        error: str | None = None,
        retry_after: int | None = None,
        text_preview: str | None = None,
    ) -> None:
        """Строка на каждое сообщение.

        Это одновременно журнал рассылки и возможность потом отредактировать
        или удалить конкретное сообщение по его telegram_message_id.
        """

        await self._execute(
            """
            INSERT INTO delivery_messages (
                delivery_job_id, item_id, chat_id, telegram_message_id, position,
                status, attempt, error_code, error, retry_after, text_preview, sent_at
            ) VALUES (
                :job_id, :item_id, :chat_id, :message_id, :position,
                :status, :attempt, :error_code, :error, :retry_after, :preview, :sent_at
            )
            """,
            {
                "job_id": str(job_id),
                "item_id": str(item_id) if item_id else None,
                "chat_id": int(chat_id),
                "message_id": telegram_message_id,
                "position": position,
                "status": status,
                "attempt": attempt,
                "error_code": error_code,
                "error": (error or "")[:2000] or None,
                "retry_after": retry_after,
                "preview": (text_preview or "")[:200] or None,
                # sent_at считаем здесь: тот же параметр в колонке varchar и в
                # сравнении с текстовым литералом заставляет asyncpg вывести
                # для него два несовместимых типа.
                "sent_at": datetime.now(UTC) if status == "sent" else None,
            },
        )

    async def release_stale(self, older_than_minutes: int = 30) -> int:
        """Вернуть в очередь задания, которые воркер забрал и не довёл.

        Воркер может умереть между claim и отправкой; без этого такие строки
        зависли бы в 'claimed' навсегда.
        """

        result = await self._execute(
            "UPDATE delivery_jobs SET status='queued', claimed_by=NULL, claimed_at=NULL, "
            "updated_at=now() WHERE status IN ('claimed','sending') "
            "AND claimed_at < now() - make_interval(mins => :minutes) RETURNING id",
            {"minutes": int(older_than_minutes)},
        )
        return len(result.fetchall())

    async def expire_old(self, ttl_hours: int = 72) -> int:
        result = await self._execute(
            "UPDATE delivery_jobs SET status='skipped', finished_at=now(), "
            "last_error='истёк срок актуальности', updated_at=now() "
            "WHERE status='queued' AND created_at < now() - make_interval(hours => :hours) "
            "RETURNING id",
            {"hours": int(ttl_hours)},
        )
        return len(result.fetchall())

    async def stats(self) -> dict[str, dict[str, int]]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT channel, status, count(*) AS n FROM delivery_jobs "
                            "GROUP BY channel, status"
                        )
                    )
                )
                .mappings()
                .all()
            )
        summary: dict[str, dict[str, int]] = {PERSONAL: {}, GROUP: {}}
        for row in rows:
            summary.setdefault(row["channel"], {})[row["status"]] = int(row["n"])
        return summary

    async def _execute(self, sql: str, params: dict[str, Any]):
        async with self.engine.begin() as connection:
            return await connection.execute(text(sql), params)


def next_retry_delay(
    attempts: int,
    base_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS,
    ceiling: int = RETRY_CEILING_SECONDS,
) -> int:
    """Экспоненциальный backoff с потолком — та же формула, что в SQL."""

    return int(min(base_seconds * (2 ** max(0, attempts - 1)), ceiling))


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def target_channel(subscriber_kind: str) -> str:
    """Личка и чаты разводятся по каналам ещё на этапе постановки задания."""

    return PERSONAL if subscriber_kind == "user" else GROUP


def rate_limit_delay(
    channel: str,
    per_chat_rps: float,
    per_group_rpm: float,
    global_rps: float | None = None,
) -> float:
    """Пауза между сообщениями под лимиты Bot API.

    Лимитов три, и действует самый строгий из применимых: на один чат, на
    группу и глобальный на токен. Последний раньше объявлялся в настройках,
    но нигде не учитывался — воркер мог упереться в него, не подозревая, что
    ограничение вообще существует.
    """

    per_chat = 60.0 / max(per_group_rpm, 0.1) if channel == GROUP else 1.0 / max(per_chat_rps, 0.1)
    if global_rps is None:
        return per_chat
    return max(per_chat, 1.0 / max(global_rps, 0.1))


def spread(jobs: Sequence[DeliveryJob], now: datetime | None = None) -> list[datetime]:
    """Разложить отправки во времени, чтобы не упереться в глобальный лимит."""

    start = now or datetime.now(UTC)
    return [start + timedelta(milliseconds=40 * index) for index in range(len(jobs))]
