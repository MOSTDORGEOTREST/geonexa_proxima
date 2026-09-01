"""Структурированное логирование без утечки секретов.

Модуль долго существовал, но его никто не вызывал: `LOG_LEVEL` был
объявлен и не действовал, а сервисы писали в лог настройками по умолчанию.
Теперь `configure_from_settings` дёргается из каждой точки входа.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: structlog.types.Processor
    if json_logs:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def configure_from_settings(settings: object) -> None:
    """Настроить логирование по конфигурации приложения.

    JSON включается явно (`LOG_JSON`), иначе — по окружению: в production
    логи читает сборщик, в разработке — человек.
    """

    level = str(getattr(settings, "log_level", "INFO"))
    json_logs = getattr(settings, "log_json", None)
    if json_logs is None:
        environment = getattr(settings, "environment", None)
        json_logs = str(getattr(environment, "value", environment)) == "production"
    configure_logging(level, bool(json_logs))
