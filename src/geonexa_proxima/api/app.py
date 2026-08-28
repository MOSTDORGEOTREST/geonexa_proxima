"""HTTP health probes and Telegram webhook transport."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from geonexa_proxima.config import Settings, get_settings
from geonexa_proxima.services.container import Container, load_container
from geonexa_proxima.telegram.bot import TelegramApplication, create_telegram_app


def create_app(
    *,
    settings: Settings | None = None,
    container: Container | None = None,
    bootstrap_target: str | None = None,
) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        active_container = container
        application.state.startup_error = None
        if active_container is None:
            try:
                active_container = load_container(
                    settings, target=bootstrap_target, require_ready=False
                )
            except Exception as exc:
                application.state.startup_error = str(exc)
        application.state.container = active_container
        application.state.telegram = None
        if active_container is not None:
            try:
                application.state.telegram = create_telegram_app(active_container)
            except Exception as exc:
                application.state.startup_error = str(exc)
        try:
            yield
        finally:
            telegram: TelegramApplication | None = application.state.telegram
            if telegram is not None:
                await telegram.bot.session.close()
            if active_container is not None:
                await active_container.close()

    application = FastAPI(title=settings.app_name, lifespan=lifespan)

    @application.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready", tags=["system"])
    async def readiness(request: Request) -> JSONResponse:
        active: Container | None = getattr(request.app.state, "container", None)
        components = active.readiness() if active else {}
        error = getattr(request.app.state, "startup_error", None)
        ready = bool(active and active.ready and not error)
        payload: dict[str, object] = {"status": "ready" if ready else "not_ready"}
        payload["components"] = components
        if error:
            payload["error"] = error
        return JSONResponse(payload, status_code=200 if ready else 503)

    @application.post("/telegram/webhook", tags=["telegram"])
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, bool]:
        expected = (
            settings.telegram_webhook_secret.get_secret_value()
            if settings.telegram_webhook_secret
            else None
        )
        if expected and (
            x_telegram_bot_api_secret_token is None
            or not hmac.compare_digest(expected, x_telegram_bot_api_secret_token)
        ):
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
        telegram: TelegramApplication | None = getattr(request.app.state, "telegram", None)
        if telegram is None:
            raise HTTPException(status_code=503, detail="Telegram bot is not configured")
        update = Update.model_validate(await request.json(), context={"bot": telegram.bot})
        await telegram.dispatcher.feed_update(telegram.bot, update)
        return {"ok": True}

    return application


app = create_app()
