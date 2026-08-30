"""Async engine, пул соединений и короткоживущие сессии.

Управляемый PostgreSQL считает соединения, а не запросы: у него жёсткий
`max_connections`, общий на все процессы. Платформа же состоит из api, бота,
prefect-worker и параллельных флоу — каждый со своим пулом. Поэтому здесь три
правила, без которых база кончается раньше, чем нагрузка:

* **Пул маленький и с потолком.** ``DB_CONNECTION_BUDGET`` — жёсткий максимум
  ``pool_size + max_overflow`` на процесс, проверяется валидатором конфигурации.
  Лучше ждать соединение 15 секунд, чем получить отказ сервера.
* **Один движок на процесс.** Восемь параллельных флоу, каждый со своим
  движком, — это восемь пулов. ``get_engine`` возвращает один общий на
  процесс для одинакового DSN.
* **Таймауты на всё.** ``timeout`` ограничивает установку соединения,
  ``command_timeout`` — один запрос, ``pool_timeout`` — ожидание свободного
  соединения. Без них запрос к недоступной базе висит бесконечно и тянет за
  собой весь флоу.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from geonexa_proxima.db.base import Base
from geonexa_proxima.tls import SSLMode, asyncpg_connect_args

SessionFactory = async_sessionmaker[AsyncSession]

_ENGINES: dict[str, AsyncEngine] = {}
_ENGINE_LOCK = threading.Lock()


def normalize_database_url(database_url: str) -> str:
    """Привести DSN к asyncpg и снять libpq-параметры, которых драйвер не знает."""

    if not database_url.startswith(("postgresql+asyncpg://", "postgresql://")):
        raise ValueError("database_url must point to PostgreSQL")
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if "sslmode=" in database_url:
        raise ValueError(
            "asyncpg не понимает sslmode внутри DSN: удали параметр из "
            "DATABASE_URL и задай режим через DATABASE_SSL_MODE"
        )
    return database_url


def create_engine(
    database_url: str,
    *,
    echo: bool = False,
    pool_size: int = 2,
    max_overflow: int = 0,
    pool_timeout: int = 15,
    pool_recycle: int = 1800,
    ssl_mode: SSLMode | str = SSLMode.PREFER,
    ssl_root_cert: str | Path | None = None,
    connect_timeout_seconds: float = 10,
    command_timeout_seconds: float = 30,
    statement_timeout_ms: int | None = None,
    application_name: str = "geonexa-proxima",
) -> AsyncEngine:
    """Создать движок с пулом, рассчитанным на управляемую БД."""

    return create_async_engine(
        normalize_database_url(database_url),
        echo=echo,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        # Управляемая БД закрывает простаивающие соединения сама; пересоздаём
        # раньше, чем она это сделает, иначе всплывёт «connection was closed».
        pool_recycle=pool_recycle,
        # Последним использованным пользуемся первым: лишние соединения тихо
        # простаивают и утилизируются, а не держат слоты на сервере.
        pool_use_lifo=True,
        connect_args=asyncpg_connect_args(
            ssl_mode,
            ssl_root_cert,
            connect_timeout_seconds=connect_timeout_seconds,
            command_timeout_seconds=command_timeout_seconds,
            statement_timeout_ms=statement_timeout_ms,
            application_name=application_name,
        ),
    )


def engine_from_settings(settings: Any, *, application_name: str | None = None) -> AsyncEngine:
    """Собрать движок по Settings, не дублируя список параметров по коду."""

    return create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        ssl_mode=settings.database_ssl_mode,
        ssl_root_cert=settings.database_ssl_root_cert,
        connect_timeout_seconds=settings.db_connect_timeout,
        command_timeout_seconds=settings.db_command_timeout,
        statement_timeout_ms=settings.database_statement_timeout_ms,
        application_name=application_name or settings.db_application_name,
    )


def get_engine(settings: Any, *, application_name: str | None = None) -> AsyncEngine:
    """Один движок на процесс для одинакового DSN.

    Флоу подписчиков запускаются пачкой по восемь штук; если каждый создаст свой
    движок, процесс откроет восемь пулов вместо одного и упрётся в лимит
    соединений на ровном месте.
    """

    key = f"{normalize_database_url(settings.database_url)}|{application_name or ''}"
    engine = _ENGINES.get(key)
    if engine is not None:
        return engine
    with _ENGINE_LOCK:
        engine = _ENGINES.get(key)
        if engine is None:
            engine = engine_from_settings(settings, application_name=application_name)
            _ENGINES[key] = engine
        return engine


async def dispose_engines() -> None:
    """Закрыть все пулы процесса. Вызывается при остановке сервиса."""

    with _ENGINE_LOCK:
        engines = list(_ENGINES.values())
        _ENGINES.clear()
    for engine in engines:
        await engine.dispose()


def pool_snapshot(engine: AsyncEngine) -> dict[str, int]:
    """Состояние пула без обращения к БД — для /health и админки."""

    pool = engine.sync_engine.pool
    return {
        "size": pool.size(),
        "checked_in": pool.checkedin(),
        "checked_out": pool.checkedout(),
        # До первого заполнения QueuePool считает свободные базовые slots
        # отрицательным overflow; наружу отдаём неотрицательное значение.
        "overflow": max(0, pool.overflow()),
    }


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    """Создать фабрику независимых сессий для repository/unit-of-work."""

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Открыть транзакцию и гарантировать commit/rollback/close."""

    async with factory() as session, session.begin():
        yield session


async def init_database(engine: AsyncEngine) -> None:
    """Создать схему для локальной разработки; production использует Alembic."""

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
