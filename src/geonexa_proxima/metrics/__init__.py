"""Агрегация метрик: SQL отдельно от оркестрации, чтобы тестировать без Prefect."""

from geonexa_proxima.metrics.purge import RULES, RetentionRule, purge
from geonexa_proxima.metrics.rollups import (
    DELIVERY_ROLLUP,
    ENGAGEMENT_ROLLUP,
    HARVEST_ROLLUP,
    RETENTION_ROLLUP,
    SCOPES,
    SUBSCRIBERS_ROLLUP,
)

__all__ = [
    "DELIVERY_ROLLUP",
    "ENGAGEMENT_ROLLUP",
    "HARVEST_ROLLUP",
    "RETENTION_ROLLUP",
    "RULES",
    "SCOPES",
    "SUBSCRIBERS_ROLLUP",
    "RetentionRule",
    "purge",
]
