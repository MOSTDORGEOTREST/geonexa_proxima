"""Lazy asynchronous Qdrant implementation of the VectorStore port."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from geonexa_proxima.config import Settings
from geonexa_proxima.domain import SearchHit


class QdrantVectorStore:
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
            try:
                from qdrant_client import AsyncQdrantClient
            except ImportError as error:
                raise RuntimeError("Qdrant support requires qdrant-client") from error
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
            configured_size = _vector_size(collection)
            if configured_size is not None and configured_size != dimensions:
                raise ValueError(
                    f"Qdrant collection has {configured_size} dimensions; expected {dimensions}"
                )
            return
        from qdrant_client.models import Distance, VectorParams

        await client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=dimensions, distance=Distance.COSINE),
        )

    async def upsert(
        self,
        item_ids: Sequence[UUID],
        vectors: Sequence[Sequence[float]],
        payloads: Sequence[dict[str, object]],
    ) -> None:
        if not (len(item_ids) == len(vectors) == len(payloads)):
            raise ValueError("item_ids, vectors, and payloads must have equal lengths")
        from qdrant_client.models import PointStruct

        points = [
            PointStruct(
                id=str(item_id),
                vector=[float(value) for value in vector],
                payload={"item_id": str(item_id), **payload},
            )
            for item_id, vector, payload in zip(item_ids, vectors, payloads, strict=True)
        ]
        if points:
            await self._client().upsert(
                collection_name=self.collection,
                points=points,
                wait=True,
            )

    async def search(self, vector: Sequence[float], limit: int = 20) -> list[SearchHit]:
        response = await self._client().query_points(
            collection_name=self.collection,
            query=[float(value) for value in vector],
            limit=limit,
            with_payload=True,
        )
        points = getattr(response, "points", response)
        hits: list[SearchHit] = []
        for point in points:
            payload = dict(getattr(point, "payload", None) or {})
            item_id = payload.get("item_id") or point.id
            hits.append(
                SearchHit(
                    item_id=UUID(str(item_id)),
                    score=float(point.score),
                    title=str(payload.get("title") or "Untitled"),
                    snippet=_snippet(payload),
                )
            )
        return hits

    async def retrieve_payloads(self, item_ids: Sequence[UUID]) -> dict[UUID, dict[str, object]]:
        if not item_ids:
            return {}
        records = await self._client().retrieve(
            collection_name=self.collection,
            ids=[str(item_id) for item_id in item_ids],
            with_payload=True,
            with_vectors=False,
        )
        result: dict[UUID, dict[str, object]] = {}
        for record in records:
            payload = dict(getattr(record, "payload", None) or {})
            item_id = UUID(str(payload.get("item_id") or record.id))
            result[item_id] = payload
        return result

    async def get_payload(self, item_id: UUID) -> dict[str, object] | None:
        return (await self.retrieve_payloads([item_id])).get(item_id)

    async def aclose(self) -> None:
        if self._owns_client and self._client_instance is not None:
            await self._client_instance.close()


def create_vector_store(settings: Settings) -> QdrantVectorStore:
    return QdrantVectorStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None,
        collection=settings.qdrant_collection,
    )


def _vector_size(collection: object) -> int | None:
    config = getattr(collection, "config", None)
    params = getattr(config, "params", None)
    vectors = getattr(params, "vectors", None)
    size = getattr(vectors, "size", None)
    return size if isinstance(size, int) else None


def _snippet(payload: dict[str, object]) -> str | None:
    value = payload.get("snippet") or payload.get("abstract")
    return str(value) if value else None
