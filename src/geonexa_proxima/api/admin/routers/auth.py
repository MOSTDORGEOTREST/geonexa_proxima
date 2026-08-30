"""Вход, обновление токена и «кто я»."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from geonexa_proxima.api.admin import security
from geonexa_proxima.api.admin.deps import Admin, AppSettings, Engine, audit
from geonexa_proxima.api.admin.security import client_ip, ip_or_none

router = APIRouter(prefix="/auth", tags=["admin:auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=500)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest, request: Request, response: Response, settings: AppSettings, db: Engine
) -> TokenResponse:
    """Выдать пару токенов. Неудачные попытки считаются и пишутся в аудит."""

    throttle: security.LoginThrottle = request.app.state.login_throttle
    key = client_ip(request)
    if not throttle.hit(key):
        retry = throttle.retry_after(key)
        response.headers["Retry-After"] = str(retry)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Слишком много попыток входа, повторите через {retry} с",
            headers={"Retry-After": str(retry)},
        )

    checker: security.PasswordChecker = request.app.state.password_checker
    # Логин сверяется тем же способом, что и пароль: разное время ответа на
    # «нет такого пользователя» и «неверный пароль» — это подсказка.
    username_ok = security.constant_time_equals(settings.admin_username, payload.username)
    if not (username_ok and checker.verify(payload.password)):
        await _audit_failure(db, request, payload.username)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Неверный логин или пароль")

    throttle.reset(key)
    access, expires_in = security.issue_token(settings, kind=security.ACCESS)
    refresh, _ = security.issue_token(settings, kind=security.REFRESH)
    now = datetime.now(UTC)
    identity = security.AdminIdentity(
        username=settings.admin_username, issued_at=now, expires_at=now
    )
    await audit(db, identity, request, action="auth.login")
    return TokenResponse(access_token=access, refresh_token=refresh, expires_in=expires_in)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, settings: AppSettings) -> TokenResponse:
    """Обновить access по refresh. Refresh тоже перевыпускается — ротация."""

    security.decode_token(settings, payload.refresh_token, expect=security.REFRESH)
    access, expires_in = security.issue_token(settings, kind=security.ACCESS)
    new_refresh, _ = security.issue_token(settings, kind=security.REFRESH)
    return TokenResponse(access_token=access, refresh_token=new_refresh, expires_in=expires_in)


@router.post("/logout")
async def logout(admin: Admin, request: Request, db: Engine) -> dict[str, bool]:
    """Отметить выход в аудите.

    Сервер не хранит список выданных токенов, поэтому «отозвать» нечего:
    сессию забывает клиент, удаляя cookie. Честнее сказать это прямо, чем
    делать вид, что токен аннулирован.
    """

    await audit(db, admin, request, action="auth.logout")
    return {"ok": True}


@router.get("/me")
async def me(admin: Admin, settings: AppSettings) -> dict[str, object]:
    return {
        "username": admin.username,
        "expires_at": admin.expires_at,
        "environment": settings.environment.value,
        "app_name": settings.app_name,
    }


async def _audit_failure(db: AsyncEngine, request: Request, username: str) -> None:
    """Записать неудачный вход отдельно: подбор пароля виден только по ним."""

    try:
        async with db.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO admin_audit_log (actor, action, after, ip,"
                    " user_agent) VALUES (:actor, 'auth.login_failed',"
                    " CAST(:payload AS jsonb), CAST(:ip AS inet), :agent)"
                ),
                {
                    "actor": username[:200],
                    "payload": "{}",
                    "ip": ip_or_none(request),
                    "agent": request.headers.get("user-agent", "")[:500] or None,
                },
            )
    except Exception:
        return
