"""Реестр моделей: провайдеры, модели и привязка ролей.

Экран ролей — это и есть «настройка ризонинга отдельно для лёгких и тяжёлых
действий»: восемь строк, в каждой своя модель и свой уровень рассуждения.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from geonexa_proxima.api.admin.deps import (
    Admin,
    AppSettings,
    Engine,
    audit,
    fetch_all,
    fetch_one,
    require,
    returning,
)
from geonexa_proxima.api.admin.security import mask_secret

router = APIRouter(prefix="/llm", tags=["admin:llm"])

ROLES = (
    "ranker",
    "explainer",
    "profile_compiler",
    "query_expander",
    "digest_writer",
    "analyzer",
    "deep_dive",
    "chat",
)

REASONING_LEVELS = ("none", "low", "high", "max")


class ProviderIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    protocol: str = "openai_compatible"
    base_url: str
    api_key: str | None = None
    enabled: bool = True


class ModelIn(BaseModel):
    provider_key: str
    key: str = Field(min_length=1, max_length=64)
    model_name: str
    display_name: str | None = None
    tier: str | None = None
    supports_reasoning: bool = False
    reasoning_style: str = "none"
    supports_json_mode: bool = True
    context_window: int | None = None
    max_output_tokens: int | None = None
    enabled: bool = True


class RoleBinding(BaseModel):
    model_key: str
    fallback_model_key: str | None = None
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=200_000)
    reasoning_effort: str | None = None
    json_mode: bool = True
    timeout_seconds: int = Field(default=120, ge=1, le=600)
    concurrency: int = Field(default=4, ge=1, le=64)
    system_prompt_override: str | None = None
    enabled: bool = True


@router.get("/providers")
async def providers(admin: Admin, db: Engine) -> list[dict[str, Any]]:
    rows = await fetch_all(db, text("SELECT * FROM llm_providers ORDER BY key"))
    for row in rows:
        # Ключ показываем ровно настолько, чтобы его узнать, но не использовать.
        row["api_key"] = mask_secret(row.pop("api_key_encrypted", None) or row.get("api_key"))
    return rows


@router.get("/models")
async def models(admin: Admin, db: Engine) -> list[dict[str, Any]]:
    return await fetch_all(
        db,
        text(
            "SELECT m.*, p.key AS provider_key, p.name AS provider_name, p.base_url"
            " FROM llm_models m JOIN llm_providers p ON p.id = m.provider_id"
            " ORDER BY p.key, m.key"
        ),
    )


@router.post("/models", status_code=status.HTTP_201_CREATED)
async def create_model(
    payload: ModelIn, admin: Admin, db: Engine, request: Request
) -> dict[str, Any]:
    """Добавить свою модель по API — то, ради чего реестр и существует."""

    provider = require(
        await fetch_one(
            db,
            text("SELECT id FROM llm_providers WHERE key = :key"),
            {"key": payload.provider_key},
        ),
        "Провайдер",
    )
    row = await returning(
        db,
        text(
            "INSERT INTO llm_models (id, provider_id, key, model_name, display_name, tier,"
            " supports_reasoning, reasoning_style, supports_json_mode, context_window,"
            " max_output_tokens, enabled) VALUES (gen_random_uuid(), :provider_id, :key,"
            " :model_name, :display_name, :tier, :supports_reasoning, :reasoning_style,"
            " :supports_json_mode, :context_window, :max_output_tokens, :enabled)"
            " ON CONFLICT (provider_id, key) DO UPDATE SET model_name = EXCLUDED.model_name,"
            " display_name = EXCLUDED.display_name, tier = EXCLUDED.tier,"
            " supports_reasoning = EXCLUDED.supports_reasoning,"
            " reasoning_style = EXCLUDED.reasoning_style, enabled = EXCLUDED.enabled,"
            " updated_at = now() RETURNING *"
        ),
        {
            **payload.model_dump(exclude={"provider_key"}),
            "provider_id": str(provider["id"]),
        },
    )
    await audit(
        db,
        admin,
        request,
        action="llm.model_upsert",
        entity_type="llm_model",
        entity_id=str(row["id"]),
        payload={"key": payload.key},
    )
    return row


@router.get("/roles")
async def roles(admin: Admin, db: Engine) -> list[dict[str, Any]]:
    """Все роли с текущими привязками. Роль без привязки — тоже строка."""

    bound = await fetch_all(
        db,
        text(
            "SELECT b.*, m.key AS model_key, m.model_name, m.supports_reasoning,"
            " m.reasoning_style, m.reasoning_levels, p.key AS provider_key"
            " FROM llm_role_bindings b"
            " JOIN llm_models m ON m.id = b.model_id"
            " JOIN llm_providers p ON p.id = m.provider_id"
        ),
    )
    by_role = {row["role"]: row for row in bound}
    return [
        by_role.get(role, {"role": role, "model_key": None, "enabled": False}) for role in ROLES
    ]


@router.put("/roles/{role}")
async def bind_role(
    role: str, payload: RoleBinding, admin: Admin, db: Engine, request: Request
) -> dict[str, Any]:
    """Назначить роли модель и уровень рассуждения."""

    if role not in ROLES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Неизвестная роль: {role}")
    if payload.reasoning_effort and payload.reasoning_effort not in REASONING_LEVELS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="reasoning_effort: none, low, high или max",
        )
    model = require(
        await fetch_one(
            db,
            text("SELECT id, supports_reasoning FROM llm_models WHERE key = :key"),
            {"key": payload.model_key},
        ),
        "Модель",
    )
    if payload.reasoning_effort not in (None, "none") and not model["supports_reasoning"]:
        # Молча проглотить настройку хуже, чем отказать: администратор был бы
        # уверен, что включил рассуждение, а его бы не было.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Модель {payload.model_key} не поддерживает рассуждение",
        )
    fallback = None
    if payload.fallback_model_key:
        fallback = require(
            await fetch_one(
                db,
                text("SELECT id FROM llm_models WHERE key = :key"),
                {"key": payload.fallback_model_key},
            ),
            "Запасная модель",
        )["id"]
    row = await returning(
        db,
        text(
            "INSERT INTO llm_role_bindings (id, role, model_id, fallback_model_id, temperature,"
            " max_tokens, reasoning_effort, json_mode, timeout_seconds, concurrency,"
            " system_prompt_override, enabled, updated_by)"
            " VALUES (gen_random_uuid(), :role, :model_id, :fallback_id, :temperature,"
            " :max_tokens, :reasoning_effort, :json_mode, :timeout_seconds, :concurrency,"
            " :system_prompt_override, :enabled, :actor)"
            " ON CONFLICT (role) DO UPDATE SET model_id = EXCLUDED.model_id,"
            " fallback_model_id = EXCLUDED.fallback_model_id,"
            " temperature = EXCLUDED.temperature, max_tokens = EXCLUDED.max_tokens,"
            " reasoning_effort = EXCLUDED.reasoning_effort, json_mode = EXCLUDED.json_mode,"
            " timeout_seconds = EXCLUDED.timeout_seconds, concurrency = EXCLUDED.concurrency,"
            " system_prompt_override = EXCLUDED.system_prompt_override,"
            " enabled = EXCLUDED.enabled, updated_by = EXCLUDED.updated_by,"
            " updated_at = now() RETURNING *"
        ),
        {
            "role": role,
            "model_id": str(model["id"]),
            "fallback_id": str(fallback) if fallback else None,
            "temperature": payload.temperature,
            "max_tokens": payload.max_tokens,
            "reasoning_effort": payload.reasoning_effort,
            "json_mode": payload.json_mode,
            "timeout_seconds": payload.timeout_seconds,
            "concurrency": payload.concurrency,
            "system_prompt_override": payload.system_prompt_override,
            "enabled": payload.enabled,
            "actor": admin.username,
        },
    )
    await audit(
        db,
        admin,
        request,
        action="llm.role_bind",
        entity_type="llm_role",
        entity_id=role,
        payload={"model": payload.model_key, "reasoning": payload.reasoning_effort},
    )
    return row


@router.post("/providers/{provider_key}/test")
async def test_provider(
    provider_key: str, admin: Admin, db: Engine, settings: AppSettings
) -> dict[str, Any]:
    """Пробный вызов: доступен ли провайдер и с какой задержкой."""

    import time

    import httpx

    provider = require(
        await fetch_one(
            db, text("SELECT * FROM llm_providers WHERE key = :key"), {"key": provider_key}
        ),
        "Провайдер",
    )
    base = str(provider["base_url"]).rstrip("/")
    key = settings.llm_api_key(heavy=False)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{base}/models", headers={"Authorization": f"Bearer {key}"}
            )
        latency = int((time.monotonic() - started) * 1000)
        available = [m.get("id") for m in (response.json().get("data") or [])][:50]
        return {
            "ok": response.status_code < 400,
            "status_code": response.status_code,
            "latency_ms": latency,
            "models": available,
        }
    except Exception as error:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": str(error)[:300],
        }


@router.get("/usage")
async def usage(
    admin: Admin,
    db: Engine,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    group_by: Annotated[str, Query(pattern="^(role|model|day)$")] = "day",
) -> dict[str, Any]:
    """Расход по ролям, моделям или дням — сколько это всё стоит."""

    column = {"role": "role", "model": "model_id::text", "day": "created_at::date::text"}[group_by]
    rows = await fetch_all(
        db,
        text(
            f"SELECT {column} AS bucket, count(*) AS calls,"
            " coalesce(sum(coalesce(prompt_tokens, 0) + coalesce(completion_tokens, 0)"
            " + coalesce(reasoning_tokens, 0)), 0) AS tokens,"
            " coalesce(sum(cost_usd), 0) AS cost,"
            " count(*) FILTER (WHERE status <> 'ok') AS errors,"
            " round(avg(latency_ms)) AS avg_latency_ms"
            " FROM llm_call_log WHERE created_at >= now() - make_interval(days => :days)"
            " GROUP BY 1 ORDER BY 1"
        ),
        {"days": days},
    )
    return {"group_by": group_by, "days": days, "rows": rows}
