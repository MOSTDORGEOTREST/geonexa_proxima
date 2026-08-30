"""Вход в админку: пароль, токены, ограничение попыток.

Таблицы администраторов нет — учётные данные берутся из окружения. Это
сознательное упрощение: администратор один, и хранить его в той же базе,
которую он чинит, значит потерять доступ ровно тогда, когда он нужнее всего.

Пароль сверяется по argon2-хешу, если он задан; plaintext допускается только
вне production, и конфигурация с plaintext-паролем в production отклоняется
ещё на старте (`Settings`).
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from geonexa_proxima.config import Settings, get_settings

ALGORITHM = "HS256"


def constant_time_equals(left: str, right: str) -> bool:
    """Сравнение за постоянное время, устойчивое к любым символам."""

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


ACCESS = "access"
REFRESH = "refresh"

_bearer = HTTPBearer(auto_error=False)


class PasswordChecker:
    """Проверка пароля: argon2, если хеш задан, иначе постоянное сравнение."""

    def __init__(self, settings: Settings) -> None:
        self._hash = (settings.admin_password_hash or "").strip()
        self._plain = settings.admin_password.get_secret_value() if settings.admin_password else ""
        self._verifier: Any | None = None
        if self._hash:
            from argon2 import PasswordHasher

            self._verifier = PasswordHasher()

    def verify(self, password: str) -> bool:
        if self._verifier is not None:
            from argon2.exceptions import VerificationError, VerifyMismatchError

            try:
                return bool(self._verifier.verify(self._hash, password))
            except (VerifyMismatchError, VerificationError):
                return False
            except Exception:
                return False
        if not self._plain:
            return False
        # compare_digest, а не ==: время сравнения не должно зависеть от того,
        # сколько первых символов пароля угадано. Сравниваем байты: на строках
        # с не-ASCII символами compare_digest бросает TypeError, и вход с
        # кириллическим паролем падал бы пятисоткой вместо отказа.
        return constant_time_equals(self._plain, password)


@dataclass
class LoginThrottle:
    """Ограничение попыток входа на IP.

    Скользящее окно в памяти процесса. Этого достаточно: админка запущена в
    одном экземпляре, а против распределённого перебора всё равно нужен слой
    выше — фаервол или reverse proxy.
    """

    limit: int = 5
    window_seconds: int = 60
    _attempts: dict[str, list[float]] = field(default_factory=dict)

    def hit(self, key: str) -> bool:
        """Зафиксировать попытку. False — лимит исчерпан."""

        now = time.monotonic()
        window = [t for t in self._attempts.get(key, []) if now - t < self.window_seconds]
        if len(window) >= self.limit:
            self._attempts[key] = window
            return False
        window.append(now)
        self._attempts[key] = window
        return True

    def reset(self, key: str) -> None:
        self._attempts.pop(key, None)

    def retry_after(self, key: str) -> int:
        window = self._attempts.get(key)
        if not window:
            return 0
        return max(1, int(self.window_seconds - (time.monotonic() - min(window))))


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    """Кто именно сейчас работает в админке."""

    username: str
    issued_at: datetime
    expires_at: datetime


def issue_token(settings: Settings, *, kind: str = ACCESS) -> tuple[str, int]:
    """Выпустить токен. Возвращает сам токен и время жизни в секундах."""

    now = datetime.now(UTC)
    ttl = (
        timedelta(minutes=settings.admin_jwt_ttl_minutes)
        if kind == ACCESS
        else timedelta(days=settings.admin_refresh_ttl_days)
    )
    payload = {
        "sub": settings.admin_username,
        "typ": kind,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    token = jwt.encode(payload, settings.admin_jwt_secret.get_secret_value(), algorithm=ALGORITHM)
    return token, int(ttl.total_seconds())


def decode_token(settings: Settings, token: str, *, expect: str = ACCESS) -> AdminIdentity:
    """Разобрать токен. Любая проблема — 401, без подробностей наружу."""

    try:
        payload = jwt.decode(
            token, settings.admin_jwt_secret.get_secret_value(), algorithms=[ALGORITHM]
        )
    except jwt.ExpiredSignatureError as error:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Срок действия токена истёк",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    except jwt.PyJWTError as error:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Недействительный токен",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    if payload.get("typ") != expect:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail=f"Ожидался токен типа {expect}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if payload.get("sub") != settings.admin_username:
        # Логин сменили в .env — старые токены обязаны перестать работать.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Учётные данные изменились")
    return AdminIdentity(
        username=str(payload["sub"]),
        issued_at=datetime.fromtimestamp(payload["iat"], UTC),
        expires_at=datetime.fromtimestamp(payload["exp"], UTC),
    )


BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


async def current_admin(request: Request, credentials: BearerCredentials) -> AdminIdentity:
    """Зависимость авторизации для всех защищённых роутеров."""

    if credentials is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Требуется авторизация",
            headers={"WWW-Authenticate": "Bearer"},
        )
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    return decode_token(settings, credentials.credentials)


def client_ip(request: Request) -> str:
    """IP клиента с учётом обратного прокси."""

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def ip_or_none(request: Request) -> str | None:
    """IP для колонки inet — либо настоящий адрес, либо NULL.

    `client_ip` может вернуть «unknown» (ASGI-транспорт без клиента, часть
    прокси). CAST такого значения в inet падает, а аудит глотает исключения —
    и записи молча перестали бы появляться вовсе.
    """

    import ipaddress

    value = client_ip(request)
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return None
    return value


def mask_secret(value: str | None) -> str | None:
    """Показать достаточно, чтобы узнать ключ, и мало, чтобы им воспользоваться."""

    if not value:
        return None
    if len(value) <= 10:
        return "•" * len(value)
    return f"{value[:5]}…{value[-4:]}"
