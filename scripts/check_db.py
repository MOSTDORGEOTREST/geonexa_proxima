#!/usr/bin/env python3
"""Проверка подключения к PostgreSQL перед первой миграцией.

Отвечает на вопросы, которые дороже выяснять постфактум: доехали ли мы вообще,
шифруется ли канал, чем занята база, хватит ли прав завести btree_gist для
ограничения непересекающихся подписок.

    poetry run python scripts/check_db.py
    poetry run python scripts/check_db.py --create-extension
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from geonexa_proxima.config import get_settings
from geonexa_proxima.db.session import normalize_database_url
from geonexa_proxima.tls import asyncpg_connect_args

OK = "  OK "
NO = "  -- "
BAD = " FAIL"


async def run(create_extension: bool) -> int:
    settings = get_settings()
    url = normalize_database_url(settings.database_url)
    host = url.rsplit("@", 1)[-1]
    print(f"Цель      : {host}")
    print(f"Режим TLS : {settings.database_ssl_mode}")
    print(f"Сертификат: {settings.database_ssl_root_cert or '—'}\n")

    engine = create_async_engine(
        url,
        connect_args=asyncpg_connect_args(
            settings.database_ssl_mode,
            settings.database_ssl_root_cert,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
            application_name="geonexa-check-db",
        ),
    )
    failures = 0
    try:
        async with engine.connect() as connection:
            version = await connection.scalar(text("show server_version"))
            print(f"{OK} подключение          PostgreSQL {version}")

            encrypted = await connection.scalar(
                text(
                    "select ssl from pg_stat_ssl s join pg_stat_activity a using (pid) "
                    "where a.pid = pg_backend_pid()"
                )
            )
            cipher = await connection.scalar(
                text(
                    "select version from pg_stat_ssl s join pg_stat_activity a using (pid) "
                    "where a.pid = pg_backend_pid()"
                )
            )
            if encrypted:
                print(f"{OK} шифрование           {cipher}")
            else:
                failures += 1
                print(f"{BAD} шифрование           соединение открытым текстом")

            user, database = await _one(connection, "select current_user, current_database()")
            print(f"{OK} identity             {user} @ {database}")

            tables = await connection.scalar(
                text("select count(*) from information_schema.tables where table_schema = 'public'")
            )
            alembic = await connection.scalar(
                text("select to_regclass('public.alembic_version') is not null")
            )
            state = "пустая" if not tables else f"{tables} таблиц"
            revision = ""
            if alembic:
                revision = await connection.scalar(text("select version_num from alembic_version"))
                revision = f", alembic={revision}"
            print(f"{OK} состояние схемы      {state}{revision}")

            available = await connection.scalar(
                text("select count(*) from pg_available_extensions where name = 'btree_gist'")
            )
            installed = await connection.scalar(
                text("select count(*) from pg_extension where extname = 'btree_gist'")
            )
            if installed:
                print(f"{OK} btree_gist           уже установлено")
            elif available:
                print(f"{NO} btree_gist           доступно, но не установлено")
            else:
                failures += 1
                print(f"{BAD} btree_gist           недоступно — ограничение подписок не заработает")

            can_create = await connection.scalar(
                text(
                    "select pg_catalog.has_database_privilege("
                    "current_user, current_database(), 'CREATE')"
                )
            )
            verdict = "разрешено" if can_create else "запрещено"
            print(f"{OK if can_create else BAD} привилегия CREATE    {verdict}")
            failures += 0 if can_create else 1

            createdb = await connection.scalar(
                text("select rolcreatedb from pg_roles where rolname = current_user")
            )
            verdict = (
                "разрешено, Prefect можно выделить отдельную базу"
                if createdb
                else "запрещено, база Prefect остаётся в контейнере"
            )
            print(f"{OK if createdb else NO} привилегия CREATEDB  {verdict}")

        if create_extension and not installed:
            async with engine.begin() as connection:
                await connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
            print(f"\n{OK} btree_gist           установлено")
    except Exception as error:  # диагностический скрипт: показываем любую поломку
        print(f"{BAD} {type(error).__name__}: {error}")
        return 1
    finally:
        await engine.dispose()

    if failures:
        print(f"\nНеполадок: {failures}. Миграции запускать рано.")
        return 1
    print("\nМожно запускать alembic upgrade head.")
    return 0


async def _one(connection, sql: str):
    result = await connection.execute(text(sql))
    return result.one()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--create-extension",
        action="store_true",
        help="Установить btree_gist при отсутствии.",
    )
    return asyncio.run(run(parser.parse_args().create_extension))


if __name__ == "__main__":
    raise SystemExit(main())
