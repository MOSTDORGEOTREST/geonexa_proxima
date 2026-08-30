"""Изоляция тестов от окружения разработчика.

Раньше `pytest` на чистом клоне падал на этапе сбора: `db/models.py` дёргал
`get_settings()` при импорте, тот читал `.env` разработчика, и один неверный
путь к сертификату ронял весь persistence-слой. Теперь конфигурация из ORM
убрана, а тесты дополнительно фиксируют окружение — чтобы результат прогона не
зависел от того, что лежит в `.env` на конкретной машине.

Интеграционные тесты (`tests/integration`) намеренно исключены: они ходят в
настоящую базу и должны читать настоящий `.env`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Минимум, без которого `Settings` не собирается. Значения заведомо нерабочие:
#: если тест куда-то дозвонится с ними, это само по себе баг.
BASELINE = {
    "ENVIRONMENT": "test",
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/geonexa_test",
    "DATABASE_SSL_MODE": "disable",
    "DATABASE_SSL_ROOT_CERT": "",
    "TELEGRAM_BOT_TOKEN": "000000000:dummy-test-token",
    "ADMIN_USERNAME": "test-admin",
    "ADMIN_PASSWORD": "test-password",
    "ADMIN_PASSWORD_HASH": "",
    "ADMIN_JWT_SECRET": "test-jwt-secret",
    "EMBEDDING_DIMENSIONS": "1024",
    "VECTOR_COLUMN_TYPE": "vector",
    "HARVEST_CONFIG_PATH": str(ROOT / "config" / "harvest.yaml"),
    "TAXONOMY_PATH": str(ROOT / "config" / "taxonomy.yaml"),
}


def pytest_configure(config: pytest.Config) -> None:
    """Зафиксировать окружение до импорта тестовых модулей."""

    if _is_integration_run(config):
        return
    # Пустой путь уводит pydantic-settings от .env разработчика: файла с таким
    # именем нет, и настройки собираются только из переменных ниже.
    os.environ.setdefault("GEONEXA_ENV_FILE", str(ROOT / ".env.absent"))
    for key, value in BASELINE.items():
        os.environ[key] = value


def _is_integration_run(config: pytest.Config) -> bool:
    if os.getenv("GEONEXA_RUN_INTEGRATION") == "1":
        return True
    return any("integration" in str(argument) for argument in config.args)
