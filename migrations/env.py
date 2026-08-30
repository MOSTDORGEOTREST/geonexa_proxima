"""Async Alembic environment for GeoNexa Proxima."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from geonexa_proxima.config import get_settings
from geonexa_proxima.db import models  # noqa: F401
from geonexa_proxima.db.base import Base
from geonexa_proxima.db.session import normalize_database_url
from geonexa_proxima.tls import asyncpg_connect_args

settings = get_settings()
config = context.config

# Индексы pgvector объявляются сырым SQL: SQLAlchemy не умеет описывать
# operator class (hnsw ... vector_cosine_ops), поэтому autogenerate каждый раз
# предлагал бы их удалить. Исключаем их из сравнения, чтобы `alembic check`
# оставался полезным сигналом, а не постоянно красным.
VECTOR_INDEX_SUFFIXES = ("_embedding_hnsw", "_embedding_ivfflat")


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    if type_ == "index" and name and name.endswith(VECTOR_INDEX_SUFFIXES):
        return False
    return True


if config.config_file_name:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", normalize_database_url(settings.database_url))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without opening a database connection."""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_sync_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=asyncpg_connect_args(
            settings.database_ssl_mode,
            settings.database_ssl_root_cert,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
            application_name="geonexa-alembic",
        ),
    )
    async with connectable.connect() as connection:
        await connection.run_sync(run_sync_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
