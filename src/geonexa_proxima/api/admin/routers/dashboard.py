"""Сводка первого экрана: что происходит с платформой прямо сейчас."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import text

from geonexa_proxima.api.admin.deps import Admin, Engine, fetch_all, fetch_one

router = APIRouter(prefix="/dashboard", tags=["admin:dashboard"])

# Один запрос вместо десяти: каждая подзапросная строка — независимый счётчик,
# и гонять их по отдельности значило бы десять раз занимать соединение из
# пула, который намеренно маленький.
SUMMARY = text(
    """
    SELECT
      (SELECT count(*) FROM items) AS items,
      (SELECT count(*) FROM items WHERE created_at >= now() - interval '24 hours')
        AS items_24h,
      (SELECT count(*) FROM items WHERE rank_total_score IS NOT NULL) AS ranked,
      (SELECT count(*) FROM items WHERE deep_analysis IS NOT NULL) AS analyzed,
      (SELECT count(*) FROM subscribers) AS subscribers,
      (SELECT count(*) FROM subscribers WHERE status = 'active') AS subscribers_active,
      (SELECT count(*) FROM subscribers WHERE kind = 'user') AS subscribers_users,
      (SELECT count(*) FROM subscribers WHERE kind = 'group') AS subscribers_groups,
      (SELECT count(*) FROM subscribers WHERE kind = 'channel') AS subscribers_channels,
      (SELECT count(*) FROM subscribers WHERE status = 'pending') AS subscribers_pending,
      (SELECT count(*) FROM subscriptions WHERE status IN ('active', 'trial')
        AND starts_at <= now()
        AND (ends_at IS NULL OR coalesce(grace_until, ends_at) >= now())) AS subs_active,
      (SELECT count(*) FROM subscriptions WHERE status IN ('active', 'trial')
        AND ends_at BETWEEN now() AND now() + interval '7 days') AS subs_expiring,
      (SELECT count(*) FROM subscriptions WHERE status = 'expired') AS subs_expired,
      (SELECT count(*) FROM delivery_jobs WHERE status = 'queued') AS delivery_queued,
      (SELECT count(*) FROM delivery_jobs WHERE status = 'sent'
        AND finished_at >= now() - interval '24 hours') AS delivery_sent_24h,
      (SELECT count(*) FROM delivery_jobs WHERE status = 'failed'
        AND finished_at >= now() - interval '24 hours') AS delivery_failed_24h,
      (SELECT count(*) FROM llm_call_log WHERE created_at >= now() - interval '24 hours')
        AS llm_calls_24h,
      (SELECT coalesce(sum(coalesce(prompt_tokens, 0)
        + coalesce(completion_tokens, 0) + coalesce(reasoning_tokens, 0)), 0)
        FROM llm_call_log
        WHERE created_at >= now() - interval '24 hours') AS llm_tokens_24h,
      (SELECT coalesce(sum(cost_usd), 0) FROM llm_call_log
        WHERE created_at >= now() - interval '24 hours') AS llm_cost_24h
    """
)

LAST_RUN = text(
    """
    SELECT id, status, trigger, started_at, finished_at, error, stats,
           coalesce((stats->>'fetched')::int, 0)    AS fetched,
           coalesce((stats->>'accepted')::int, 0)   AS accepted,
           coalesce((stats->>'borderline')::int, 0) AS borderline,
           coalesce((stats->>'rejected')::int, 0)   AS rejected
      FROM harvest_runs
     ORDER BY started_at DESC
     LIMIT 1
    """
)

RECENT_ERRORS = text(
    """
    SELECT 'harvest' AS source, id::text AS id, error AS message, finished_at AS at
      FROM harvest_runs WHERE status = 'failed' AND error IS NOT NULL
    UNION ALL
    SELECT 'delivery', id::text, last_error, finished_at
      FROM delivery_jobs WHERE status = 'failed' AND last_error IS NOT NULL
    UNION ALL
    SELECT 'flow', id::text, error, finished_at
      FROM flow_runs WHERE state IN ('FAILED', 'CRASHED') AND error IS NOT NULL
    ORDER BY at DESC NULLS LAST
    LIMIT 20
    """
)


@router.get("/summary")
async def summary(admin: Admin, db: Engine) -> dict[str, Any]:
    """Все счётчики первого экрана."""

    row = await fetch_one(db, SUMMARY) or {}
    run = await fetch_one(db, LAST_RUN)
    errors = await fetch_all(db, RECENT_ERRORS)
    return {
        "corpus": {
            "items": row.get("items", 0),
            "new_24h": row.get("items_24h", 0),
            "ranked": row.get("ranked", 0),
            "analyzed": row.get("analyzed", 0),
        },
        "harvest": {
            "last_run": run,
            "status": (run or {}).get("status"),
            "funnel": {
                "fetched": (run or {}).get("fetched", 0),
                "accepted": (run or {}).get("accepted", 0),
                "borderline": (run or {}).get("borderline", 0),
                "rejected": (run or {}).get("rejected", 0),
            },
        },
        "subscribers": {
            "total": row.get("subscribers", 0),
            "active": row.get("subscribers_active", 0),
            "users": row.get("subscribers_users", 0),
            "groups": row.get("subscribers_groups", 0),
            "channels": row.get("subscribers_channels", 0),
            "pending": row.get("subscribers_pending", 0),
        },
        "subscriptions": {
            "active": row.get("subs_active", 0),
            "expiring_7d": row.get("subs_expiring", 0),
            "expired": row.get("subs_expired", 0),
        },
        "delivery": {
            "queued": row.get("delivery_queued", 0),
            "sent_24h": row.get("delivery_sent_24h", 0),
            "failed_24h": row.get("delivery_failed_24h", 0),
        },
        "llm": {
            "calls_24h": row.get("llm_calls_24h", 0),
            "tokens_24h": int(row.get("llm_tokens_24h") or 0),
            "cost_24h": float(row.get("llm_cost_24h") or 0),
        },
        "errors": errors,
    }


@router.get("/funnel")
async def funnel(
    admin: Admin, db: Engine, days: Annotated[int, Query(ge=1, le=365)] = 30
) -> dict[str, Any]:
    """Воронка сбора по дням. Ряды подписаны слотом цвета — см. analytics.md."""

    rows = await fetch_all(
        db,
        text(
            """
            SELECT d::date AS day,
                   coalesce(sum(m.fetched), 0) AS fetched,
                   coalesce(sum(m.accepted), 0) AS accepted,
                   coalesce(sum(m.borderline), 0) AS borderline,
                   coalesce(sum(m.rejected), 0) AS rejected
              FROM generate_series(
                     current_date - make_interval(days => :days - 1), current_date, '1 day'
                   ) AS d
              LEFT JOIN metrics_harvest_daily m ON m.day = d::date
             GROUP BY d::date
             ORDER BY d::date
            """
        ),
        {"days": days},
    )
    return {
        "days": days,
        "series": [
            {"key": "accepted", "label": "Принято", "color_slot": 1},
            {"key": "borderline", "label": "Пограничные", "color_slot": 2},
            {"key": "rejected", "label": "Отклонено", "color_slot": 3},
        ],
        "points": rows,
    }


@router.get("/timeline")
async def timeline(
    admin: Admin, db: Engine, days: Annotated[int, Query(ge=1, le=365)] = 30
) -> dict[str, Any]:
    """Прогоны сбора и доставки на одной оси — видно, что за чем сломалось."""

    rows = await fetch_all(
        db,
        text(
            """
            SELECT d::date AS day,
                   coalesce(h.stored, 0) AS items,
                   coalesce(dl.sent, 0) AS sent,
                   coalesce(dl.failed, 0) AS failed
              FROM generate_series(
                     current_date - make_interval(days => :days - 1), current_date, '1 day'
                   ) AS d
              LEFT JOIN (
                    SELECT day, sum(stored) AS stored
                      FROM metrics_harvest_daily GROUP BY day
                   ) h ON h.day = d::date
              LEFT JOIN (
                    SELECT day, sum(jobs_sent) AS sent, sum(jobs_failed) AS failed
                      FROM metrics_delivery_daily GROUP BY day
                   ) dl ON dl.day = d::date
             ORDER BY d::date
            """
        ),
        {"days": days},
    )
    return {
        "days": days,
        "series": [
            {"key": "items", "label": "Новые материалы", "color_slot": 1},
            {"key": "sent", "label": "Отправлено", "color_slot": 2},
            {"key": "failed", "label": "Провалено", "color_slot": "critical"},
        ],
        "points": rows,
    }
