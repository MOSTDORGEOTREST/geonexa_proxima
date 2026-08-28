"""Qdrant cache for compiled user-profile embeddings."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from geonexa_proxima.config import Settings


class QdrantProfileVectorStore:
    """Rebuildable vector cache; PostgreSQL profile text remains authoritative."""

    def __init__(
        self,
        *,
        url: str,
        collection: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        client: Any | None = None,
    ) -> None:
        self.url = url
        self.collection = collection
        self.api_key = api_key
        self.timeout = timeout
        self._client_instance = client
        self._owns_client = client is None

    def _client(self) -> Any:
        if self._client_instance is None:
            from qdrant_client import AsyncQdrantClient

            self._client_instance = AsyncQdrantClient(
                url=self.url,
                api_key=self.api_key,
                timeout=self.timeout,
            )
        return self._client_instance

    async def ensure_collection(self, dimensions: int) -> None:
        client = self._client()
        if await client.collection_exists(self.collection):
            collection = await client.get_collection(self.collection)
            vectors = getattr(getattr(collection.config, "params", None), "vectors", None)
            configured_size = getattr(vectors, "size", None)
            if configured_size is not None and configured_size != dimensions:
                raise ValueError(
                    f"Profile collection has {configured_size} dimensions; expected {dimensions}"
                )
            return

        from qdrant_client.models import Distance, VectorParams

        await client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=dimensions, distance=Distance.COSINE),
        )

    async def get(self, profile_id: UUID, version: int) -> list[float] | None:
        records = await self._client().retrieve(
            collection_name=self.collection,
            ids=[str(profile_id)],
            with_payload=True,
            with_vectors=True,
        )
        if not records:
            return None
        record = records[0]
        payload = dict(getattr(record, "payload", None) or {})
        if payload.get("version") != version:
            return None
        vector = getattr(record, "vector", None)
        if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)):
            return None
        return [float(value) for value in vector]

    async def upsert(self, profile_id: UUID, version: int, vector: Sequence[float]) -> None:
        from qdrant_client.models import PointStruct

        await self._client().upsert(
            collection_name=self.collection,
            points=[
                PointStruct(
                    id=str(profile_id),
                    vector=[float(value) for value in vector],
                    payload={"profile_id": str(profile_id), "version": version},
                )
            ],
            wait=True,
        )

    async def delete(self, profile_id: UUID) -> None:
        from qdrant_client.models import PointIdsList

        await self._client().delete(
            collection_name=self.collection,
            points_selector=PointIdsList(points=[str(profile_id)]),
            wait=True,
        )

    async def aclose(self) -> None:
        if self._owns_client and self._client_instance is not None:
            await self._client_instance.close()


def create_profile_vector_store(settings: Settings) -> QdrantProfileVectorStore:
    return QdrantProfileVectorStore(
        url=settings.qdrant_url,
        collection=settings.qdrant_profile_collection,
        api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None,
    )
