"""Подъём платформы на чистой базе: схема и обязательные записи."""

from geonexa_proxima.bootstrap.entrypoint import start_service, wait_for_database
from geonexa_proxima.bootstrap.schema import (
    SchemaState,
    ensure_schema,
    inspect_schema,
    upgrade_to_head,
)
from geonexa_proxima.bootstrap.seed import bootstrap, seed_all

__all__ = [
    "SchemaState",
    "bootstrap",
    "ensure_schema",
    "inspect_schema",
    "seed_all",
    "start_service",
    "upgrade_to_head",
    "wait_for_database",
]
