"""Выбор векторного хранилища по настройкам.

Порт ``VectorStore`` остаётся единственным, что видит остальной код: переезд
между pgvector и Qdrant — это смена адаптера, а не переписывание сервисов.
"""

from __future__ import annotations

from typing import Any

from geonexa_proxima.config import Settings, VectorBackend
from geonexa_proxima.vector.pgvector import (
    PgProfileVectorStore,
    PgVectorDialect,
    PgVectorStore,
)


def build_dialect(settings: Settings) -> PgVectorDialect:
    return PgVectorDialect(
        settings.embedding_dimensions,
        settings.vector_column_type,
        settings.vector_index_kind,
        hnsw_m=settings.vector_hnsw_m,
        hnsw_ef_construction=settings.vector_hnsw_ef_construction,
        ivfflat_lists=settings.vector_ivfflat_lists,
    )


def create_vector_store(settings: Settings, engine: Any = None) -> Any:
    if settings.vector_backend is VectorBackend.PGVECTOR:
        if engine is None:
            from geonexa_proxima.db.session import get_engine

            engine = get_engine(settings)
        return PgVectorStore(
            engine,
            build_dialect(settings),
            ef_search=settings.vector_hnsw_ef_search,
            model_id=settings.embedding_model,
        )
    from geonexa_proxima.vector.qdrant import create_vector_store as create_qdrant

    return create_qdrant(settings)


def create_profile_vector_store(settings: Settings, engine: Any = None) -> Any:
    if settings.vector_backend is VectorBackend.PGVECTOR:
        if engine is None:
            from geonexa_proxima.db.session import get_engine

            engine = get_engine(settings)
        return PgProfileVectorStore(engine, build_dialect(settings))
    from geonexa_proxima.vector.profiles import (
        create_profile_vector_store as create_qdrant_profiles,
    )

    return create_qdrant_profiles(settings)
