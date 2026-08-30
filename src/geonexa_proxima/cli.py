"""Команды Проксимы."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated

import typer

app = typer.Typer(help="Сервисы Проксимы.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Настроить логирование до выполнения любой команды.

    Без этого LOG_LEVEL из .env не действовал ни на одну команду: модуль
    логирования существовал, но его никто не вызывал.
    """

    try:
        from geonexa_proxima.config import get_settings
        from geonexa_proxima.logging import configure_from_settings

        configure_from_settings(get_settings())
    except Exception:
        pass


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
    uvicorn.run("geonexa_proxima.api:app", host=host, port=port, reload=reload)


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
        typer.Option(help="Ставить задания в очередь; выключи для сухого прогона."),
    ] = True,
    kinds: Annotated[
        str | None,
        typer.Option(help="Виды подписчиков через запятую: user, group, channel."),
    ] = None,
    bootstrap: Annotated[
        str | None, typer.Option(help="Dependency factory as module:callable.")
    ] = None,
) -> None:
    """Построить дайджест для каждого профиля, которому пора."""

    from geonexa_proxima.workflows.dispatch import digest_dispatch_flow

    selected = [part.strip() for part in kinds.split(",") if part.strip()] if kinds else None
    result = asyncio.run(
        digest_dispatch_flow(bootstrap_target=bootstrap, kinds=selected, deliver=deliver)
    )
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

    from geonexa_proxima.workflows.harvest import global_harvest_flow

    if schedule:
        raise typer.BadParameter(
            "Локальный цикл убран: расписаниями управляет Prefect. "
            "Заведи расписание в админке или запусти deployment вручную."
        )
    result = asyncio.run(global_harvest_flow(bootstrap_target=bootstrap, trigger="manual"))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


db_app = typer.Typer(help="Схема базы данных.", no_args_is_help=True)
prefect_app = typer.Typer(help="Оркестрация Prefect.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(prefect_app, name="prefect")


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Привести схему к текущей ревизии. На пустой базе создаёт всё с нуля."""

    async def execute() -> dict[str, object]:
        from geonexa_proxima.bootstrap import bootstrap
        from geonexa_proxima.config import get_settings
        from geonexa_proxima.db.session import dispose_engines, get_engine

        settings = get_settings()
        engine = get_engine(settings, application_name="geonexa-migrate")
        try:
            return await bootstrap(engine, settings)
        finally:
            await dispose_engines()

    typer.echo(json.dumps(asyncio.run(execute()), ensure_ascii=False, indent=2))


@db_app.command("status")
def db_status() -> None:
    """Что сейчас в базе: пустая, отстаёт или актуальна."""

    async def execute() -> dict[str, object]:
        from geonexa_proxima.bootstrap import inspect_schema
        from geonexa_proxima.config import get_settings
        from geonexa_proxima.db.session import dispose_engines, get_engine, pool_snapshot

        settings = get_settings()
        engine = get_engine(settings, application_name="geonexa-status")
        try:
            state = await inspect_schema(engine)
            return {
                "summary": state.summary,
                "current_revision": state.current_revision,
                "head_revision": state.head_revision,
                "tables": state.tables,
                "pool": pool_snapshot(engine),
            }
        finally:
            await dispose_engines()

    typer.echo(json.dumps(asyncio.run(execute()), ensure_ascii=False, indent=2))


@db_app.command("seed")
def db_seed() -> None:
    """Досеять обязательные записи. Идемпотентно."""

    async def execute() -> dict[str, object]:
        from geonexa_proxima.bootstrap import seed_all
        from geonexa_proxima.config import get_settings
        from geonexa_proxima.db.session import dispose_engines, get_engine

        settings = get_settings()
        engine = get_engine(settings, application_name="geonexa-seed")
        try:
            report = await seed_all(engine, settings)
            return report.as_dict()
        finally:
            await dispose_engines()

    typer.echo(json.dumps(asyncio.run(execute()), ensure_ascii=False, indent=2))


@prefect_app.command("deploy")
def prefect_deploy() -> None:
    """Зарегистрировать флоу с расписаниями из базы."""

    async def execute() -> dict[str, object]:
        from geonexa_proxima.config import get_settings
        from geonexa_proxima.db.session import dispose_engines, get_engine
        from geonexa_proxima.workflows.deployments import deploy_all

        settings = get_settings()
        engine = get_engine(settings, application_name="geonexa-deploy")
        try:
            return await deploy_all(engine, settings)
        finally:
            await dispose_engines()

    typer.echo(json.dumps(asyncio.run(execute()), ensure_ascii=False, indent=2))


@prefect_app.command("run")
def prefect_run(
    key: Annotated[str, typer.Argument(help="Ключ флоу, например global-harvest.")],
    parameter: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Параметр вида имя=значение."),
    ] = None,
) -> None:
    """Запустить флоу вручную — тем же путём, что и кнопка в админке."""

    async def execute() -> dict[str, object]:
        from geonexa_proxima.config import get_settings
        from geonexa_proxima.db.session import dispose_engines, get_engine
        from geonexa_proxima.services.prefect_admin import PrefectAdmin

        params: dict[str, object] = {}
        for raw in parameter or []:
            name, _, value = raw.partition("=")
            params[name] = json.loads(value) if value[:1] in '[{"0123456789' else value
        settings = get_settings()
        engine = get_engine(settings, application_name="geonexa-cli")
        admin = PrefectAdmin(settings, engine)
        try:
            return await admin.run_now(key, parameters=params, actor="cli")
        finally:
            await admin.aclose()
            await dispose_engines()

    typer.echo(json.dumps(asyncio.run(execute()), ensure_ascii=False, indent=2))


@prefect_app.command("schedule")
def prefect_schedule(
    key: Annotated[str, typer.Argument(help="Ключ расписания.")],
    cron: Annotated[str | None, typer.Option(help="Cron-выражение.")] = None,
    interval: Annotated[int | None, typer.Option(help="Интервал в секундах.")] = None,
    disable: Annotated[bool, typer.Option(help="Выключить расписание.")] = False,
) -> None:
    """Изменить расписание: сначала в базе, затем в Prefect."""

    async def execute() -> dict[str, object]:
        from geonexa_proxima.config import get_settings
        from geonexa_proxima.db.session import dispose_engines, get_engine
        from geonexa_proxima.services.prefect_admin import PrefectAdmin

        settings = get_settings()
        engine = get_engine(settings, application_name="geonexa-cli")
        admin = PrefectAdmin(settings, engine)
        try:
            return await admin.set_schedule(
                key, cron=cron, interval_seconds=interval, enabled=not disable, actor="cli"
            )
        finally:
            await admin.aclose()
            await dispose_engines()

    typer.echo(json.dumps(asyncio.run(execute()), ensure_ascii=False, indent=2))


@prefect_app.command("cron")
def prefect_cron(
    expression: Annotated[str, typer.Argument(help="Cron-выражение для проверки.")],
) -> None:
    """Показать, когда сработает выражение. Проверка до сохранения."""

    from geonexa_proxima.config import get_settings
    from geonexa_proxima.services.prefect_admin import describe_cron

    result = describe_cron(expression, timezone=get_settings().timezone)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
