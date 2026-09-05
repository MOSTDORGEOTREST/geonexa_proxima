"""Гейт в пайплайне: что не доходит до эмбеддингов и почему."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from geonexa_proxima.domain import CollectedItem, RankResult, SourceName, StoredItem
from geonexa_proxima.harvest import HarvestMatcher, load_harvest_profile
from geonexa_proxima.services.decisions import NullDecisionSink
from geonexa_proxima.services.ingestion import IngestionService

RELEVANT = [
    (
        "Physics-informed neural networks for undrained shear strength from CPT",
        "A PINN fuses cone penetration test soundings with a critical state soil model.",
    ),
    (
        "Deep learning landslide susceptibility mapping using InSAR time series",
        "Convolutional network trained on interferometric deformation series.",
    ),
    (
        "Graph neural network surrogate for discrete element method simulations",
        "Learned simulator for granular material flow; code is available.",
    ),
    # Мягкий гейт: геотехнический якорь без ML тоже проходит — источники и так
    # опрашиваются профильными запросами, а отсеивать по строгому правилу
    # значило терять большинство собранного (332 из 385 за месяц).
    ("Slope stability analysis using limit equilibrium methods", "No learning component."),
]
IRRELEVANT = [
    ("Deep learning for protein folding prediction", "AlphaFold-style model."),
    ("Soil microbiome diversity under fertilizer regimes", "Crop yield study."),
    ("Transformer architectures for sentiment analysis of reviews", ""),
    ("A bibliometric review of machine learning in geotechnical engineering", "Trends."),
]


def _item(title: str, abstract: str, index: int) -> CollectedItem:
    return CollectedItem(
        source=SourceName.ARXIV,
        external_id=f"probe-{index}",
        title=title,
        abstract=abstract or None,
    )


class _Repo:
    def __init__(self) -> None:
        self.saved: list[CollectedItem] = []

    async def save_collected(self, item: CollectedItem) -> tuple[StoredItem, bool]:
        self.saved.append(item)
        return StoredItem(id=uuid4(), kind=item.kind, title=item.title), True

    async def set_semantic_score(self, item_id, score) -> None: ...
    async def set_rank(self, item_id, rank) -> None: ...
    async def set_analysis(self, item_id, analysis) -> None: ...


class _Embedder:
    """Считает, сколько текстов реально дошло до модели."""

    def __init__(self) -> None:
        self.documents: list[str] = []
        self.queries: list[str] = []

    @property
    def dimensions(self) -> int:
        return 4

    async def embed_documents(self, texts):
        self.documents.extend(texts)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    async def embed_query(self, text):
        self.queries.append(text)
        return [1.0, 0.0, 0.0, 0.0]


class _VectorStore:
    def __init__(self) -> None:
        self.upserted = 0

    async def ensure_collection(self, dimensions: int) -> None: ...

    async def upsert(self, item_ids, vectors, payloads) -> None:
        self.upserted += len(item_ids)

    async def search(self, vector, limit: int = 20):
        return []


class _Ranker:
    def __init__(self) -> None:
        self.calls = 0

    async def rank(self, item, semantic_score) -> RankResult:
        self.calls += 1
        return RankResult(
            relevance=7,
            novelty=6,
            scientific_quality=7,
            practical_value=7,
            importance_for_geotechnics=7,
            importance_for_ai=6,
            reason="проба",
        )


class _Analyzer:
    async def analyze(self, item, rank):
        raise AssertionError("глубокий разбор не должен запускаться в этом тесте")


class _Collector:
    def __init__(self, items: list[CollectedItem]) -> None:
        self.items = items

    async def collect(
        self, since: datetime, limit: int, until: datetime | None = None
    ) -> list[CollectedItem]:
        return self.items


@pytest.fixture(scope="module")
def matcher() -> HarvestMatcher:
    return HarvestMatcher(load_harvest_profile(Path("config/harvest.yaml")))


async def test_gate_keeps_noise_out_of_the_expensive_stages(matcher: HarvestMatcher) -> None:
    items = [_item(t, a, i) for i, (t, a) in enumerate(RELEVANT + IRRELEVANT)]
    repo, embedder, store, ranker = _Repo(), _Embedder(), _VectorStore(), _Ranker()
    sink = NullDecisionSink()
    service = IngestionService(
        collectors=[_Collector(items)],
        repository=repo,
        embedder=embedder,
        vector_store=store,
        ranker=ranker,
        analyzer=_Analyzer(),
        profile_text="геотехника и машинное обучение",
        matcher=matcher,
        decision_sink=sink,
        semantic_threshold=0.0,
        deep_analysis_threshold=11.0,
    )
    stats = await service.ingest(since=datetime.now(UTC), limit_per_source=50)

    assert stats.collected == len(items)
    assert stats.gate_rejected == len(IRRELEVANT), stats.blocked_by
    assert stats.gate_accepted + stats.gate_borderline == len(RELEVANT)
    # Главное: отсечённое не дошло ни до эмбеддера, ни до репозитория, ни до LLM.
    assert len(repo.saved) == len(RELEVANT)
    assert len(embedder.documents) == len(RELEVANT)
    assert ranker.calls == len(RELEVANT)
    assert store.upserted == len(RELEVANT)


async def test_every_decision_is_journalled(matcher: HarvestMatcher) -> None:
    items = [_item(t, a, i) for i, (t, a) in enumerate(RELEVANT + IRRELEVANT)]
    sink = NullDecisionSink()
    service = IngestionService(
        collectors=[_Collector(items)],
        repository=_Repo(),
        embedder=_Embedder(),
        vector_store=_VectorStore(),
        ranker=_Ranker(),
        analyzer=_Analyzer(),
        profile_text="геотехника",
        matcher=matcher,
        decision_sink=sink,
        semantic_threshold=0.0,
        deep_analysis_threshold=11.0,
    )
    await service.ingest(since=datetime.now(UTC), limit_per_source=50)

    decisions = {row["external_id"]: row for row in sink.recorded}
    # Журналируется КАЖДЫЙ материал, включая отклонённые: без них через месяц
    # нечем калибровать пороги и нечего чистить в списке терминов.
    assert len(decisions) == len(items)
    for row in sink.recorded:
        assert row["decision"] in {"accepted", "borderline", "rejected"}
        assert row["title"]
    rejected = [row for row in sink.recorded if row["decision"] == "rejected"]
    assert len(rejected) == len(IRRELEVANT)
    # У отклонённого по стоп-листу видно, какая группа сработала.
    blocked = [row for row in sink.recorded if row["blocked_by"]]
    assert all(row["decision"] == "rejected" for row in blocked)


async def test_store_rejected_off_keeps_journal_to_survivors(matcher: HarvestMatcher) -> None:
    """HARVEST_STORE_REJECTED=false — единственный способ не копить отказы."""

    items = [_item(t, a, i) for i, (t, a) in enumerate(RELEVANT + IRRELEVANT)]
    sink = NullDecisionSink()
    service = IngestionService(
        collectors=[_Collector(items)],
        repository=_Repo(),
        embedder=_Embedder(),
        vector_store=_VectorStore(),
        ranker=_Ranker(),
        analyzer=_Analyzer(),
        profile_text="геотехника",
        matcher=matcher,
        decision_sink=sink,
        semantic_threshold=0.0,
        deep_analysis_threshold=11.0,
        store_rejected=False,
    )
    await service.ingest(since=datetime.now(UTC), limit_per_source=50)

    assert not [row for row in sink.recorded if row["decision"] == "rejected"]
    assert len(sink.recorded) == len(RELEVANT)


async def test_keyword_threshold_from_settings_overrides_profile(matcher: HarvestMatcher) -> None:
    """Порог из .env перекрывает YAML — иначе калибровка требует релиза."""

    items = [_item(t, a, i) for i, (t, a) in enumerate(RELEVANT)]

    def run(threshold: float | None) -> object:
        return IngestionService(
            collectors=[_Collector(items)],
            repository=_Repo(),
            embedder=_Embedder(),
            vector_store=_VectorStore(),
            ranker=_Ranker(),
            analyzer=_Analyzer(),
            profile_text="геотехника",
            matcher=matcher,
            decision_sink=NullDecisionSink(),
            semantic_threshold=0.0,
            deep_analysis_threshold=11.0,
            keyword_threshold=threshold,
        )

    default = await run(None).ingest(since=datetime.now(UTC), limit_per_source=50)
    strict = await run(1.0).ingest(since=datetime.now(UTC), limit_per_source=50)

    # При пороге 1.0 «уверенно принято» не бывает: всё уходит в borderline.
    assert default.gate_accepted > 0
    assert strict.gate_accepted == 0
    assert strict.gate_borderline == len(RELEVANT)


async def test_without_matcher_everything_passes() -> None:
    """Матчер необязателен: без него сервис ведёт себя как раньше."""

    items = [_item(t, a, i) for i, (t, a) in enumerate(IRRELEVANT)]
    repo = _Repo()
    service = IngestionService(
        collectors=[_Collector(items)],
        repository=repo,
        embedder=_Embedder(),
        vector_store=_VectorStore(),
        ranker=_Ranker(),
        analyzer=_Analyzer(),
        profile_text="",
        semantic_threshold=0.0,
        deep_analysis_threshold=11.0,
    )
    stats = await service.ingest(since=datetime.now(UTC), limit_per_source=50)
    assert stats.gate_rejected == 0
    assert len(repo.saved) == len(IRRELEVANT)
