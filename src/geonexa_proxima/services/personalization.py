"""Отбор кандидатов под профиль и прозрачное слияние оценок.

Ключевое здесь — поиск по граням. Профиль из нескольких тем («математические
модели в геотехнике… также ИИ в обработке опытов») в одном векторе превращается
в центроид между ними: статья, глубоко попадающая в одну тему, получает средний
косинус, потому что центроид оттянут остальными темами. Чем больше у человека
интересов, тем сильнее размывание — и тем увереннее выпадает как раз то, что
ему нужнее всего.

Поэтому ищем не одним вектором, а набором: весь профиль плюс каждая его тема
отдельно. Близость берём максимумом по граням, реранкер спрашиваем той гранью,
которой материал нашёлся (узкий запрос — точнее оценка), а объяснение и итоговую
сводку LLM делает уже по всему профилю: узкой гранью можно найти материал, но
нельзя объяснить, зачем он этому человеку целиком.

`PROFILE_FACET_LIMIT=0` возвращает прежнее поведение с одним вектором.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from geonexa_proxima.config import Settings
from geonexa_proxima.domain import ItemKind, StoredItem
from geonexa_proxima.ports import (
    Embedder,
    ItemRepository,
    ProfileExplainer,
    ProfileVectorStore,
    Reranker,
    VectorStore,
)
from geonexa_proxima.services.facets import (
    ProfileFacet,
    build_facets,
    interest_variants,
    with_full_profile,
)


class ProfileLike(Protocol):
    id: UUID
    user_id: UUID
    version: int
    compiled_text: str


@dataclass(slots=True)
class PersonalizedItem:
    item: StoredItem
    profile_score_id: UUID
    personal_score: float
    semantic_score: float
    reranker_score: float
    global_score: float
    interest_score: float
    explanation: str = ""
    #: Грань профиля, которой материал нашёлся. Пусто — профилем целиком.
    matched_facet: str = ""


@dataclass(slots=True)
class _Scored:
    """Промежуточная строка ранжирования: материал и все его составляющие."""

    item: StoredItem
    personal: float
    semantic: float
    reranker: float
    global_score: float
    interest: float
    facet: ProfileFacet


class PersonalizationService:
    """Rank global items for one profile without mutating global scientific scores."""

    def __init__(
        self,
        *,
        settings: Settings,
        item_repository: ItemRepository,
        profile_repository: Any,
        embedder: Embedder,
        item_vectors: VectorStore,
        profile_vectors: ProfileVectorStore,
        reranker: Reranker | None = None,
        explainer: ProfileExplainer | None = None,
    ) -> None:
        self.settings = settings
        self.item_repository = item_repository
        self.profile_repository = profile_repository
        self.embedder = embedder
        self.item_vectors = item_vectors
        self.profile_vectors = profile_vectors
        self.reranker = reranker
        self.explainer = explainer

    async def rank(
        self,
        profile: ProfileLike,
        *,
        limit: int = 20,
        since: datetime | None = None,
        kinds: set[ItemKind] | None = None,
        explain_top: int = 10,
        minimum_global_score: float = 0,
        minimum_personal_score: float = 0,
    ) -> list[PersonalizedItem]:
        facets = with_full_profile(
            profile.compiled_text,
            build_facets(
                profile.compiled_text,
                limit=self.settings.profile_facet_limit,
                min_chars=self.settings.profile_facet_min_chars,
            ),
        )
        vectors = await self._facet_vectors(profile, facets)
        candidate_limit = max(limit, self.settings.personalization_candidate_limit)
        facet_limit = max(limit, self.settings.personalization_facet_candidate_limit)

        # Пул базы — два соединения (DB_POOL_SIZE), и это не оплошность, а
        # бюджет: сервис живёт рядом с воркерами и Prefect на одной базе.
        # Поэтому параллелим ровно две операции, как и раньше, а грани
        # обходим последовательно. Разослать девять поисков через gather
        # означало бы мгновенный pool_timeout на профиле с девятью гранями —
        # и выглядело бы это как «дайджест иногда не собирается».
        global_items, full_hits = await asyncio.gather(
            self.item_repository.list_digest_candidates(
                minimum_global_score,
                candidate_limit,
                since,
            ),
            self._search(vectors[0], candidate_limit, kinds),
        )
        hit_lists = [full_hits]
        for vector in vectors[1:]:
            hit_lists.append(await self._search(vector, facet_limit, kinds))

        full_semantic: dict[UUID, float] = {}
        facet_semantic: dict[UUID, float] = {}
        matched: dict[UUID, tuple[float, ProfileFacet]] = {}
        for facet, hits in zip(facets, hit_lists, strict=True):
            for hit in hits:
                score = _cosine_to_unit(hit.score)
                if facet.is_full_profile:
                    full_semantic[hit.item_id] = score
                else:
                    facet_semantic[hit.item_id] = max(facet_semantic.get(hit.item_id, 0.0), score)
                best = matched.get(hit.item_id)
                if best is None or score > best[0]:
                    matched[hit.item_id] = (score, facet)

        items = {item.id: item for item in global_items}
        missing_ids = [item_id for item_id in matched if item_id not in items]
        # Одним запросом, а не пачкой параллельных `get`: гранями находится
        # ровно то, чего нет в общей выборке, и таких материалов бывают десятки.
        for item in await _get_many(self.item_repository, missing_ids):
            items[item.id] = item
        # Окно дайджеста обязано действовать и на векторные попадания. SQL-ветка
        # ограничена `since`, а векторная — нет, и без этого фильтра материал
        # трёхлетней давности с высоким косинусом попадал бы в каждый недельный
        # дайджест заново: списка «уже отправленного» в системе нет, и `since` —
        # единственная защита от повторов.
        if since is not None:
            items = {
                item_id: item
                for item_id, item in items.items()
                if item.created_at is None or item.created_at >= since
            }
        items = self._passing_gate(items, full_semantic, facet_semantic, minimum_global_score)
        if kinds is not None:
            items = {item_id: item for item_id, item in items.items() if item.kind in kinds}
        if not items:
            return []

        ordered = list(items.values())
        # Материал, пришедший из общей выборки без единого векторного попадания,
        # относим к профилю целиком: ни одна грань его не находила.
        facet_of = {item.id: matched.get(item.id, (0.0, facets[0]))[1] for item in ordered}
        interests = await self.profile_repository.list_interests(profile.user_id, profile.id)
        signals = await self.profile_repository.list_profile_signals(profile.user_id, profile.id)

        # Предварительная оценка — та же формула без реранкера, который мы ещё
        # не считали. Нужна, чтобы отсечь хвост ДО кросс-энкодера: граней до
        # тридцати двух, и без потолка одна рассылка означала бы тысячи пар
        # «запрос-документ» на профиль. Реранкер — самая дорогая часть цикла, и
        # росла она незаметно: раньше кандидатов было сто.
        prepared: list[_Scored] = []
        for item in ordered:
            observed = [
                value
                for value in (full_semantic.get(item.id), facet_semantic.get(item.id))
                if value is not None
            ]
            # Максимум, а не среднее: смысл граней в том, чтобы «попал в одну
            # тему целиком» перестало проигрывать «слегка похож на всё сразу».
            semantic_score = max(observed) if observed else _NO_SEMANTIC_EVIDENCE
            prepared.append(
                _Scored(
                    item=item,
                    personal=0.0,
                    semantic=semantic_score,
                    reranker=0.0,
                    global_score=_clamp(item.rank.total_score / 10 if item.rank else 0),
                    interest=_interest_score(item, interests, signals),
                    facet=facet_of[item.id],
                )
            )
        prepared.sort(key=self._without_reranker, reverse=True)
        del prepared[max(limit, self.settings.personalization_rerank_limit) :]

        reranker_scores = await self._rerank(
            [row.item for row in prepared], facet_of, facets, prepared
        )
        ranked: list[_Scored] = []
        for row in prepared:
            reranker_unit = _clamp(float(reranker_scores[row.item.id]))
            ranked.append(
                _Scored(
                    item=row.item,
                    personal=(
                        self._without_reranker(row)
                        + self.settings.personal_reranker_weight * reranker_unit
                    ),
                    semantic=row.semantic,
                    reranker=reranker_unit,
                    global_score=row.global_score,
                    interest=row.interest,
                    facet=row.facet,
                )
            )
        ranked.sort(key=lambda row: row.personal, reverse=True)
        # Порог личной оценки применяем ДО квоты. Иначе квота резервирует место
        # слабой грани, вызывающий код выбрасывает этот материал по порогу, и
        # дайджест приходит короче, чем мог бы, — вытесненный материал уже не
        # вернуть.
        if minimum_personal_score > 0:
            ranked = [row for row in ranked if row.personal >= minimum_personal_score]
        selected = _apply_facet_quota(ranked, limit, self.settings.personal_facet_min_slots)

        results: list[PersonalizedItem] = []
        for index, row in enumerate(selected):
            explanation = ""
            if self.explainer is not None and index < explain_top:
                try:
                    # Объяснение — по всему профилю, а не по грани: гранью
                    # материал найден, но зачем он человеку целиком, знает
                    # только полный профиль.
                    explanation = await self.explainer.explain(
                        row.item,
                        profile_text=profile.compiled_text,
                        personal_score=row.personal,
                    )
                except Exception:
                    explanation = ""
            facet_text = "" if row.facet.is_full_profile else row.facet.text
            score = await self.profile_repository.upsert_profile_item_score(
                user_id=profile.user_id,
                profile_id=profile.id,
                item_id=row.item.id,
                profile_version=profile.version,
                semantic_score=row.semantic,
                reranker_score=row.reranker,
                global_score=row.global_score,
                interest_score=row.interest,
                personal_score=row.personal,
                explanation=explanation,
                matched_facet=facet_text or None,
            )
            results.append(
                PersonalizedItem(
                    item=row.item,
                    profile_score_id=score.id,
                    personal_score=row.personal,
                    semantic_score=row.semantic,
                    reranker_score=row.reranker,
                    global_score=row.global_score,
                    interest_score=row.interest,
                    explanation=explanation,
                    matched_facet=facet_text,
                )
            )
        return results

    def _without_reranker(self, row: _Scored) -> float:
        """Личная оценка без вклада реранкера.

        Используется дважды: как ключ отсечения хвоста до кросс-энкодера и как
        слагаемое итоговой суммы. Одна формула в двух местах — чтобы отсечение
        и итог не разъезжались.
        """

        return (
            self.settings.personal_semantic_weight * row.semantic
            + self.settings.personal_global_weight * row.global_score
            + self.settings.personal_interest_weight * row.interest
        )

    def _passing_gate(
        self,
        items: dict[UUID, StoredItem],
        full_semantic: dict[UUID, float],
        facet_semantic: dict[UUID, float],
        minimum_global_score: float,
    ) -> dict[UUID, StoredItem]:
        """Общий научный порог с обходом для сильного семантического попадания.

        Порог стоит на общей оценке статьи, одинаковой для всех подписчиков, и
        именно он отсекал случай «в мой интерес попало, а в общем не прошло»:
        узкая тема почти всегда весит меньше, чем средний научный балл. Поэтому
        очень близкий материал проходит и с низкой общей оценкой.

        Обход смотрит на лучшее попадание — и по грани, и по профилю целиком.
        Только по граням получалась дыра в логике: материал, найденный профилем
        с косинусом 0.95, отсекался, а найденный гранью с 0.60 — проходил.

        Порог задан в косинусе (как `SEMANTIC_THRESHOLD`), а сравнение идёт в
        долях единицы, поэтому переводим. Раньше настройка описывалась косинусом,
        а сравнивалась с приведённым значением: `0.45` из соседней настройки
        означало бы «пропускать всё подряд», потому что ортогональный материал
        в этой шкале равен `0.5`.

        Обход требует, чтобы статья была оценена: `rank is None` означает, что
        глобальный ранкер до неё не дошёл, и в дайджесте она выглядела бы
        строкой «без оценки». `PERSONAL_FACET_OVERRIDE_SCORE=1` выключает обход.
        """

        if minimum_global_score <= 0:
            return items
        override = _cosine_to_unit(self.settings.personal_facet_override_score)
        passing: dict[UUID, StoredItem] = {}
        for item_id, item in items.items():
            if item.rank is None:
                continue
            if item.rank.total_score >= minimum_global_score:
                passing[item_id] = item
                continue
            best = max(full_semantic.get(item_id, 0.0), facet_semantic.get(item_id, 0.0))
            if best >= override:
                passing[item_id] = item
        return passing

    async def _search(
        self,
        vector: list[float],
        limit: int,
        kinds: set[ItemKind] | None,
    ) -> list[Any]:
        """Векторный поиск с отбором по виду материала там, где он поддержан.

        Отбор по виду делается и после выборки, но полагаться только на него
        нельзя: на запрос `/datasets` сорок ближайших соседей окажутся статьями,
        и после фильтра не останется ничего. Хранилище Qdrant таких параметров
        не принимает — для него остаётся выборка пошире и фильтр после.
        """

        if kinds:
            try:
                return await self.item_vectors.search(
                    vector, limit=limit, kinds=[kind.value for kind in kinds]
                )
            except TypeError:
                pass
        return await self.item_vectors.search(vector, limit=limit)

    async def _rerank(
        self,
        ordered: Sequence[StoredItem],
        facet_of: dict[UUID, ProfileFacet],
        facets: Sequence[ProfileFacet],
        prepared: Sequence[_Scored] = (),
    ) -> dict[UUID, float]:
        """Оценить материалы реранкером — каждый своей гранью.

        Реранкер сравнивает пару «запрос — документ», и запрос из четырёх
        несвязанных тем для него такой же шум, как и для эмбеддера. Материал,
        найденный гранью «ИИ в обработке опытов», спрашивается именно ей.

        Число пар не меняется: каждый документ оценивается ровно один раз,
        просто вызовов становится столько, сколько задействовано граней.
        """

        if self.reranker is None:
            # Без реранкера его слагаемое дублирует семантическое: другого
            # свидетельства у нас нет. Это осознанная деградация, а не оценка —
            # молча ставить сюда ноль было бы хуже, он занижал бы всё подряд.
            fallback = {row.item.id: row.semantic for row in prepared}
            return {item.id: fallback.get(item.id, _NO_SEMANTIC_EVIDENCE) for item in ordered}
        groups: dict[int, list[StoredItem]] = defaultdict(list)
        for item in ordered:
            groups[facet_of[item.id].index].append(item)
        by_index = {facet.index: facet for facet in facets}
        scores: dict[UUID, float] = {}
        for index in sorted(groups):
            group = groups[index]
            query = by_index[index].text
            values = await self.reranker.score(query, [_item_text(item) for item in group])
            if len(values) != len(group):
                raise ValueError("Reranker returned a different number of scores than candidates")
            scores.update(
                {item.id: float(value) for item, value in zip(group, values, strict=True)}
            )
        return scores

    async def _facet_vectors(
        self, profile: ProfileLike, facets: Sequence[ProfileFacet]
    ) -> list[list[float]]:
        """Векторы всех граней, с кэшем по (профиль, версия, грань).

        Кэш обязателен, а не желателен: диспетчер обходит все профили в каждом
        прогоне, и без него каждая рассылка перевычисляла бы по вектору на
        каждую грань каждого профиля. Недостающие грани эмбеддятся одной пачкой.
        """

        await self.profile_vectors.ensure_collection(self.embedder.dimensions)
        # Последовательно, а не через gather: соединений в пуле два, и девять
        # одновременных чтений кэша упёрлись бы в pool_timeout. Запросы по
        # первичному ключу, их цена — round-trip, а не работа базы.
        vectors: list[list[float] | None] = [
            await self.profile_vectors.get(
                profile.id, profile.version, facet.index, facet.text_hash
            )
            for facet in facets
        ]
        missing = [position for position, vector in enumerate(vectors) if vector is None]
        if missing:
            fresh = await _embed_queries(self.embedder, [facets[i].text for i in missing])
            for position, vector in zip(missing, fresh, strict=True):
                vectors[position] = vector
                await self.profile_vectors.upsert(
                    profile.id,
                    profile.version,
                    vector,
                    facets[position].index,
                    facets[position].text_hash,
                )
        return [vector for vector in vectors if vector is not None]


async def _get_many(repository: ItemRepository, item_ids: Sequence[UUID]) -> list[StoredItem]:
    """Материалы пачкой, если репозиторий это умеет, иначе по одному.

    Проверка вместо требования: реализации `ItemRepository` приходят и из
    тестов, и обязать их все завести `get_many` — значит уронить сервис на том,
    у кого его нет.
    """

    if not item_ids:
        return []
    batch = getattr(repository, "get_many", None)
    if batch is not None:
        return list(await batch(list(item_ids)))
    found = [await repository.get(item_id) for item_id in item_ids]
    return [item for item in found if item is not None]


async def _embed_queries(embedder: Embedder, texts: Sequence[str]) -> list[list[float]]:
    """Пачка запросов, если эмбеддер это умеет, иначе по одному.

    Проверка вместо требования: эмбеддеры приходят и снаружи (совместимый
    HTTP-провайдер, подмена в тестах), и обязать их все реализовать батч —
    значит уронить сервис на том, у кого его нет.
    """

    batch = getattr(embedder, "embed_queries", None)
    if batch is not None:
        return list(await batch(list(texts)))
    return [await embedder.embed_query(text) for text in texts]


def _apply_facet_quota(ranked: list[_Scored], limit: int, min_slots: int) -> list[_Scored]:
    """Мягкая квота: каждой грани — гарантированные места, остальное по баллу.

    Без неё одна «горячая» тема забирает весь дайджест: у профиля из четырёх
    тем в топ-20 попадает одна, и остальные три интереса человек просто не
    видит — при том, что материалы по ним нашлись и лежат чуть ниже.

    Квота именно мягкая: она резервирует места, а не делит выдачу поровну.
    Порядок брони — по силе лучшего материала грани, чтобы при нехватке мест
    первой отсекалась самая слабая тема, а не последняя по алфавиту. Итог
    пересортирован по общему баллу: дайджест всё равно читается сверху вниз.
    """

    if limit <= 0:
        return []
    if min_slots <= 0 or len(ranked) <= limit:
        return ranked[:limit]

    groups: dict[int, list[_Scored]] = defaultdict(list)
    for row in ranked:
        groups[row.facet.index].append(row)
    # Грань с самым сильным материалом бронирует первой.
    order = sorted(groups, key=lambda index: groups[index][0].personal, reverse=True)

    reserved: list[_Scored] = []
    taken: set[UUID] = set()
    for slot in range(min_slots):
        for index in order:
            if len(reserved) >= limit:
                break
            group = groups[index]
            if slot < len(group):
                reserved.append(group[slot])
                taken.add(group[slot].item.id)
    selected = (
        reserved
        + [row for row in ranked if row.item.id not in taken][: max(0, limit - len(reserved))]
    )
    selected.sort(key=lambda row: row.personal, reverse=True)
    return selected


def _item_text(item: StoredItem) -> str:
    parts = [item.title]
    if item.abstract:
        parts.append(item.abstract)
    if item.rank and item.rank.categories:
        parts.append("Categories: " + ", ".join(item.rank.categories))
    return "\n\n".join(parts)


#: Что ставим материалу, которого не нашёл ни один векторный запрос.
#:
#: Ровно середина шкалы — то же, что косинус 0: «похожести не измеряли».
#: Раньше сюда подставлялся `item.semantic_score`, но это близость к общему
#: профилю сбора, а не к профилю подписчика, да ещё и смешанная с вероятностью
#: реранкера при загрузке. Получалось, что материал, которого не нашёл никто,
#: обгонял по семантике материал, который грань реально нашла.
_NO_SEMANTIC_EVIDENCE = 0.5


def _cosine_to_unit(score: float) -> float:
    return _clamp((float(score) + 1) / 2)


def _interest_score(
    item: StoredItem,
    interests: Sequence[object],
    signals: Sequence[object],
) -> float:
    haystack = _item_text(item).casefold()
    weighted = 0.0
    maximum = 0.0
    for preference in [*interests, *signals]:
        term = (
            getattr(preference, "query", None)
            or getattr(preference, "topic_name", None)
            or getattr(preference, "term", None)
        )
        if not term:
            continue
        weight = abs(float(getattr(preference, "weight", 1.0)))
        polarity = str(getattr(preference, "polarity", "positive"))
        maximum += weight
        # Любого из написаний достаточно: интерес «liquefaction; разжижение
        # грунтов» должен совпадать с английским текстом статьи, оставаясь
        # читаемым по-русски.
        if any(variant.casefold() in haystack for variant in interest_variants(str(term))):
            weighted += -weight if polarity.endswith("negative") else weight
    if maximum == 0:
        return 0.5
    return _clamp(0.5 + 0.5 * weighted / maximum)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
