"""Aiogram bot construction and polling entrypoints."""

from geonexa_proxima.telegram.bot import TelegramApplication, create_telegram_app, run_polling

__all__ = ["TelegramApplication", "create_telegram_app", "run_polling"]
