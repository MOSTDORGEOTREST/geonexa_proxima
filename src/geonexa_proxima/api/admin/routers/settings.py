"""Настройки: что взято из .env, что переопределено в базе, что менять нельзя."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text

from geonexa_proxima.api.admin.deps import (
    Admin,
    AppSettings,
    Engine,
    audit,
    execute,
    fetch_all,
    fetch_one,
)
from geonexa_proxima.api.admin.security import mask_secret
from geonexa_proxima.bootstrap.seed import ENV_ONLY

router = APIRouter(prefix="/settings", tags=["admin:settings"])


class SettingValue(BaseModel):
    value: Any


@router.get("")
async def list_settings(
    admin: Admin, db: Engine, settings: AppSettings, scope: str | None = None
) -> list[dict[str, Any]]:
    """Все настройки с действующим значением.

    Колонка `effective` — то, что реально применяется: значение из базы, если
    оно есть, иначе из `.env`. Без неё администратор гадает, подействовала ли
    правка.
    """

    rows = await fetch_all(
        db,
        text(
            "SELECT key, value, value_type, env_default, scope, description, is_secret,"
            " is_env_only, updated_by, updated_at FROM app_settings"
            " WHERE (CAST(:scope AS text) IS NULL OR scope = :scope) ORDER BY scope, key"
        ),
        {"scope": scope},
    )
    for row in rows:
        overridden = row["updated_by"] not in (None, "seed")
        row["overridden"] = overridden
        row["effective"] = row["value"] if overridden else row["env_default"]
        if row["is_secret"]:
            row["value"] = mask_secret(str(row["value"]))
            row["env_default"] = mask_secret(str(row["env_default"]))
            row["effective"] = mask_secret(str(row["effective"]))
    return rows


@router.get("/env-diff")
async def env_diff(admin: Admin, db: Engine) -> list[dict[str, Any]]:
    """Что переопределено относительно `.env` — короткий список для ревизии."""

    return await fetch_all(
        db,
        text(
            "SELECT key, value, env_default, scope, updated_by, updated_at FROM app_settings"
            " WHERE updated_by IS NOT NULL AND updated_by <> 'seed'"
            " AND value IS DISTINCT FROM env_default ORDER BY updated_at DESC"
        ),
    )


@router.put("/{key}")
async def set_setting(
    key: str, payload: SettingValue, admin: Admin, db: Engine, request: Request
) -> dict[str, Any]:
    """Переопределить настройку.

    Часть настроек читается до подключения к базе либо даёт доступ к ней самой;
    менять такие через админку бессмысленно и опасно — они помечены `is_env_only`.
    """

    row = await fetch_one(
        db, text("SELECT key, is_env_only FROM app_settings WHERE key = :key"), {"key": key}
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Нет такой настройки")
    if row["is_env_only"] or key.lower() in ENV_ONLY:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{key} задаётся только через .env: она нужна до подключения к базе",
        )
    await execute(
        db,
        text(
            "UPDATE app_settings SET value = CAST(:value AS jsonb), updated_by = :actor,"
            " updated_at = now() WHERE key = :key"
        ),
        {
            "key": key,
            "value": json.dumps(payload.value, ensure_ascii=False, default=str),
            "actor": admin.username,
        },
    )
    await audit(db, admin, request, action="settings.update", entity_type="setting", entity_id=key)
    return {"key": key, "value": payload.value, "applies_after": "перезапуск сервиса"}


@router.delete("/{key}")
async def reset_setting(key: str, admin: Admin, db: Engine, request: Request) -> dict[str, Any]:
    """Вернуть настройку к значению из `.env`."""

    updated = await execute(
        db,
        text(
            "UPDATE app_settings SET value = env_default, updated_by = 'seed',"
            " updated_at = now() WHERE key = :key AND env_default IS NOT NULL"
        ),
        {"key": key},
    )
    if not updated:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Настройка не найдена или у неё нет значения из .env"
        )
    await audit(db, admin, request, action="settings.reset", entity_type="setting", entity_id=key)
    return {"key": key, "reset": True}
