"""Очередь доставки и логи отправок."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import text

from geonexa_proxima.api.admin.deps import (
    Admin,
    AppSettings,
    Engine,
    Paging,
    audit,
    execute,
    fetch_all,
    fetch_one,
    page_response,
    require,
)
from geonexa_proxima.services.delivery import GROUP, PERSONAL, DeliveryQueue

router = APIRouter(prefix="/deliveries", tags=["admin:deliveries"])


@router.get("/queue")
async def queue_summary(admin: Admin, db: Engine) -> dict[str, Any]:
    """Сводка очереди по каналам и статусам — первое, куда смотрят при затыке."""

    rows = await fetch_all(
        db,
        text(
            "SELECT channel, status, count(*) AS n,"
            " min(scheduled_at) AS oldest,"
            " round(extract(epoch FROM now() - min(scheduled_at))) AS oldest_age_seconds"
            " FROM delivery_jobs GROUP BY channel, status ORDER BY channel, status"
        ),
    )
    stuck = await fetch_one(
        db,
        text(
            "SELECT count(*) AS n FROM delivery_jobs"
            " WHERE status IN ('claimed', 'sending') AND claimed_at < now() - interval '30 minutes'"
        ),
    )
    return {
        "channels": [PERSONAL, GROUP],
        "rows": rows,
        "stuck": int((stuck or {}).get("n", 0)),
    }


@router.get("/jobs")
async def jobs(
    admin: Admin,
    db: Engine,
    paging: Paging,
    channel: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    subscriber_id: UUID | None = None,
) -> dict[str, Any]:
    conditions = ["true"]
    params: dict[str, Any] = {"limit": paging.limit, "offset": paging.offset}
    if channel:
        conditions.append("j.channel = :channel")
        params["channel"] = channel
    if status_filter:
        conditions.append("j.status = :status")
        params["status"] = status_filter
    if subscriber_id:
        conditions.append("j.subscriber_id = :subscriber_id")
        params["subscriber_id"] = str(subscriber_id)
    where = " AND ".join(conditions)
    rows = await fetch_all(
        db,
        text(
            f"SELECT j.id, j.channel, j.status, j.attempts, j.max_attempts, j.target_chat_id,"
            f" j.scheduled_at, j.next_retry_at, j.finished_at, j.last_error,"
            f" s.title AS subscriber_title, s.kind AS subscriber_kind"
            f" FROM delivery_jobs j JOIN subscribers s ON s.id = j.subscriber_id"
            f" WHERE {where} ORDER BY j.created_at DESC LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    total = await fetch_one(
        db,
        text(f"SELECT count(*) AS n FROM delivery_jobs j WHERE {where}"),
        {k: v for k, v in params.items() if k not in {"limit", "offset"}},
    )
    return page_response(rows, int((total or {}).get("n", 0)), paging)


@router.get("/jobs/{job_id}")
async def job(job_id: UUID, admin: Admin, db: Engine) -> dict[str, Any]:
    row = require(
        await fetch_one(
            db, text("SELECT * FROM delivery_jobs WHERE id = :id"), {"id": str(job_id)}
        ),
        "Задание",
    )
    messages = await fetch_all(
        db,
        text(
            "SELECT status, position, attempt, telegram_message_id, error, error_code,"
            " retry_after, text_preview, sent_at, created_at FROM delivery_messages"
            " WHERE delivery_job_id = :id"
            " ORDER BY position, created_at"
        ),
        {"id": str(job_id)},
    )
    return {"job": row, "messages": messages}


@router.post("/jobs/{job_id}/retry")
async def retry(job_id: UUID, admin: Admin, db: Engine, request: Request) -> dict[str, Any]:
    """Вернуть задание в очередь немедленно, сбросив счётчик попыток."""

    updated = await execute(
        db,
        text(
            "UPDATE delivery_jobs SET status = 'queued', attempts = 0, next_retry_at = NULL,"
            " claimed_by = NULL, claimed_at = NULL, finished_at = NULL, last_error = NULL,"
            " scheduled_at = now(), updated_at = now()"
            " WHERE id = :id AND status IN ('failed', 'cancelled', 'skipped')"
        ),
        {"id": str(job_id)},
    )
    if not updated:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Повторить можно только провалившееся, отменённое или пропущенное задание",
        )
    await audit(
        db,
        admin,
        request,
        action="delivery.retry",
        entity_type="delivery_job",
        entity_id=str(job_id),
    )
    return {"queued": True}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: UUID, admin: Admin, db: Engine, settings: AppSettings, request: Request
) -> dict[str, Any]:
    queue = DeliveryQueue(db, retry_backoff_seconds=settings.delivery_retry_backoff_seconds)
    await queue.cancel(job_id, reason=f"отменено администратором {admin.username}")
    await audit(
        db,
        admin,
        request,
        action="delivery.cancel",
        entity_type="delivery_job",
        entity_id=str(job_id),
    )
    return {"cancelled": True}


@router.get("/messages")
async def messages(
    admin: Admin,
    db: Engine,
    paging: Paging,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    chat_id: int | None = None,
) -> dict[str, Any]:
    conditions = ["true"]
    params: dict[str, Any] = {"limit": paging.limit, "offset": paging.offset}
    if status_filter:
        conditions.append("m.status = :status")
        params["status"] = status_filter
    if chat_id:
        conditions.append("m.chat_id = :chat_id")
        params["chat_id"] = chat_id
    where = " AND ".join(conditions)
    rows = await fetch_all(
        db,
        text(
            f"SELECT m.id, m.delivery_job_id, m.chat_id, m.status, m.attempt,"
            f" m.telegram_message_id, m.error, m.error_code, m.retry_after,"
            f" m.text_preview, m.sent_at, m.created_at"
            f" FROM delivery_messages m WHERE {where}"
            f" ORDER BY m.created_at DESC LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    total = await fetch_one(
        db,
        text(f"SELECT count(*) AS n FROM delivery_messages m WHERE {where}"),
        {k: v for k, v in params.items() if k not in {"limit", "offset"}},
    )
    return page_response(rows, int((total or {}).get("n", 0)), paging)


@router.get("/stats")
async def stats(
    admin: Admin, db: Engine, days: Annotated[int, Query(ge=1, le=365)] = 30
) -> dict[str, Any]:
    rows = await fetch_all(
        db,
        text(
            "SELECT day, channel, jobs_created, jobs_sent, jobs_failed, messages_sent,"
            " messages_failed, rate_limited, avg_queue_seconds, p95_queue_seconds"
            " FROM metrics_delivery_daily"
            " WHERE day >= current_date - make_interval(days => :days)"
            " ORDER BY day, channel"
        ),
        {"days": days},
    )
    return {
        "days": days,
        "series": [
            {"key": "jobs_sent", "label": "Отправлено", "color_slot": 1},
            {"key": "jobs_failed", "label": "Провалено", "color_slot": "critical"},
        ],
        "rows": rows,
    }


@router.get("/digests")
async def digests(
    admin: Admin,
    db: Engine,
    paging: Paging,
    subscriber_id: UUID | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> dict[str, Any]:
    conditions = ["true"]
    params: dict[str, Any] = {"limit": paging.limit, "offset": paging.offset}
    if subscriber_id:
        conditions.append("d.subscriber_id = :subscriber_id")
        params["subscriber_id"] = str(subscriber_id)
    if status_filter:
        conditions.append("d.status = :status")
        params["status"] = status_filter
    where = " AND ".join(conditions)
    rows = await fetch_all(
        db,
        text(
            f"SELECT d.id, d.status, d.period_start, d.period_end, d.created_at,"
            f" s.title AS subscriber_title, s.kind AS subscriber_kind, p.name AS profile_name,"
            f" (SELECT count(*) FROM digest_items di WHERE di.digest_id = d.id) AS items"
            f" FROM digests d JOIN subscribers s ON s.id = d.subscriber_id"
            f" LEFT JOIN subscriber_profiles p ON p.id = d.profile_id"
            f" WHERE {where} ORDER BY d.created_at DESC LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    total = await fetch_one(
        db,
        text(f"SELECT count(*) AS n FROM digests d WHERE {where}"),
        {k: v for k, v in params.items() if k not in {"limit", "offset"}},
    )
    return page_response(rows, int((total or {}).get("n", 0)), paging)
