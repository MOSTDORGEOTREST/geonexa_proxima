"""Здоровье платформы и журнал действий администратора."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from geonexa_proxima.api.admin.deps import (
    Admin,
    AppSettings,
    Engine,
    Paging,
    fetch_all,
    fetch_one,
    page_response,
)

router = APIRouter(tags=["admin:system"])


@router.get("/health")
async def health(
    admin: Admin, db: Engine, settings: AppSettings, request: Request
) -> dict[str, Any]:
    """Состояние всех внешних зависимостей одним ответом.

    Каждая проверка изолирована: недоступный Prefect не должен мешать увидеть,
    что с базой всё в порядке.
    """

    report: dict[str, Any] = {}

    try:
        row = await fetch_one(
            db,
            text(
                "SELECT version() AS version,"
                " (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database())"
                " AS connections"
            ),
        )
        from geonexa_proxima.db.session import pool_snapshot

        report["postgres"] = {"ok": True, **(row or {}), "pool": pool_snapshot(db)}
    except Exception as error:
        report["postgres"] = {"ok": False, "error": str(error)[:300]}

    container = getattr(request.app.state, "container", None)
    report["components"] = container.readiness() if container else {}

    try:
        from geonexa_proxima.services.prefect_admin import PrefectAdmin

        client = getattr(request.app.state, "prefect_admin", None) or PrefectAdmin(settings, db)
        request.app.state.prefect_admin = client
        report["prefect"] = await client.health()
    except Exception as error:
        report["prefect"] = {"ok": False, "error": str(error)[:300]}

    if container is not None and hasattr(container, "telegram_bot"):
        try:
            me = await container.telegram_bot().get_me()
            report["telegram"] = {"ok": True, "username": me.username, "id": me.id}
        except Exception as error:
            report["telegram"] = {"ok": False, "error": str(error)[:300]}
    else:
        report["telegram"] = {"ok": False, "error": "бот не сконфигурирован"}

    report["schema"] = getattr(request.app.state, "bootstrap", None)
    return report


@router.get("/audit")
async def audit_log(
    admin: Admin,
    db: Engine,
    paging: Paging,
    actor: str | None = None,
    action: str | None = None,
    entity_type: str | None = None,
) -> dict[str, Any]:
    conditions = ["true"]
    params: dict[str, Any] = {"limit": paging.limit, "offset": paging.offset}
    for name, value in (("actor", actor), ("action", action), ("entity_type", entity_type)):
        if value:
            conditions.append(f"{name} = :{name}")
            params[name] = value
    where = " AND ".join(conditions)
    rows = await fetch_all(
        db,
        text(
            f"SELECT id, actor, action, entity_type, entity_id, before, after, ip::text,"
            f" created_at FROM admin_audit_log WHERE {where}"
            f" ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    total = await fetch_one(
        db,
        text(f"SELECT count(*) AS n FROM admin_audit_log WHERE {where}"),
        {k: v for k, v in params.items() if k not in {"limit", "offset"}},
    )
    return page_response(rows, int((total or {}).get("n", 0)), paging)


@router.get("/analytics/overview")
async def analytics_overview(
    admin: Admin, db: Engine, days: Annotated[int, Query(ge=1, le=365)] = 30
) -> dict[str, Any]:
    """Сводные ряды для экрана аналитики.

    Слот цвета приходит с бэкенда, а не выбирается на фронте по индексу
    массива: иначе фильтр, убравший одну серию, перекрасил бы остальные.
    """

    subscribers = await fetch_all(
        db,
        text(
            "SELECT day, kind, total, total_active, registered, activated, churned,"
            " dau, wau, mau, with_subscription FROM metrics_subscribers_daily"
            " WHERE day >= current_date - make_interval(days => :days) ORDER BY day, kind"
        ),
        {"days": days},
    )
    delivery = await fetch_all(
        db,
        text(
            "SELECT day, channel, jobs_sent AS sent, jobs_failed AS failed,"
            " messages_sent, recipients FROM metrics_delivery_daily"
            " WHERE day >= current_date - make_interval(days => :days) ORDER BY day, channel"
        ),
        {"days": days},
    )
    harvest = await fetch_all(
        db,
        text(
            "SELECT day, source, fetched, accepted, borderline, rejected, stored"
            " FROM metrics_harvest_daily"
            " WHERE day >= current_date - make_interval(days => :days) ORDER BY day, source"
        ),
        {"days": days},
    )
    engagement = await fetch_all(
        db,
        text(
            "SELECT day, digests_sent, items_delivered, feedback_total, unique_reactors,"
            " empty_digests, engagement_rate, avg_items_per_digest"
            " FROM metrics_engagement_daily"
            " WHERE day >= current_date - make_interval(days => :days) ORDER BY day"
        ),
        {"days": days},
    )
    return {
        "days": days,
        "subscribers": subscribers,
        "delivery": delivery,
        "harvest": harvest,
        "engagement": engagement,
        "series": {
            "subscribers": [
                {"key": "user", "label": "Личные чаты", "color_slot": 1},
                {"key": "group", "label": "Группы", "color_slot": 2},
                {"key": "channel", "label": "Каналы", "color_slot": 3},
            ],
            "delivery": [
                {"key": "personal", "label": "Личные", "color_slot": 1},
                {"key": "group", "label": "Чаты", "color_slot": 2},
            ],
        },
    }


@router.get("/analytics/retention")
async def retention(
    admin: Admin, db: Engine, weeks: Annotated[int, Query(ge=1, le=52)] = 12
) -> list[dict[str, Any]]:
    """Тепловая карта удержания по когортам."""

    return await fetch_all(
        db,
        text(
            "SELECT cohort_week, week_offset, kind, cohort_size, retained,"
            " round((100.0 * retained / nullif(cohort_size, 0))::numeric, 1) AS percent"
            " FROM metrics_retention"
            " WHERE cohort_week >= current_date - make_interval(weeks => :weeks)"
            " ORDER BY cohort_week, week_offset"
        ),
        {"weeks": weeks},
    )
