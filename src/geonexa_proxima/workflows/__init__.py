"""Prefect workflow entrypoints."""

from geonexa_proxima.workflows.digests import personal_digests_flow
from geonexa_proxima.workflows.ingestion import ingestion_flow, run_once, scheduled_entrypoint

__all__ = [
    "ingestion_flow",
    "personal_digests_flow",
    "run_once",
    "scheduled_entrypoint",
]
