"""Фабрика FastAPI-приложения.

Модуль называется `application`, а не `app`: иначе атрибут пакета `app` — это
подмодуль, а не ASGI-приложение, и `geonexa_proxima.api:app` отдаёт серверу
модуль. Ошибка тихая и находится не сразу.

Само приложение собирается лениво. Собирать его на импорте нельзя: тогда любой
инструмент, который просто импортирует пакет, обязан иметь валидное окружение,
а ошибка конфигурации превращается в трейсбек импорта вместо внятного отказа.
"""

from geonexa_proxima.api.application import create_app

__all__ = ["app", "create_app"]


def __getattr__(name: str) -> object:
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
