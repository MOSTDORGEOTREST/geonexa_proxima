"""Prefect 3 flow wrappers that also run against its ephemeral local API."""

from __future__ import annotations

import asyncio
from typing import Any

from prefect import flow

from geonexa_proxima.services.container import load_container


@flow(name="geonexa-ingestion", log_prints=True)
async def ingestion_flow(*, bootstrap_target: str | None = None) -> dict[str, Any]:
    """Execute one independently bootstrapped collection run."""

    container = load_container(target=bootstrap_target)
    try:
        stats = await container.ingestion_service().ingest(
            lookback_hours=container.settings.collection_lookback_hours,
            limit_per_source=container.settings.max_items_per_source,
        )
        return stats.as_dict()
    finally:
        await container.close()


def run_once(*, bootstrap_target: str | None = None) -> dict[str, Any]:
    """Synchronous process entrypoint; no Prefect deployment is required."""

    return asyncio.run(ingestion_flow(bootstrap_target=bootstrap_target))


async def _schedule_loop(interval_seconds: float, bootstrap_target: str | None) -> None:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    while True:
        await ingestion_flow(bootstrap_target=bootstrap_target)
        await asyncio.sleep(interval_seconds)


def scheduled_entrypoint(
    *, interval_seconds: float = 86_400, bootstrap_target: str | None = None
) -> None:
    """Run the Prefect flow on a local interval without a Prefect server/deployment."""

    asyncio.run(_schedule_loop(interval_seconds, bootstrap_target))
