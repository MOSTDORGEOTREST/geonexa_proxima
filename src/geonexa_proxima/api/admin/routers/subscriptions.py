"""Тарифы и подписки с датами действия."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from geonexa_proxima.api.admin.deps import (
    Admin,
    Engine,
    Paging,
    Subscribers,
    audit,
    conflict_from,
    execute,
    fetch_all,
    fetch_one,
    page_response,
    require,
    returning,
)
from geonexa_proxima.db.subscriber_repository import (
    PlanNotFoundError,
    SubscriberNotFoundError,
    SubscriptionNotFoundError,
    SubscriptionOverlapError,
)

router = APIRouter(tags=["admin:subscriptions"])


class PlanIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    max_profiles: int = Field(default=1, ge=1, le=100)
    max_items_per_digest: int = Field(default=20, ge=1, le=100)
    min_interval_hours: int = Field(default=168, ge=1, le=8760)
    deep_analysis_quota_per_month: int = Field(default=0, ge=0, le=100_000)
    allow_group_chats: bool = False
    enabled: bool = True


class GrantIn(BaseModel):
    subscriber_id: UUID
    plan_key: str
    status: str = "active"
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    days: int | None = Field(default=None, ge=1, le=3650)
    grace_days: int | None = Field(default=None, ge=0, le=365)
    auto_renew: bool = False
    notes: str | None = None
    replace_current: bool = True

    @model_validator(mode="after")
    def check_period(self) -> GrantIn:
        if self.ends_at is None and self.days is None:
            # Бессрочная подписка — законный случай, но он должен быть выбран
            # явно, а не получиться из-за незаполненного поля формы.
            return self
        if self.ends_at is not None and self.days is not None:
            raise ValueError("укажите либо ends_at, либо days")
        return self


class ExtendIn(BaseModel):
    days: int | None = Field(default=None, ge=1, le=3650)
    until: datetime | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> ExtendIn:
        if (self.days is None) == (self.until is None):
            raise ValueError("укажите либо days, либо until")
        return self


@router.get("/plans")
async def list_plans(admin: Admin, db: Engine, enabled_only: bool = False) -> list[dict[str, Any]]:
    clause = " WHERE enabled" if enabled_only else ""
    return await fetch_all(
        db,
        text(f"SELECT * FROM subscription_plans{clause} ORDER BY min_interval_hours, key"),
    )


@router.post("/plans", status_code=status.HTTP_201_CREATED)
async def create_plan(
    payload: PlanIn, admin: Admin, db: Engine, request: Request
) -> dict[str, Any]:
    try:
        row = await returning(
            db,
            text(
                "INSERT INTO subscription_plans (id, key, name, description, max_profiles,"
                " max_items_per_digest, min_interval_hours, deep_analysis_quota_per_month,"
                " allow_group_chats, enabled) VALUES (gen_random_uuid(), :key, :name,"
                " :description, :max_profiles, :max_items_per_digest, :min_interval_hours,"
                " :deep_analysis_quota_per_month, :allow_group_chats, :enabled) RETURNING *"
            ),
            payload.model_dump(),
        )
    except IntegrityError as error:
        raise conflict_from(error) from error
    await audit(
        db,
        admin,
        request,
        action="plan.create",
        entity_type="plan",
        entity_id=str(row["id"]),
        payload={"key": payload.key},
    )
    return row


@router.patch("/plans/{plan_id}")
async def patch_plan(
    plan_id: UUID, payload: PlanIn, admin: Admin, db: Engine, request: Request
) -> dict[str, Any]:
    changes = payload.model_dump()
    assignments = ", ".join(f"{key} = :{key}" for key in changes)
    updated = await execute(
        db,
        text(f"UPDATE subscription_plans SET {assignments}, updated_at = now() WHERE id = :id"),
        {**changes, "id": str(plan_id)},
    )
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Тариф не найден")
    await audit(
        db, admin, request, action="plan.update", entity_type="plan", entity_id=str(plan_id)
    )
    return {"updated": True}


@router.get("/subscriptions")
async def list_subscriptions(
    admin: Admin,
    db: Engine,
    paging: Paging,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    subscriber_id: UUID | None = None,
    expiring_days: Annotated[int | None, Query(ge=1, le=365)] = None,
) -> dict[str, Any]:
    conditions = ["true"]
    params: dict[str, Any] = {"limit": paging.limit, "offset": paging.offset}
    if status_filter:
        conditions.append("sub.status = :status")
        params["status"] = status_filter
    if subscriber_id:
        conditions.append("sub.subscriber_id = :subscriber_id")
        params["subscriber_id"] = str(subscriber_id)
    if expiring_days:
        conditions.append(
            "sub.ends_at BETWEEN now() AND now() + make_interval(days => :expiring_days)"
        )
        params["expiring_days"] = expiring_days
    where = " AND ".join(conditions)
    rows = await fetch_all(
        db,
        text(
            f"SELECT sub.*, p.key AS plan_key, p.name AS plan_name, s.title AS subscriber_title,"
            f" s.kind AS subscriber_kind, s.telegram_chat_id"
            f" FROM subscriptions sub"
            f" JOIN subscription_plans p ON p.id = sub.plan_id"
            f" JOIN subscribers s ON s.id = sub.subscriber_id"
            f" WHERE {where} ORDER BY sub.starts_at DESC LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    total = await fetch_one(
        db,
        text(f"SELECT count(*) AS n FROM subscriptions sub WHERE {where}"),
        {k: v for k, v in params.items() if k not in {"limit", "offset"}},
    )
    return page_response(rows, int((total or {}).get("n", 0)), paging)


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
async def grant(
    payload: GrantIn, admin: Admin, db: Engine, repository: Subscribers, request: Request
) -> dict[str, Any]:
    """Выдать подписку. Пересечение периодов запрещено самой базой."""

    try:
        record = await repository.grant_subscription(
            payload.subscriber_id,
            payload.plan_key,
            status=payload.status,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            duration=timedelta(days=payload.days) if payload.days else None,
            grace=timedelta(days=payload.grace_days) if payload.grace_days else None,
            auto_renew=payload.auto_renew,
            notes=payload.notes,
            actor=admin.username,
            replace_current=payload.replace_current,
        )
    except SubscriptionOverlapError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
    except (SubscriberNotFoundError, PlanNotFoundError) as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except IntegrityError as error:
        raise conflict_from(error) from error
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    await audit(
        db,
        admin,
        request,
        action="subscription.grant",
        entity_type="subscription",
        entity_id=str(record.id),
        payload={"plan": record.plan_key},
    )
    return _record(record)


@router.post("/subscriptions/{subscription_id}/extend")
async def extend(
    subscription_id: UUID,
    payload: ExtendIn,
    admin: Admin,
    db: Engine,
    repository: Subscribers,
    request: Request,
) -> dict[str, Any]:
    try:
        record = await repository.extend_subscription(
            subscription_id,
            until=payload.until,
            by=timedelta(days=payload.days) if payload.days else None,
            actor=admin.username,
        )
    except SubscriptionNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except IntegrityError as error:
        raise conflict_from(error) from error
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    await audit(
        db,
        admin,
        request,
        action="subscription.extend",
        entity_type="subscription",
        entity_id=str(subscription_id),
    )
    return _record(record)


@router.post("/subscriptions/{subscription_id}/cancel")
async def cancel(
    subscription_id: UUID,
    admin: Admin,
    db: Engine,
    repository: Subscribers,
    request: Request,
    reason: str | None = None,
) -> dict[str, Any]:
    try:
        record = await repository.cancel_subscription(
            subscription_id, actor=admin.username, reason=reason
        )
    except SubscriptionNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    await audit(
        db,
        admin,
        request,
        action="subscription.cancel",
        entity_type="subscription",
        entity_id=str(subscription_id),
        payload={"reason": reason},
    )
    return _record(record)


@router.get("/subscriptions/expiring")
async def expiring(
    admin: Admin,
    repository: Subscribers,
    days: Annotated[int, Query(ge=1, le=365)] = 7,
) -> list[dict[str, Any]]:
    records = await repository.list_expiring(within=timedelta(days=days))
    return [_record(record) for record in records]


@router.get("/subscriptions/{subscription_id}")
async def get_subscription(subscription_id: UUID, admin: Admin, db: Engine) -> dict[str, Any]:
    row = require(
        await fetch_one(
            db,
            text(
                "SELECT sub.*, p.key AS plan_key, p.name AS plan_name FROM subscriptions sub"
                " JOIN subscription_plans p ON p.id = sub.plan_id WHERE sub.id = :id"
            ),
            {"id": str(subscription_id)},
        ),
        "Подписка",
    )
    events = await fetch_all(
        db,
        text(
            "SELECT event, payload, actor, created_at FROM subscription_events"
            " WHERE subscription_id = :id ORDER BY created_at DESC"
        ),
        {"id": str(subscription_id)},
    )
    return {"subscription": row, "events": events}


@router.post("/subscriptions/expire-due")
async def expire_due(
    admin: Admin, db: Engine, repository: Subscribers, request: Request
) -> dict[str, int]:
    """Погасить всё просроченное прямо сейчас, не дожидаясь расписания."""

    count = await repository.expire_due(now=datetime.now(UTC))
    await audit(db, admin, request, action="subscription.expire_due", payload={"count": count})
    return {"expired": count}


def _record(record: Any) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "subscriber_id": str(record.subscriber_id),
        "plan_key": record.plan_key,
        "plan_name": record.plan_name,
        "status": record.status,
        "starts_at": record.starts_at,
        "ends_at": record.ends_at,
        "grace_until": record.grace_until,
        "auto_renew": record.auto_renew,
        "source": record.source,
        "notes": record.notes,
        "is_running": record.is_running(),
    }
