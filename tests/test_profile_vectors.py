from types import SimpleNamespace
from uuid import uuid4

import pytest

from geonexa_proxima.vector.profiles import QdrantProfileVectorStore


class FakeProfileQdrant:
    def __init__(self, record: object | None = None) -> None:
        self.record = record

    async def retrieve(self, **_: object) -> list[object]:
        return [self.record] if self.record is not None else []


@pytest.mark.asyncio
async def test_profile_vector_cache_checks_profile_version() -> None:
    profile_id = uuid4()
    client = FakeProfileQdrant(SimpleNamespace(payload={"version": 3}, vector=[0.1, 0.2, 0.3]))
    store = QdrantProfileVectorStore(
        url="http://qdrant.invalid",
        collection="profiles",
        client=client,
    )

    assert await store.get(profile_id, 3) == [0.1, 0.2, 0.3]
    assert await store.get(profile_id, 2) is None


@pytest.mark.asyncio
async def test_profile_vector_cache_miss() -> None:
    store = QdrantProfileVectorStore(
        url="http://qdrant.invalid",
        collection="profiles",
        client=FakeProfileQdrant(),
    )

    assert await store.get(uuid4(), 1) is None
