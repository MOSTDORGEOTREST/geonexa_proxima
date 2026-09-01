"""Поиск по граням профиля: разбор, отбор и квота.

Профиль из нескольких тем в одном векторе — это центроид между ними. Статья,
глубоко попадающая в одну тему, получает средний косинус, потому что центроид
оттянут остальными темами, и выпадает как раз то, что человеку нужнее всего.
Тесты закрывают три места, где эта ошибка возвращается молча: разбор профиля на
грани, обход общего порога сильным попаданием и квоту на разнообразие.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from geonexa_proxima.config import Settings
from geonexa_proxima.domain import ItemKind, RankResult, SearchHit, StoredItem
from geonexa_proxima.services.facets import (
    FULL_PROFILE,
    build_facets,
    sections,
    split_sentences,
    with_full_profile,
)
from geonexa_proxima.services.personalization import PersonalizationService, _apply_facet_quota
from geonexa_proxima.services.profiles import ProfileCompiler

PROFILE = """Base taxonomy:
инженерная геология, геотехника, ИИ

Profile description:
Меня интересуют математические модели в геотехнике, механики и разжижения
грунтов. Также ии в области обработки опытов

Explicit interests:
- positive: разжижение грунтов (weight=5)
- negative: распознавание трещин в асфальте (weight=3)"""


# --------------------------------------------------------------------------- #
# Разбор профиля                                                               #
# --------------------------------------------------------------------------- #


def test_description_splits_into_separate_topics() -> None:
    """Ровно тот профиль, на котором ломался поиск: две темы через точку."""

    facets = build_facets(PROFILE)
    texts = [facet.text for facet in facets]

    assert texts[0].startswith("Меня интересуют математические модели")
    assert texts[1] == "Также ии в области обработки опытов"
    # Явный интерес — уже отдельная тема, склеивать его обратно в общий вектор
    # было бы той же ошибкой.
    assert "разжижение грунтов" in texts


def test_negative_interests_never_become_facets() -> None:
    """Грань — это поисковый запрос.

    Запрос «распознавание трещин в асфальте» принёс бы ровно то, что человек
    просил не показывать: минус в профиле превратился бы в плюс в выдаче.
    """

    assert all("асфальт" not in facet.text for facet in build_facets(PROFILE))


def test_facet_zero_is_always_the_whole_profile() -> None:
    facets = with_full_profile(PROFILE, build_facets(PROFILE))

    assert facets[0].index == FULL_PROFILE
    assert facets[0].is_full_profile
    assert facets[0].text == PROFILE
    # Номера остальных идут подряд с единицы: это ключи кэша векторов.
    assert [facet.index for facet in facets[1:]] == list(range(1, len(facets)))
    assert not any(facet.is_full_profile for facet in facets[1:])


def test_limit_zero_restores_the_single_vector_behaviour() -> None:
    assert build_facets(PROFILE, limit=0) == []
    assert len(build_facets(PROFILE, limit=1)) == 1


def test_profile_without_description_yields_no_facets() -> None:
    """Свежий профиль — только таксономия. Граней нет, поведение прежнее."""

    assert build_facets("Base taxonomy:\nинженерная геология") == []


def test_blank_line_inside_the_description_does_not_split_sections() -> None:
    """Описание пишет человек, и абзац в нём — обычное дело.

    Разбор по пустым строкам приписал бы вторую половину описания следующему
    разделу, и явные интересы попали бы в грань вместе с текстом.
    """

    compiled = (
        "Profile description:\nПервый абзац про геотехнику и модели.\n\n"
        "Второй абзац про обработку опытов ИИ.\n\n"
        "Explicit interests:\n- positive: разжижение грунтов (weight=5)"
    )
    parts = sections(compiled)

    assert "Второй абзац" in parts["Profile description"]
    assert "Explicit interests" not in parts["Profile description"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Дробное число не должно разваливаться на обрывки.
        (
            "мод. 2.5 кг и ещё что-то длинное про грунты.",
            ["мод. 2.5 кг и ещё что-то длинное про грунты."],
        ),
        # Точка с запятой и перевод строки — такие же границы тем.
        (
            "Первая тема довольно длинная про геотехнику;\nвторая тема про обработку опытов",
            ["Первая тема довольно длинная про геотехнику;", "вторая тема про обработку опытов"],
        ),
        # Текст короче минимума гранью не становится: искать по нему нечего.
        ("грунты", []),
        ("...   —  ", []),
        # Перевод строки не должен склеивать соседние темы встык.
        (
            "Разжижение грунтов при нагрузках.\nИИ для обработки данных опытов.",
            ["Разжижение грунтов при нагрузках.", "ИИ для обработки данных опытов."],
        ),
        # Перечисление построчно — это перечисление тем.
        (
            "Модели в геотехнике и механике\nОбработка опытов с помощью ИИ",
            ["Модели в геотехнике и механике", "Обработка опытов с помощью ИИ"],
        ),
        # А жёсткий перенос посреди предложения — не граница темы: строчная
        # буква в начале строки означает, что мысль продолжается.
        (
            "Математические модели в геотехнике и механике\nгрунтов при землетрясениях",
            ["Математические модели в геотехнике и механике грунтов при землетрясениях"],
        ),
    ],
)
def test_sentence_splitting_edges(text: str, expected: list[str]) -> None:
    assert split_sentences(text, min_chars=16) == expected


def test_short_tail_joins_the_previous_topic_instead_of_becoming_one() -> None:
    """«Также ИИ.» отдельной гранью — это грань из одного слова.

    Она заняла бы место в квоте разнообразия и притащила бы шум по всему
    корпусу: слишком общий запрос похож на что угодно.
    """

    chunks = split_sentences("Длинная первая тема про геотехнику. Также ИИ.", min_chars=16)

    assert len(chunks) == 1
    assert chunks[0].endswith("Также ИИ.")


def test_compiler_output_parses_back_into_facets() -> None:
    """Компилятор и разбор — две стороны одного формата.

    Переименовали раздел в компиляторе — грани молча перестали находиться, и
    поиск тихо вернулся к одному вектору. Ловится только сверкой.
    """

    compiled = ProfileCompiler("инженерная геология").compile_profile(
        description="Модели в геотехнике и механике грунтов. Обработка опытов с помощью ИИ.",
        interests=[
            SimpleNamespace(
                polarity=SimpleNamespace(value="positive"),
                target_text="разжижение грунтов",
                weight=5.0,
                id=uuid4(),
            )
        ],
        learned_signals=[],
    )
    facets = build_facets(compiled)

    assert len(facets) == 3
    assert {facet.source for facet in facets} == {"description", "interest"}
    assert facets[-1].text == "разжижение грунтов"


# --------------------------------------------------------------------------- #
# Квота разнообразия                                                           #
# --------------------------------------------------------------------------- #


def _row(facet_index: int, personal: float) -> object:
    from geonexa_proxima.services.facets import ProfileFacet
    from geonexa_proxima.services.personalization import _Scored

    return _Scored(
        item=StoredItem(kind=ItemKind.PAPER, title=f"item-{facet_index}-{personal}"),
        personal=personal,
        semantic=personal,
        reranker=personal,
        global_score=personal,
        interest=personal,
        facet=ProfileFacet(index=facet_index, text=f"тема {facet_index}", source="description"),
    )


def test_one_hot_topic_does_not_take_the_whole_digest() -> None:
    """Без квоты выдача целиком про одну тему, хотя нашлись все четыре."""

    ranked = [_row(1, 0.9 - index * 0.01) for index in range(10)]
    ranked += [_row(2, 0.5), _row(3, 0.45)]
    ranked.sort(key=lambda row: row.personal, reverse=True)

    selected = _apply_facet_quota(ranked, limit=4, min_slots=1)
    facets = {row.facet.index for row in selected}

    assert len(selected) == 4
    assert facets == {1, 2, 3}
    # Выдача всё равно читается сверху вниз по убыванию балла.
    assert [row.personal for row in selected] == sorted(
        (row.personal for row in selected), reverse=True
    )


def test_quota_zero_is_a_plain_ranking() -> None:
    ranked = [_row(1, 0.9), _row(1, 0.8), _row(2, 0.1)]

    assert [row.personal for row in _apply_facet_quota(ranked, limit=2, min_slots=0)] == [0.9, 0.8]


def test_quota_does_nothing_when_everything_fits() -> None:
    ranked = [_row(1, 0.9), _row(2, 0.8)]

    assert _apply_facet_quota(ranked, limit=5, min_slots=2) == ranked


# --------------------------------------------------------------------------- #
# Отбор кандидатов                                                             #
# --------------------------------------------------------------------------- #


def _ranked(total: float) -> RankResult:
    return RankResult(
        relevance=total,
        novelty=total,
        scientific_quality=total,
        practical_value=total,
        importance_for_geotechnics=total,
        importance_for_ai=total,
        reason="—",
    )


class _Repository:
    """Корпус целиком плюс отдельный список «общих кандидатов».

    Разделение не косметическое: материал, который не попал в общую выборку и
    нашёлся только вектором грани, — это и есть проверяемый случай. Если
    подсунуть его обоими путями, тест пройдёт даже со сломанным поиском по граням.
    """

    def __init__(
        self, items: list[StoredItem], *, candidates: list[StoredItem] | None = None
    ) -> None:
        self.items = {item.id: item for item in items}
        self.candidates = items if candidates is None else candidates
        self.batched: list[list[object]] = []

    async def list_digest_candidates(self, minimum: float, *_: object) -> list[StoredItem]:
        return [item for item in self.candidates if item.rank and item.rank.total_score >= minimum]

    async def get(self, item_id: object) -> StoredItem | None:
        return self.items.get(item_id)  # type: ignore[arg-type]

    async def get_many(self, item_ids: list[object]) -> list[StoredItem]:
        self.batched.append(list(item_ids))
        return [self.items[item_id] for item_id in item_ids if item_id in self.items]


class _Profiles:
    def __init__(self) -> None:
        self.saved: list[dict[str, object]] = []

    async def list_interests(self, *_: object) -> list[object]:
        return []

    async def list_profile_signals(self, *_: object) -> list[object]:
        return []

    async def upsert_profile_item_score(self, **values: object) -> object:
        self.saved.append(values)
        return SimpleNamespace(id=uuid4())


class _Embedder:
    dimensions = 2

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [1.0, 0.0]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self.queries.extend(texts)
        return [[1.0, 0.0] for _ in texts]


class _ItemVectors:
    """Каждая грань находит свой материал: индекс запроса решает, чей."""

    def __init__(self, by_call: list[list[SearchHit]]) -> None:
        self.by_call = by_call
        self.calls = 0

    async def search(self, _: object, limit: int = 20) -> list[SearchHit]:
        hits = self.by_call[self.calls] if self.calls < len(self.by_call) else []
        self.calls += 1
        return hits


class _ProfileVectors:
    def __init__(self) -> None:
        self.upserted: list[int] = []
        self.keys: list[tuple[int, str]] = []

    async def ensure_collection(self, _: int) -> None:
        return None

    async def get(
        self, _profile: object, _version: int, facet: int = 0, text_hash: str = ""
    ) -> None:
        return None

    async def upsert(
        self,
        _profile: object,
        _version: int,
        _vector: object,
        facet: int = 0,
        text_hash: str = "",
    ) -> None:
        self.upserted.append(facet)
        self.keys.append((facet, text_hash))


class _Reranker:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.sizes: list[int] = []

    async def score(self, query: str, documents: object) -> list[float]:
        self.queries.append(query)
        batch = list(documents)
        self.sizes.append(len(batch))
        return [0.5 for _ in batch]


def _service(repository: object, vectors: object, profiles: object, **overrides: object):
    store = _ProfileVectors()
    reranker = _Reranker()
    settings = Settings(_env_file=None, admin_password="test-password", **overrides)
    service = PersonalizationService(
        settings=settings,
        item_repository=repository,  # type: ignore[arg-type]
        profile_repository=profiles,
        embedder=_Embedder(),  # type: ignore[arg-type]
        item_vectors=vectors,  # type: ignore[arg-type]
        profile_vectors=store,  # type: ignore[arg-type]
        reranker=reranker,  # type: ignore[arg-type]
    )
    return service, store, reranker


def _profile() -> object:
    return SimpleNamespace(id=uuid4(), user_id=uuid4(), version=1, compiled_text=PROFILE)


@pytest.mark.asyncio
async def test_each_facet_gets_its_own_vector_search_and_cached_vector() -> None:
    narrow = StoredItem(kind=ItemKind.PAPER, title="ИИ в обработке опытов", rank=_ranked(7))
    repository = _Repository([narrow], candidates=[])
    vectors = _ItemVectors([[], [], [SearchHit(item_id=narrow.id, score=0.9, title="x")]])
    service, store, _ = _service(repository, vectors, _Profiles())

    results = await service.rank(_profile(), limit=5, minimum_global_score=0)

    # Полный профиль плюс три грани — четыре запроса, четыре кэшированных вектора.
    assert vectors.calls == 4
    assert store.upserted == [0, 1, 2, 3]
    assert [item.item.id for item in results] == [narrow.id]
    assert results[0].matched_facet == "Также ии в области обработки опытов"


@pytest.mark.asyncio
async def test_strong_facet_match_passes_the_global_threshold() -> None:
    """Тот самый случай: «в мой интерес попало, а в общем не прошло».

    Общая научная оценка одинакова для всех подписчиков и про интересы ничего
    не знает. Без обхода узкая, но точно нужная человеку статья не доезжает.
    """

    weak = StoredItem(kind=ItemKind.PAPER, title="Узкая по обработке опытов", rank=_ranked(3))
    repository = _Repository([weak])  # общий порог всё равно выбросит его из выборки
    hits = [[], [], [SearchHit(item_id=weak.id, score=0.95, title="x")]]
    service, _, _ = _service(repository, _ItemVectors(hits), _Profiles())

    passed = await service.rank(_profile(), limit=5, minimum_global_score=6.5)
    assert [item.item.id for item in passed] == [weak.id]

    # С выключенным обходом — прежнее поведение. Выключает единица, а не ноль:
    # ноль в шкале косинуса означал бы «пропускать вообще всё».
    strict, _, _ = _service(
        repository, _ItemVectors(hits), _Profiles(), personal_facet_override_score=1
    )
    assert await strict.rank(_profile(), limit=5, minimum_global_score=6.5) == []


@pytest.mark.asyncio
async def test_a_weak_facet_match_still_respects_the_threshold() -> None:
    """Обход — для сильного попадания, а не для любого."""

    weak = StoredItem(kind=ItemKind.PAPER, title="Мимо", rank=_ranked(3))
    hits = [[], [], [SearchHit(item_id=weak.id, score=0.2, title="x")]]
    service, _, _ = _service(_Repository([weak]), _ItemVectors(hits), _Profiles())

    assert await service.rank(_profile(), limit=5, minimum_global_score=6.5) == []


@pytest.mark.asyncio
async def test_reranker_is_asked_with_the_narrow_facet_not_the_whole_profile() -> None:
    """Реранкер сравнивает пару «запрос — документ».

    Запрос из четырёх несвязанных тем для него такой же шум, как и для
    эмбеддера: материал, найденный узкой гранью, надо спрашивать ей же.
    """

    narrow = StoredItem(kind=ItemKind.PAPER, title="ИИ и опыты", rank=_ranked(7))
    vectors = _ItemVectors([[], [], [SearchHit(item_id=narrow.id, score=0.9, title="x")]])
    service, _, reranker = _service(_Repository([narrow], candidates=[]), vectors, _Profiles())

    await service.rank(_profile(), limit=5, minimum_global_score=0)

    assert reranker.queries == ["Также ии в области обработки опытов"]


@pytest.mark.asyncio
async def test_items_found_only_by_facets_are_fetched_in_one_query() -> None:
    """Пул базы — два соединения, и это бюджет, а не оплошность.

    Гранями находится ровно то, чего нет в общей выборке, и таких материалов
    бывают десятки. Пачка параллельных `get` упёрлась бы в `pool_timeout`, и
    выглядело бы это как «дайджест иногда не собирается».
    """

    narrow = StoredItem(kind=ItemKind.PAPER, title="ИИ и опыты", rank=_ranked(7))
    other = StoredItem(kind=ItemKind.PAPER, title="Модели грунтов", rank=_ranked(7))
    repository = _Repository([narrow, other], candidates=[])
    vectors = _ItemVectors(
        [
            [],
            [SearchHit(item_id=other.id, score=0.7, title="y")],
            [SearchHit(item_id=narrow.id, score=0.9, title="x")],
        ]
    )
    service, _, _ = _service(repository, vectors, _Profiles())

    await service.rank(_profile(), limit=5, minimum_global_score=0)

    assert len(repository.batched) == 1
    assert sorted(map(str, repository.batched[0])) == sorted([str(other.id), str(narrow.id)])


@pytest.mark.asyncio
async def test_matched_facet_is_persisted_for_the_why_answer() -> None:
    narrow = StoredItem(kind=ItemKind.PAPER, title="ИИ и опыты", rank=_ranked(7))
    vectors = _ItemVectors([[], [], [SearchHit(item_id=narrow.id, score=0.9, title="x")]])
    profiles = _Profiles()
    service, _, _ = _service(_Repository([narrow], candidates=[]), vectors, profiles)

    await service.rank(_profile(), limit=5, minimum_global_score=0)

    assert profiles.saved[0]["matched_facet"] == "Также ии в области обработки опытов"


@pytest.mark.asyncio
async def test_digest_window_applies_to_vector_hits_too() -> None:
    """Иначе старая статья возвращается в каждый недельный дайджест заново.

    SQL-ветка ограничена окном `since`, векторная — нет, а списка «уже
    отправленного» в системе не существует: окно и есть единственная защита от
    повторов. Без фильтра материал трёхлетней давности с высоким косинусом
    приходил бы вечно.
    """

    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    stale = StoredItem(
        kind=ItemKind.PAPER, title="Старое", rank=_ranked(8), created_at=now - timedelta(days=400)
    )
    fresh = StoredItem(
        kind=ItemKind.PAPER, title="Свежее", rank=_ranked(8), created_at=now - timedelta(hours=2)
    )
    vectors = _ItemVectors(
        [
            [],
            [],
            [
                SearchHit(item_id=stale.id, score=0.99, title="x"),
                SearchHit(item_id=fresh.id, score=0.90, title="y"),
            ],
        ]
    )
    service, _, _ = _service(_Repository([stale, fresh], candidates=[]), vectors, _Profiles())

    results = await service.rank(_profile(), limit=5, since=now - timedelta(days=7))

    assert [item.item.id for item in results] == [fresh.id]


@pytest.mark.asyncio
async def test_candidate_set_is_capped_before_the_reranker() -> None:
    """Кросс-энкодер — самая дорогая часть цикла, и росла она незаметно.

    Граней может быть до тридцати двух, каждая тянет свою выборку. Без потолка
    одна рассылка означала бы тысячи пар «запрос-документ» на профиль.
    """

    items = [
        StoredItem(kind=ItemKind.PAPER, title=f"Материал {index}", rank=_ranked(8))
        for index in range(40)
    ]
    vectors = _ItemVectors(
        [[SearchHit(item_id=item.id, score=0.8, title="x") for item in items], [], [], []]
    )
    service, _, reranker = _service(
        _Repository(items, candidates=[]),
        vectors,
        _Profiles(),
        personalization_rerank_limit=10,
    )

    await service.rank(_profile(), limit=5)

    assert sum(reranker.sizes) == 10


@pytest.mark.asyncio
async def test_override_threshold_is_a_cosine_not_a_unit_score() -> None:
    """Настройка описана косинусом — и сравниваться должна с косинусом.

    В приведённой шкале ортогональный материал равен 0.5, поэтому значение
    вроде 0.45 из соседней настройки означало бы «пропускать вообще всё».
    """

    weak = StoredItem(kind=ItemKind.PAPER, title="Слабая", rank=_ranked(3))
    # Косинус 0.55 ниже порога 0.60, хотя в приведённой шкале это 0.775.
    hits = [[], [], [SearchHit(item_id=weak.id, score=0.55, title="x")]]
    service, _, _ = _service(_Repository([weak]), _ItemVectors(hits), _Profiles())
    assert await service.rank(_profile(), limit=5, minimum_global_score=6.5) == []

    # А косинус 0.65 — выше.
    hits = [[], [], [SearchHit(item_id=weak.id, score=0.65, title="x")]]
    service, _, _ = _service(_Repository([weak]), _ItemVectors(hits), _Profiles())
    assert len(await service.rank(_profile(), limit=5, minimum_global_score=6.5)) == 1


@pytest.mark.asyncio
async def test_whole_profile_match_can_also_bypass_the_threshold() -> None:
    """Обход по граням в одиночку давал дыру в логике.

    Материал, найденный профилем целиком с косинусом 0.95, отсекался, а
    найденный гранью с 0.60 — проходил. Обход смотрит на лучшее попадание.
    """

    weak = StoredItem(kind=ItemKind.PAPER, title="Слабая по науке", rank=_ranked(3))
    hits = [[SearchHit(item_id=weak.id, score=0.95, title="x")], [], [], []]
    service, _, _ = _service(_Repository([weak]), _ItemVectors(hits), _Profiles())

    assert len(await service.rank(_profile(), limit=5, minimum_global_score=6.5)) == 1


@pytest.mark.asyncio
async def test_item_nobody_found_does_not_outrank_a_real_match() -> None:
    """Раньше материал без единого попадания обгонял по семантике настоящее.

    В качестве запасного значения бралась близость к общему профилю сбора,
    смешанная с вероятностью реранкера при загрузке, — другая величина в другой
    шкале. Теперь это честная середина: «похожесть не измеряли».
    """

    found = StoredItem(kind=ItemKind.PAPER, title="Нашли гранью", rank=_ranked(8))
    unseen = StoredItem(
        kind=ItemKind.PAPER, title="Не нашёл никто", rank=_ranked(8), semantic_score=0.9
    )
    vectors = _ItemVectors([[], [], [SearchHit(item_id=found.id, score=0.7, title="x")]])
    service, _, _ = _service(_Repository([found, unseen]), vectors, _Profiles())

    results = await service.rank(_profile(), limit=5)

    assert results[0].item.id == found.id


@pytest.mark.asyncio
async def test_personal_threshold_is_applied_before_the_quota() -> None:
    """Квота не должна тратить место на материал, который потом выбросят.

    Раньше порог личной оценки применял вызывающий код к готовой выдаче: место,
    зарезервированное слабой гранью, пропадало, и дайджест приходил короче, чем
    мог бы, — вытесненный материал уже не вернуть.
    """

    strong = [
        StoredItem(kind=ItemKind.PAPER, title=f"Сильный {index}", rank=_ranked(9))
        for index in range(4)
    ]
    weak = StoredItem(kind=ItemKind.PAPER, title="Слабый", rank=_ranked(1))
    vectors = _ItemVectors(
        [
            [SearchHit(item_id=item.id, score=0.9, title="x") for item in strong],
            [],
            [SearchHit(item_id=weak.id, score=0.62, title="y")],
        ]
    )
    service, _, _ = _service(_Repository([*strong, weak], candidates=[]), vectors, _Profiles())

    results = await service.rank(_profile(), limit=3, minimum_personal_score=0.6)

    assert len(results) == 3
    assert weak.id not in {item.item.id for item in results}


@pytest.mark.asyncio
async def test_facet_vector_cache_is_keyed_by_the_text_too() -> None:
    """Номер грани позиционный, а текст под ним зависит ещё и от настроек.

    Без отпечатка смена `PROFILE_FACET_MIN_CHARS` молча оставляла бы под старым
    номером чужой вектор: поиск шёл бы по прошлому тексту, а объяснение
    называло бы новую тему.
    """

    item = StoredItem(kind=ItemKind.PAPER, title="Что-то", rank=_ranked(8))
    service, store, _ = _service(_Repository([item]), _ItemVectors([[], [], [], []]), _Profiles())

    await service.rank(_profile(), limit=1)

    assert store.keys, "векторы граней должны попадать в кэш"
    assert all(text_hash for _, text_hash in store.keys[1:]), "у каждой грани свой отпечаток"
    assert len({text_hash for _, text_hash in store.keys}) == len(store.keys)


@pytest.mark.asyncio
async def test_whole_profile_match_leaves_the_facet_empty() -> None:
    """Нашёлся профилем целиком — называть в объяснении нечего."""

    broad = StoredItem(kind=ItemKind.PAPER, title="На стыке тем", rank=_ranked(8))
    vectors = _ItemVectors([[SearchHit(item_id=broad.id, score=0.99, title="x")], [], [], []])
    profiles = _Profiles()
    service, _, _ = _service(_Repository([broad]), vectors, profiles)

    results = await service.rank(_profile(), limit=5, minimum_global_score=0)

    assert results[0].matched_facet == ""
    assert profiles.saved[0]["matched_facet"] is None
