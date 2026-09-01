"""Async SQLAlchemy implementation of the domain ItemRepository port."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from geonexa_proxima.db.models import (
    AuthorModel,
    DatasetModel,
    ItemAuthorModel,
    ItemDatasetModel,
    ItemModel,
    ItemRepositoryLinkModel,
    ItemSourceModel,
    ItemTopicModel,
    RepositoryModel,
    TopicModel,
)
from geonexa_proxima.db.session import SessionFactory
from geonexa_proxima.domain import (
    Author,
    CollectedItem,
    DeepAnalysis,
    NotFoundError,
    RankResult,
    StoredItem,
)

_WHITESPACE = re.compile(r"\s+")
_ARXIV_VERSION = re.compile(r"v\d+$", re.IGNORECASE)


class ItemNotFoundError(NotFoundError):
    """Запрошенный канонический объект отсутствует."""


def normalize_title(value: str) -> str:
    """Normalize Unicode, case and whitespace without fuzzy matching."""

    normalized = unicodedata.normalize("NFKC", value)
    return _WHITESPACE.sub(" ", normalized).strip().casefold()


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized.removeprefix(prefix).strip()
            break
    return normalized or None


def normalize_arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.removesuffix(".pdf")
    normalized = normalized.removeprefix("arXiv:").removeprefix("arxiv:")
    return _ARXIV_VERSION.sub("", normalized).strip().casefold() or None


def _normalize_name(value: str) -> str:
    return normalize_title(value)


def _json_payload(value: Any) -> Any:
    """Convert Pydantic/domain values to JSON-safe primitives."""

    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", fallback=str)
    if isinstance(value, dict):
        return {str(key): _json_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_payload(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class SQLAlchemyItemRepository:
    """PostgreSQL repository with transactional canonical deduplication."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def save_collected(self, item: CollectedItem) -> tuple[StoredItem, bool]:
        normalized_title = normalize_title(item.title)
        if not normalized_title:
            raise ValueError("item title cannot be empty after normalization")
        if not item.external_id.strip():
            raise ValueError("item external_id cannot be empty")

        doi = normalize_doi(item.doi)
        arxiv_id = normalize_arxiv_id(item.arxiv_id)
        lock_keys = [
            f"source:{item.source.value}:{item.external_id}",
            f"title:{normalized_title}",
        ]
        if doi:
            lock_keys.append(f"doi:{doi}")
        if arxiv_id:
            lock_keys.append(f"arxiv:{arxiv_id.casefold()}")

        async with self._session_factory() as session, session.begin():
            await self._lock_identities(session, lock_keys)
            model = await self._find_canonical(
                session,
                doi=doi,
                arxiv_id=arxiv_id,
                normalized_title=normalized_title,
                source=item.source.value,
                external_id=item.external_id,
            )
            created = model is None
            if model is None:
                model = ItemModel(
                    kind=item.kind.value,
                    title=item.title.strip(),
                    normalized_title=normalized_title,
                    abstract=item.abstract,
                    doi=doi,
                    arxiv_id=arxiv_id,
                    canonical_url=str(item.url) if item.url else None,
                    publication_date=item.publication_date,
                    venue=item.venue,
                    citation_count=item.citation_count,
                )
                session.add(model)
                await session.flush()
            else:
                await self._enrich_canonical(session, model, item, doi=doi, arxiv_id=arxiv_id)

            await self._upsert_source(session, model.id, item)
            await self._attach_authors(session, model.id, item.authors)
            await self._attach_topics(session, model.id, item.keywords)
            if item.code_url:
                await self._attach_repository(session, model.id, str(item.code_url), item)
            if item.dataset_url:
                await self._attach_dataset(session, model.id, str(item.dataset_url), item)
            await session.flush()

        return self._to_domain(model), created

    async def set_semantic_score(self, item_id: UUID, score: float) -> None:
        if not math.isfinite(score) or not -1 <= score <= 1:
            raise ValueError("semantic score must be finite and between -1 and 1")
        await self._update_existing(item_id, semantic_score=score)

    async def set_rank(self, item_id: UUID, rank: RankResult) -> None:
        await self._update_existing(
            item_id,
            ranking=rank.model_dump(mode="json"),
            rank_total_score=rank.total_score,
        )

    async def set_analysis(self, item_id: UUID, analysis: DeepAnalysis) -> None:
        await self._update_existing(
            item_id,
            deep_analysis=analysis.model_dump(mode="json"),
        )

    async def list_digest_candidates(
        self,
        minimum_score: float,
        limit: int,
        since: datetime | None = None,
    ) -> list[StoredItem]:
        if not math.isfinite(minimum_score) or not 0 <= minimum_score <= 10:
            raise ValueError("minimum_score must be finite and between 0 and 10")
        if limit < 1:
            raise ValueError("limit must be positive")

        statement = (
            select(ItemModel)
            .where(ItemModel.rank_total_score >= minimum_score)
            .order_by(
                ItemModel.rank_total_score.desc(),
                ItemModel.publication_date.desc().nullslast(),
                ItemModel.created_at.desc(),
            )
            .limit(limit)
        )
        if since is not None:
            statement = statement.where(ItemModel.created_at >= since)
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
            return [self._to_domain(row) for row in rows]

    async def get(self, item_id: UUID) -> StoredItem | None:
        async with self._session_factory() as session:
            model = await session.get(ItemModel, item_id)
            return self._to_domain(model) if model else None

    async def get_many(self, item_ids: Sequence[UUID]) -> list[StoredItem]:
        """Материалы по списку идентификаторов — одним запросом.

        Персонализация добирает сюда всё, что нашлось векторным поиском мимо
        общей выборки, и таких материалов бывают десятки. Через `get` это
        означало бы десятки обращений к пулу из двух соединений: конкурентно —
        мгновенный `pool_timeout`, последовательно — десятки round-trip на
        каждый профиль в каждом прогоне диспетчера.

        Порядок результата не гарантируется: вызывающий раскладывает их по id.
        """

        unique = list(dict.fromkeys(item_ids))
        if not unique:
            return []
        async with self._session_factory() as session:
            rows = (await session.scalars(select(ItemModel).where(ItemModel.id.in_(unique)))).all()
            return [self._to_domain(row) for row in rows]

    async def _update_existing(self, item_id: UUID, **values: Any) -> None:
        values["updated_at"] = func.now()
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(ItemModel).where(ItemModel.id == item_id).values(**values)
            )
            if result.rowcount != 1:
                raise ItemNotFoundError(f"item {item_id} not found")

    @staticmethod
    async def _lock_identities(session: AsyncSession, keys: Iterable[str]) -> None:
        # Sorted locks avoid deadlocks when records share more than one identity.
        for key in sorted(set(keys)):
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": key},
            )

    @staticmethod
    async def _find_canonical(
        session: AsyncSession,
        *,
        doi: str | None,
        arxiv_id: str | None,
        normalized_title: str,
        source: str,
        external_id: str,
    ) -> ItemModel | None:
        statements: list[Select[tuple[ItemModel]]] = []
        if doi:
            statements.append(select(ItemModel).where(ItemModel.doi == doi))
        if arxiv_id:
            statements.append(select(ItemModel).where(ItemModel.arxiv_id == arxiv_id))
        statements.append(select(ItemModel).where(ItemModel.normalized_title == normalized_title))
        for statement in statements:
            model = await session.scalar(statement)
            if model is not None:
                return model

        # A stable source identity remains useful when a publisher changes title/metadata.
        return await session.scalar(
            select(ItemModel)
            .join(ItemSourceModel, ItemSourceModel.item_id == ItemModel.id)
            .where(
                ItemSourceModel.source == source,
                ItemSourceModel.external_id == external_id,
            )
        )

    @staticmethod
    async def _enrich_canonical(
        session: AsyncSession,
        model: ItemModel,
        item: CollectedItem,
        *,
        doi: str | None,
        arxiv_id: str | None,
    ) -> None:
        if item.abstract and (not model.abstract or len(item.abstract) > len(model.abstract)):
            model.abstract = item.abstract
        if model.doi is None and doi:
            doi_owner = await session.scalar(select(ItemModel.id).where(ItemModel.doi == doi))
            if doi_owner is None:
                model.doi = doi
        if model.arxiv_id is None and arxiv_id:
            arxiv_owner = await session.scalar(
                select(ItemModel.id).where(ItemModel.arxiv_id == arxiv_id)
            )
            if arxiv_owner is None:
                model.arxiv_id = arxiv_id
        model.canonical_url = model.canonical_url or (str(item.url) if item.url else None)
        model.publication_date = model.publication_date or item.publication_date
        model.venue = model.venue or item.venue
        if item.citation_count is not None:
            model.citation_count = max(model.citation_count or 0, item.citation_count)
        model.updated_at = func.now()

    @staticmethod
    async def _upsert_source(session: AsyncSession, item_id: UUID, item: CollectedItem) -> None:
        statement = insert(ItemSourceModel).values(
            item_id=item_id,
            source=item.source.value,
            external_id=item.external_id,
            raw_payload=_json_payload(item.raw),
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ItemSourceModel.source, ItemSourceModel.external_id],
            set_={
                "item_id": statement.excluded.item_id,
                "raw_payload": statement.excluded.raw_payload,
                "last_seen_at": func.now(),
            },
        )
        await session.execute(statement)

    async def _attach_authors(
        self, session: AsyncSession, item_id: UUID, authors: list[Author]
    ) -> None:
        existing_ids = set(
            await session.scalars(
                select(ItemAuthorModel.author_id).where(ItemAuthorModel.item_id == item_id)
            )
        )
        next_position = (
            await session.scalar(
                select(func.coalesce(func.max(ItemAuthorModel.position), -1)).where(
                    ItemAuthorModel.item_id == item_id
                )
            )
        ) + 1

        for author in authors:
            normalized_name = _normalize_name(author.name)
            if not normalized_name:
                continue
            lock_id = author.orcid.casefold() if author.orcid else normalized_name
            await self._lock_identities(session, [f"author:{lock_id}"])
            author_id = await self._upsert_author(session, author, normalized_name)
            if author_id in existing_ids:
                continue
            await session.execute(
                insert(ItemAuthorModel)
                .values(item_id=item_id, author_id=author_id, position=next_position)
                .on_conflict_do_nothing()
            )
            existing_ids.add(author_id)
            next_position += 1

    @staticmethod
    async def _upsert_author(session: AsyncSession, author: Author, normalized_name: str) -> UUID:
        if author.orcid:
            existing = await session.scalar(
                select(AuthorModel).where(AuthorModel.orcid == author.orcid)
            )
            if existing is not None:
                return existing.id

        statement = insert(AuthorModel).values(
            name=author.name.strip(),
            normalized_name=normalized_name,
            orcid=author.orcid,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[AuthorModel.normalized_name],
            set_={
                "name": statement.excluded.name,
                "orcid": func.coalesce(AuthorModel.orcid, statement.excluded.orcid),
            },
        ).returning(AuthorModel.id)
        return (await session.execute(statement)).scalar_one()

    @staticmethod
    async def _attach_topics(session: AsyncSession, item_id: UUID, keywords: list[str]) -> None:
        for keyword in keywords:
            normalized_name = normalize_title(keyword)
            if not normalized_name:
                continue
            topic_statement = insert(TopicModel).values(
                name=keyword.strip(),
                normalized_name=normalized_name,
            )
            topic_id = (
                await session.execute(
                    topic_statement.on_conflict_do_update(
                        index_elements=[TopicModel.normalized_name],
                        set_={"name": topic_statement.excluded.name},
                    ).returning(TopicModel.id)
                )
            ).scalar_one()
            await session.execute(
                insert(ItemTopicModel)
                .values(item_id=item_id, topic_id=topic_id)
                .on_conflict_do_nothing()
            )

    @staticmethod
    async def _attach_repository(
        session: AsyncSession, item_id: UUID, url: str, item: CollectedItem
    ) -> None:
        resource = insert(RepositoryModel).values(
            url=url,
            source=item.source.value,
            external_id=item.external_id if item.source.value == "github" else None,
        )
        repository_id = (
            await session.execute(
                resource.on_conflict_do_update(
                    index_elements=[RepositoryModel.url],
                    set_={
                        "source": func.coalesce(RepositoryModel.source, resource.excluded.source)
                    },
                ).returning(RepositoryModel.id)
            )
        ).scalar_one()
        await session.execute(
            insert(ItemRepositoryLinkModel)
            .values(item_id=item_id, repository_id=repository_id)
            .on_conflict_do_nothing()
        )

    @staticmethod
    async def _attach_dataset(
        session: AsyncSession, item_id: UUID, url: str, item: CollectedItem
    ) -> None:
        resource = insert(DatasetModel).values(
            url=url,
            source=item.source.value,
            external_id=item.external_id if item.source.value == "huggingface" else None,
        )
        dataset_id = (
            await session.execute(
                resource.on_conflict_do_update(
                    index_elements=[DatasetModel.url],
                    set_={"source": func.coalesce(DatasetModel.source, resource.excluded.source)},
                ).returning(DatasetModel.id)
            )
        ).scalar_one()
        await session.execute(
            insert(ItemDatasetModel)
            .values(item_id=item_id, dataset_id=dataset_id)
            .on_conflict_do_nothing()
        )

    @staticmethod
    def _to_domain(model: ItemModel) -> StoredItem:
        return StoredItem(
            id=model.id,
            kind=model.kind,
            title=model.title,
            abstract=model.abstract,
            doi=model.doi,
            arxiv_id=model.arxiv_id,
            canonical_url=model.canonical_url,
            publication_date=model.publication_date,
            semantic_score=model.semantic_score,
            rank=RankResult.model_validate(model.ranking) if model.ranking else None,
            analysis=(
                DeepAnalysis.model_validate(model.deep_analysis) if model.deep_analysis else None
            ),
            created_at=model.created_at,
        )
