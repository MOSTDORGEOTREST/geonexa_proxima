"""HTTP health probes and Telegram webhook transport."""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from geonexa_proxima.api.admin import build_admin_router
from geonexa_proxima.api.admin.security import LoginThrottle, PasswordChecker
from geonexa_proxima.config import Settings, get_settings
from geonexa_proxima.domain import NotFoundError
from geonexa_proxima.logging import configure_from_settings
from geonexa_proxima.services.container import Container, load_container
from geonexa_proxima.services.prefect_admin import PrefectUnavailable
from geonexa_proxima.telegram.bot import TelegramApplication, create_telegram_app

log = logging.getLogger(__name__)


async def _register_webhook(settings: Settings, telegram: TelegramApplication | None) -> str | None:
    """Зарегистрировать вебхук в Telegram при старте.

    Без этого webhook-режим требовал ручного вызова setWebhook, и признаком
    «забыли» была тишина в боте — самая дорогая из диагностик.
    Возвращает адрес, если регистрация удалась.
    """

    endpoint = settings.webhook_endpoint()
    if telegram is None or endpoint is None:
        return None
    secret = (
        settings.telegram_webhook_secret.get_secret_value()
        if settings.telegram_webhook_secret
        else None
    )
    try:
        await telegram.bot.set_webhook(
            endpoint,
            secret_token=secret,
            drop_pending_updates=False,
            allowed_updates=["message", "callback_query", "my_chat_member"],
        )
    except Exception as error:
        log.warning("Вебхук %s не зарегистрирован: %s", endpoint, error)
        return None
    log.info("Вебхук зарегистрирован: %s", endpoint)
    return endpoint


def create_app(
    *,
    settings: Settings | None = None,
    container: Container | None = None,
    bootstrap_target: str | None = None,
) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())

    configure_from_settings(settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        active_container = container
        application.state.startup_error = None
        application.state.bootstrap = None
        # Контейнер поднимается против какой угодно базы, в том числе чистой.
        # Схема и обязательные записи создаются здесь, до первого запроса.
        if settings.db_auto_migrate or settings.db_auto_seed:
            try:
                from geonexa_proxima.bootstrap import start_service

                application.state.bootstrap = await start_service(settings, service="api")
            except Exception as exc:
                # Без этой строки единственным следом падения бутстрапа был
                # ответ /ready, который никто не смотрит, пока не спросят.
                log.exception("Бутстрап не отработал: %s", exc)
                application.state.startup_error = f"bootstrap: {exc}"
        if active_container is None:
            try:
                active_container = load_container(
                    settings, target=bootstrap_target, require_ready=False
                )
            except Exception as exc:
                log.exception("Контейнер зависимостей не собрался: %s", exc)
                application.state.startup_error = str(exc)
        application.state.container = active_container
        application.state.telegram = None
        if active_container is not None:
            try:
                application.state.telegram = create_telegram_app(active_container)
            except Exception as exc:
                log.exception("Приложение Telegram не собралось: %s", exc)
                application.state.startup_error = str(exc)
        application.state.webhook = await _register_webhook(settings, application.state.telegram)
        try:
            yield
        finally:
            telegram: TelegramApplication | None = application.state.telegram
            if telegram is not None:
                await telegram.bot.session.close()
            if active_container is not None:
                await active_container.close()
            # Пул один на процесс — гасим его здесь, а не в контейнере.
            from geonexa_proxima.db.session import dispose_engines

            await dispose_engines()

    application = FastAPI(title=settings.app_name, lifespan=lifespan)
    application.state.settings = settings
    application.state.password_checker = PasswordChecker(settings)
    application.state.login_throttle = LoginThrottle(
        limit=settings.admin_login_rate_limit_per_minute
    )
    application.state.prefect_admin = None
    application.state.harvest_matcher = None
    application.include_router(build_admin_router())

    # Прикладной слой сообщает «не нашлось» и «оркестратор недоступен»
    # исключениями. Без этих обработчиков любое такое исключение доезжало до
    # пользователя как голый «Internal Server Error»: админка показывала
    # пятисотку вместо «Deployment не зарегистрирован, запусти prefect-worker»,
    # и понять причину по интерфейсу было нельзя.
    @application.exception_handler(PrefectUnavailable)
    async def _prefect_unavailable(_request: Request, error: PrefectUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(error)})

    @application.exception_handler(NotFoundError)
    async def _not_found(_request: Request, error: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    if settings.admin_cors_origins:
        # Нужен только для vite dev: в production наружу торчит node-слой,
        # а FastAPI живёт во внутренней сети.
        from fastapi.middleware.cors import CORSMiddleware

        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.admin_cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @application.get("/health", tags=["system"])
    async def health(request: Request) -> JSONResponse:
        """Живость процесса и того, что он вообще смог собраться.

        Раньше ручка всегда отвечала «ok», а на ней висит healthcheck compose:
        сервис с упавшим бутстрапом считался здоровым, и по его готовности
        стартовали бот, воркер и админка. Диагноз при этом выглядел как
        «всё зелёное, ничего не работает».
        """

        error = getattr(request.app.state, "startup_error", None)
        if error:
            return JSONResponse({"status": "degraded"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @application.get("/ready", tags=["system"])
    async def readiness(request: Request) -> JSONResponse:
        active: Container | None = getattr(request.app.state, "container", None)
        components = active.readiness() if active else {}
        error = getattr(request.app.state, "startup_error", None)
        ready = bool(active and active.ready and not error)
        payload: dict[str, object] = {"status": "ready" if ready else "not_ready"}
        payload["components"] = components
        boot = getattr(request.app.state, "bootstrap", None)
        if boot:
            payload["schema"] = boot.get("schema_after")
            payload["migrated"] = boot.get("migrated")
        # Пул отдаём всегда: «висит запрос» и «кончились соединения» выглядят
        # одинаково снаружи, а различаются именно здесь.
        #
        # Движок берём не только из контейнера: контейнер не собирается, если
        # недоступна любая зависимость — например, не скачаны веса модели, — а
        # состояние базы в этот момент нужно видеть как раз сильнее всего.
        engine = getattr(active, "engine", None) if active is not None else None
        if engine is None:
            try:
                from geonexa_proxima.db.session import get_engine

                engine = get_engine(settings)
            except Exception:
                engine = None
        if engine is not None:
            from geonexa_proxima.db.session import pool_snapshot

            payload["pool"] = pool_snapshot(engine)
        if error:
            # Наружу — только факт. Текст исключения содержит имена хостов и
            # сообщения драйвера базы, а ручка живёт на том же публичном
            # адресе, что и вебхук. Подробности остаются в логе контейнера.
            payload["error"] = "startup_failed"
            log.warning("Проверка готовности: %s", error)
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
            raise HTTPException(status_code=403, detail="Неверный секрет вебхука")
        telegram: TelegramApplication | None = getattr(request.app.state, "telegram", None)
        if telegram is None:
            raise HTTPException(status_code=503, detail="Бот Telegram не сконфигурирован")
        update = Update.model_validate(await request.json(), context={"bot": telegram.bot})
        await telegram.dispatcher.feed_update(telegram.bot, update)
        return {"ok": True}

    return application
