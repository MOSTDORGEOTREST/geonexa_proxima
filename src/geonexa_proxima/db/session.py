"""Создание async engine и короткоживущих SQLAlchemy-сессий."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from geonexa_proxima.db.base import Base

SessionFactory = async_sessionmaker[AsyncSession]


def create_engine(
    database_url: str,
    *,
    echo: bool = False,
    pool_size: int = 10,
    max_overflow: int = 20,
) -> AsyncEngine:
    """Create an async PostgreSQL engine with pre-ping enabled."""

    if not database_url.startswith(("postgresql+asyncpg://", "postgresql://")):
        raise ValueError("database_url must point to PostgreSQL")
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )


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
