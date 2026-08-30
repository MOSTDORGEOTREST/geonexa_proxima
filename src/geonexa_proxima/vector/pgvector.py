"""Векторное хранилище на pgvector внутри основной БД.

Векторы лежат рядом с корпусом, поэтому запись материала и его вектора
происходит в одной транзакции: рассинхронизации между двумя хранилищами
попросту не существует.

Три вещи, за которыми здесь следят:

* **Нормализация.** Все векторы приходят с единичной нормой (обрезка
  Matryoshka сопровождается ренормализацией), поэтому косинусная дистанция
  ``<=>`` и внутреннее произведение эквивалентны. Косинус выбран явно, чтобы
  ненормализованный вектор из чужого источника не испортил выдачу молча.
* **Потолки индексов.** HNSW и IVFFlat на ``vector`` держат 2000 измерений,
  HNSW на ``halfvec`` — 4000. Тип ``halfvec`` появился в pgvector 0.7.
* **Смена размерности.** Тип колонки фиксирует число измерений, поэтому смена
  модели требует новой колонки и переиндексации, а не тихой записи рядом.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from geonexa_proxima.config import VectorColumnType, VectorIndexKind
from geonexa_proxima.domain import SearchHit

ITEM_TABLE = "item_vectors"
PROFILE_TABLE = "profile_vectors"


def to_pgvector(vector: Sequence[float]) -> str:
    """Литерал pgvector: '[0.1,0.2,...]'."""

    return "[" + ",".join(repr(float(component)) for component in vector) + "]"


class PgVectorDialect:
    """Что именно писать в SQL при выбранных типе колонки и виде индекса."""

    def __init__(
        self,
        dimensions: int,
        column_type: VectorColumnType = VectorColumnType.VECTOR,
        index_kind: VectorIndexKind = VectorIndexKind.HNSW,
        *,
        hnsw_m: int = 16,
        hnsw_ef_construction: int = 64,
        ivfflat_lists: int = 100,
    ) -> None:
        self.dimensions = dimensions
        self.column_type = column_type
        self.index_kind = index_kind
        self.hnsw_m = hnsw_m
        self.hnsw_ef_construction = hnsw_ef_construction
        self.ivfflat_lists = ivfflat_lists

    @property
    def column_sql(self) -> str:
        return f"{self.column_type.value}({self.dimensions})"

    @property
    def ops_class(self) -> str:
        return (
            "vector_cosine_ops"
            if self.column_type is VectorColumnType.VECTOR
            else "halfvec_cosine_ops"
        )

    def index_sql(self, table: str, column: str = "embedding") -> str | None:
        if self.index_kind is VectorIndexKind.NONE:
            return None
        name = f"ix_{table}_{column}_{self.index_kind.value}"
        if self.index_kind is VectorIndexKind.HNSW:
            options = f"m = {self.hnsw_m}, ef_construction = {self.hnsw_ef_construction}"
        else:
            options = f"lists = {self.ivfflat_lists}"
        return (
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} "
            f"USING {self.index_kind.value} ({column} {self.ops_class}) WITH ({options})"
        )


class PgVectorStore:
    """Реализация порта VectorStore поверх pgvector."""

    def __init__(
        self,
        engine: AsyncEngine,
        dialect: PgVectorDialect,
        *,
        ef_search: int = 80,
        model_id: str = "",
    ) -> None:
        self.engine = engine
        self.dialect = dialect
        self.ef_search = ef_search
        self.model_id = model_id

    async def ensure_collection(self, dimensions: int) -> None:
        """Проверить, что схема соответствует текущей размерности."""

        if dimensions != self.dialect.dimensions:
            raise ValueError(
                f"Хранилище создано под {self.dialect.dimensions} измерений, "
                f"а эмбеддер отдаёт {dimensions}. Смена размерности требует "
                f"миграции колонки и переиндексации, а не записи рядом."
            )
        async with self.engine.connect() as connection:
            actual = await connection.scalar(
                text(
                    "SELECT atttypmod FROM pg_attribute "
                    "WHERE attrelid = to_regclass(:table) AND attname = 'embedding'"
                ),
                {"table": ITEM_TABLE},
            )
        if actual is None:
            raise RuntimeError(
                f"Таблица {ITEM_TABLE} отсутствует: примени миграции перед первым сбором"
            )
        if int(actual) != dimensions:
            raise RuntimeError(
                f"Колонка {ITEM_TABLE}.embedding объявлена на {actual} измерений, "
                f"а настройки требуют {dimensions}"
            )

    async def upsert(
        self,
        item_ids: Sequence[UUID],
        vectors: Sequence[Sequence[float]],
        payloads: Sequence[dict[str, object]],
    ) -> None:
        if not item_ids:
            return
        if not len(item_ids) == len(vectors) == len(payloads):
            raise ValueError("item_ids, vectors и payloads должны совпадать по длине")
        rows = [
            {
                "item_id": str(item_id),
                "embedding": to_pgvector(vector),
                "model": self.model_id,
                "dimensions": self.dialect.dimensions,
            }
            for item_id, vector in zip(item_ids, vectors, strict=True)
        ]
        statement = text(
            f"INSERT INTO {ITEM_TABLE} (item_id, embedding, model, dimensions, updated_at) "
            f"VALUES (:item_id, CAST(:embedding AS {self.dialect.column_sql}), "
            f":model, :dimensions, now()) "
            "ON CONFLICT (item_id) DO UPDATE SET "
            "embedding = EXCLUDED.embedding, model = EXCLUDED.model, "
            "dimensions = EXCLUDED.dimensions, updated_at = now()"
        )
        async with self.engine.begin() as connection:
            await connection.execute(statement, rows)

    async def search(
        self,
        vector: Sequence[float],
        limit: int = 20,
        *,
        since: str | None = None,
        kinds: Sequence[str] | None = None,
    ) -> list[SearchHit]:
        conditions = ["v.embedding IS NOT NULL"]
        params: dict[str, Any] = {
            "query": to_pgvector(vector),
            "limit": int(limit),
        }
        if since:
            conditions.append("i.publication_date >= CAST(:since AS date)")
            params["since"] = since
        if kinds:
            conditions.append("i.kind = ANY(:kinds)")
            params["kinds"] = list(kinds)
        where = " AND ".join(conditions)
        statement = text(
            "SELECT v.item_id, i.title, i.abstract, "
            f"1 - (v.embedding <=> CAST(:query AS {self.dialect.column_sql})) AS score "
            f"FROM {ITEM_TABLE} v JOIN items i ON i.id = v.item_id "
            f"WHERE {where} "
            f"ORDER BY v.embedding <=> CAST(:query AS {self.dialect.column_sql}) "
            "LIMIT :limit"
        )
        async with self.engine.connect() as connection:
            if self.dialect.index_kind is VectorIndexKind.HNSW:
                await connection.execute(text(f"SET LOCAL hnsw.ef_search = {self.ef_search}"))
            result = await connection.execute(statement, params)
            rows = result.mappings().all()
        return [
            SearchHit(
                item_id=row["item_id"],
                score=float(row["score"]),
                title=row["title"],
                snippet=(row["abstract"] or "")[:400] or None,
            )
            for row in rows
        ]

    async def delete(self, item_ids: Sequence[UUID]) -> None:
        if not item_ids:
            return
        async with self.engine.begin() as connection:
            await connection.execute(
                text(f"DELETE FROM {ITEM_TABLE} WHERE item_id = ANY(:ids)"),
                {"ids": [str(item_id) for item_id in item_ids]},
            )

    async def count(self) -> int:
        async with self.engine.connect() as connection:
            return int(await connection.scalar(text(f"SELECT count(*) FROM {ITEM_TABLE}")) or 0)


class PgProfileVectorStore:
    """Версионированный кэш векторов профилей. Пересобираемый, не источник истины."""

    def __init__(self, engine: AsyncEngine, dialect: PgVectorDialect) -> None:
        self.engine = engine
        self.dialect = dialect

    async def ensure_collection(self, dimensions: int) -> None:
        if dimensions != self.dialect.dimensions:
            raise ValueError(
                f"Кэш профилей создан под {self.dialect.dimensions} измерений, "
                f"эмбеддер отдаёт {dimensions}"
            )

    async def get(self, profile_id: UUID, version: int) -> list[float] | None:
        async with self.engine.connect() as connection:
            row = await connection.scalar(
                text(
                    f"SELECT embedding::text FROM {PROFILE_TABLE} "
                    "WHERE profile_id = :profile_id AND version = :version"
                ),
                {"profile_id": str(profile_id), "version": int(version)},
            )
        if row is None:
            return None
        return [float(part) for part in str(row).strip("[]").split(",") if part]

    async def upsert(self, profile_id: UUID, version: int, vector: Sequence[float]) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO {PROFILE_TABLE} (profile_id, version, embedding, updated_at) "
                    f"VALUES (:profile_id, :version, "
                    f"CAST(:embedding AS {self.dialect.column_sql}), now()) "
                    "ON CONFLICT (profile_id, version) DO UPDATE SET "
                    "embedding = EXCLUDED.embedding, updated_at = now()"
                ),
                {
                    "profile_id": str(profile_id),
                    "version": int(version),
                    "embedding": to_pgvector(vector),
                },
            )

    async def delete(self, profile_id: UUID) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text(f"DELETE FROM {PROFILE_TABLE} WHERE profile_id = :profile_id"),
                {"profile_id": str(profile_id)},
            )
