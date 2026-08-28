"""GeoNexa command-line entrypoints."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated

import typer

app = typer.Typer(help="GeoNexa Proxima application services.", no_args_is_help=True)


@app.command()
def api(
    host: Annotated[str, typer.Option(help="Bind host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8000,
    reload: Annotated[bool, typer.Option(help="Reload on source changes.")] = False,
    bootstrap: Annotated[
        str | None, typer.Option(help="Dependency factory as module:callable.")
    ] = None,
) -> None:
    """Serve health probes and the Telegram webhook."""

    import uvicorn

    if bootstrap:
        os.environ["GEONEXA_BOOTSTRAP"] = bootstrap
    uvicorn.run("geonexa_proxima.api.app:app", host=host, port=port, reload=reload)


@app.command()
def bot(
    bootstrap: Annotated[
        str | None, typer.Option(help="Dependency factory as module:callable.")
    ] = None,
) -> None:
    """Run the Telegram bot using long polling."""

    from geonexa_proxima.telegram.bot import run_polling

    asyncio.run(run_polling(bootstrap_target=bootstrap))


@app.command()
def collect(
    lookback_hours: Annotated[int | None, typer.Option(min=1)] = None,
    limit_per_source: Annotated[int | None, typer.Option(min=1)] = None,
    bootstrap: Annotated[
        str | None, typer.Option(help="Dependency factory as module:callable.")
    ] = None,
) -> None:
    """Run ingestion directly without Prefect orchestration."""

    async def execute() -> dict[str, object]:
        from geonexa_proxima.services.container import load_container

        container = load_container(target=bootstrap)
        try:
            stats = await container.ingestion_service().ingest(
                lookback_hours=lookback_hours or container.settings.collection_lookback_hours,
                limit_per_source=limit_per_source or container.settings.max_items_per_source,
            )
            return stats.as_dict()
        finally:
            await container.close()

    typer.echo(json.dumps(asyncio.run(execute()), ensure_ascii=False, indent=2))


@app.command()
def digests(
    deliver: Annotated[
        bool,
        typer.Option(help="Send via Telegram; disable for a persisted dry run."),
    ] = True,
    bootstrap: Annotated[
        str | None, typer.Option(help="Dependency factory as module:callable.")
    ] = None,
) -> None:
    """Build one personalized digest for every enabled profile."""

    from geonexa_proxima.workflows.digests import personal_digests_flow

    result = asyncio.run(personal_digests_flow(bootstrap_target=bootstrap, deliver=deliver))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command(name="flow")
def flow_command(
    schedule: Annotated[bool, typer.Option(help="Run repeatedly using a local interval.")] = False,
    interval_seconds: Annotated[float, typer.Option(min=1)] = 86_400,
    bootstrap: Annotated[
        str | None, typer.Option(help="Dependency factory as module:callable.")
    ] = None,
) -> None:
    """Run the Prefect flow once or on a local schedule."""

    from geonexa_proxima.workflows.ingestion import run_once, scheduled_entrypoint

    if schedule:
        scheduled_entrypoint(
            interval_seconds=interval_seconds,
            bootstrap_target=bootstrap,
        )
        return
    typer.echo(json.dumps(run_once(bootstrap_target=bootstrap), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
