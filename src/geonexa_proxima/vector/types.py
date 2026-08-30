"""Тип колонки pgvector для SQLAlchemy.

Диалект SQLAlchemy ничего не знает о pgvector, поэтому тип объявляется здесь —
один раз и для миграций, и для ORM-моделей, чтобы объявления не разъезжались.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType):
    """``vector(N)`` или ``halfvec(N)`` в зависимости от настроек."""

    cache_ok = True

    def __init__(self, dimensions: int, column_type: str = "vector") -> None:
        if dimensions < 1:
            raise ValueError("Размерность вектора должна быть положительной")
        self.dimensions = dimensions
        self.column_type = column_type

    def get_col_spec(self, **_: object) -> str:
        return f"{self.column_type}({self.dimensions})"

    def bind_processor(self, dialect: object):
        def process(value: Sequence[float] | str | None) -> str | None:
            if value is None:
                return None
            if isinstance(value, str):
                return value
            return "[" + ",".join(repr(float(component)) for component in value) + "]"

        return process

    def result_processor(self, dialect: object, coltype: object):
        def process(value: str | None) -> list[float] | None:
            if value is None:
                return None
            body = str(value).strip()
            if not body.startswith("["):
                return None
            return [float(part) for part in body.strip("[]").split(",") if part]

        return process
