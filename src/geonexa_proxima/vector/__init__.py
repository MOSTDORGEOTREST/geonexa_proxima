"""Адаптеры векторного хранилища."""

from geonexa_proxima.vector.factory import (
    build_dialect,
    create_profile_vector_store,
    create_vector_store,
)
from geonexa_proxima.vector.pgvector import (
    PgProfileVectorStore,
    PgVectorDialect,
    PgVectorStore,
)
from geonexa_proxima.vector.types import Vector

__all__ = [
    "PgProfileVectorStore",
    "PgVectorDialect",
    "PgVectorStore",
    "Vector",
    "build_dialect",
    "create_profile_vector_store",
    "create_vector_store",
]
