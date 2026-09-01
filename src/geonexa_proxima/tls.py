"""Построение TLS-контекста для управляемого PostgreSQL.

Модуль лежит в корне пакета, потому что зависит только от стандартной
библиотеки: ``config`` импортирует его, не втягивая слой persistence и не
рискуя круговым импортом.

asyncpg не понимает libpq-параметр ``sslmode`` в строке подключения: SQLAlchemy
передаёт его драйверу как есть и соединение падает. Режим задаётся отдельной
настройкой, а драйверу отдаётся готовый ``ssl.SSLContext`` через connect_args.
"""

from __future__ import annotations

import ssl
from enum import StrEnum
from pathlib import Path


class SSLMode(StrEnum):
    """Подмножество libpq-режимов, которое имеет смысл для asyncpg."""

    DISABLE = "disable"
    ALLOW = "allow"
    PREFER = "prefer"
    REQUIRE = "require"
    VERIFY_CA = "verify-ca"
    VERIFY_FULL = "verify-full"


_VERIFYING = frozenset({SSLMode.VERIFY_CA, SSLMode.VERIFY_FULL})
_ENCRYPTING = frozenset({SSLMode.REQUIRE, SSLMode.VERIFY_CA, SSLMode.VERIFY_FULL})


def build_ssl_context(
    mode: SSLMode | str = SSLMode.PREFER,
    root_cert: str | Path | None = None,
) -> ssl.SSLContext | bool | None:
    """Вернуть значение для asyncpg-параметра ``ssl``.

    ``False`` отключает TLS, ``True`` включает без проверки сертификата,
    ``SSLContext`` включает проверку. Режимы verify-ca и verify-full требуют
    корневой сертификат: без сертификата проверка превращается в фикцию.
    """

    resolved = SSLMode(str(mode))
    if resolved is SSLMode.DISABLE:
        return False
    if resolved not in _VERIFYING:
        # allow/prefer/require: шифруем, но цепочку не проверяем.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    if not root_cert:
        raise ValueError(f"DATABASE_SSL_MODE={resolved} требует DATABASE_SSL_ROOT_CERT")
    path = Path(root_cert).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Корневой сертификат не найден: {path}")

    context = ssl.create_default_context(cafile=str(path))
    context.verify_mode = ssl.CERT_REQUIRED
    # verify-ca проверяет цепочку, verify-full ещё и совпадение имени хоста.
    context.check_hostname = resolved is SSLMode.VERIFY_FULL
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def requires_encryption(mode: SSLMode | str) -> bool:
    return SSLMode(str(mode)) in _ENCRYPTING


def asyncpg_connect_args(
    mode: SSLMode | str = SSLMode.PREFER,
    root_cert: str | Path | None = None,
    *,
    connect_timeout_seconds: float | None = None,
    command_timeout_seconds: float | None = None,
    statement_timeout_ms: int | None = None,
    application_name: str = "geonexa-proxima",
) -> dict[str, object]:
    """Собрать connect_args для create_async_engine.

    ``timeout`` ограничивает установку соединения, ``command_timeout`` — время
    одного запроса. Без второго зависший запрос к медленной базе держит
    соединение занятым, и пул кончается быстрее, чем приходит таймаут.
    """

    args: dict[str, object] = {
        "ssl": build_ssl_context(mode, root_cert),
        "server_settings": {"application_name": application_name},
    }
    if connect_timeout_seconds:
        args["timeout"] = float(connect_timeout_seconds)
    if command_timeout_seconds:
        args["command_timeout"] = float(command_timeout_seconds)
    if statement_timeout_ms:
        # asyncpg не умеет statement_timeout параметром — только через GUC.
        args["server_settings"]["statement_timeout"] = str(int(statement_timeout_ms))  # type: ignore[index]
    return args
