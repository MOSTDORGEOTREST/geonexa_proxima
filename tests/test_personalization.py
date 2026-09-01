from types import SimpleNamespace
from uuid import uuid4

import pytest

from geonexa_proxima.config import Settings
from geonexa_proxima.domain import ItemKind, RankResult, SearchHit, StoredItem
from geonexa_proxima.services.personalization import PersonalizationService


class FakeItemRepository:
    def __init__(self, item: StoredItem) -> None:
        self.item = item

    async def list_digest_candidates(self, *_: object) -> list[StoredItem]:
        return [self.item]

    async def get(self, item_id: object) -> StoredItem | None:
        return self.item if item_id == self.item.id else None


class FakeProfileRepository:
    def __init__(self) -> None:
        self.saved: dict[str, object] = {}

    async def list_interests(self, *_: object) -> list[object]:
        return [SimpleNamespace(query="liquefaction", weight=1.0, polarity="positive")]

    async def list_profile_signals(self, *_: object) -> list[object]:
        return []

    async def upsert_profile_item_score(self, **values: object) -> object:
        self.saved = values
        return SimpleNamespace(id=uuid4())


class FakeEmbedder:
    dimensions = 2

    async def embed_query(self, _: str) -> list[float]:
        return [1.0, 0.0]


class FakeItemVectors:
    def __init__(self, item_id: object) -> None:
        self.item_id = item_id

    async def search(self, _: object, limit: int = 20) -> list[SearchHit]:
        return [SearchHit(item_id=self.item_id, score=0.8, title="Paper")]


class FakeProfileVectors:
    def __init__(self) -> None:
        self.saved = False

    async def ensure_collection(self, _: int) -> None:
        return None

    async def get(self, *_: object) -> None:
        return None

    async def upsert(self, *_: object) -> None:
        self.saved = True


class FakeReranker:
    async def score(self, _: str, documents: object) -> list[float]:
        return [0.9 for _ in documents]


@pytest.mark.asyncio
async def test_personalization_fuses_all_scores_and_persists_snapshot() -> None:
    rank = RankResult(
        relevance=8,
        novelty=8,
        scientific_quality=8,
        practical_value=8,
        importance_for_geotechnics=8,
        importance_for_ai=8,
        reason="Strong paper",
    )
    item = StoredItem(
        kind=ItemKind.PAPER,
        title="ML prediction of soil liquefaction",
        rank=rank,
    )
    profile = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        version=2,
        compiled_text="liquefaction ML",
    )
    profile_repository = FakeProfileRepository()
    profile_vectors = FakeProfileVectors()
    service = PersonalizationService(
        settings=Settings(_env_file=None, admin_password="test-password"),
        item_repository=FakeItemRepository(item),  # type: ignore[arg-type]
        profile_repository=profile_repository,
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
        item_vectors=FakeItemVectors(item.id),  # type: ignore[arg-type]
        profile_vectors=profile_vectors,
        reranker=FakeReranker(),  # type: ignore[arg-type]
    )

    results = await service.rank(profile, limit=1)

    assert results[0].personal_score == pytest.approx(0.885)
    assert profile_repository.saved["profile_version"] == 2
    assert profile_vectors.saved is True
