"""Authorization middleware for private bot access."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject


class AllowedUserMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_ids: set[int]) -> None:
        self.allowed_user_ids = allowed_user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is not None and user.id in self.allowed_user_ids:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Access denied.", show_alert=True)
        return None
