"""Admin API: роутеры под /api/admin.

Наружу этот префикс не торчит. Ходит только node-слой SvelteKit по внутреннему
адресу, а браузер работает с ним через серверные `load` и `actions`. Поэтому
CORS нужен лишь для локальной разработки, когда vite и API живут на разных
портах.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from geonexa_proxima.api.admin.routers import (
    auth,
    chats,
    dashboard,
    deliveries,
    harvest,
    items,
    llm,
    schedules,
    settings,
    subscribers,
    subscriptions,
    system,
)
from geonexa_proxima.api.admin.security import current_admin

#: Всё, кроме входа, требует токена. Зависимость навешивается на роутер
#: целиком: забыть её на отдельном эндпоинте — самый лёгкий способ открыть
#: наружу то, что открывать не собирались.
PROTECTED = (
    dashboard.router,
    subscribers.router,
    chats.router,
    subscriptions.router,
    harvest.router,
    items.router,
    schedules.router,
    llm.router,
    settings.router,
    deliveries.router,
    system.router,
)


def build_admin_router() -> APIRouter:
    router = APIRouter(prefix="/api/admin")
    router.include_router(auth.router)
    for protected in PROTECTED:
        router.include_router(protected, dependencies=[Depends(current_admin)])
    return router


__all__ = ["build_admin_router"]
