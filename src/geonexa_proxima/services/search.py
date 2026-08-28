"""Semantic search application service."""

from __future__ import annotations

from geonexa_proxima.domain import SearchHit
from geonexa_proxima.ports import Embedder, ItemRepository, Reranker, VectorStore


class SearchService:
    def __init__(
        self,
        *,
        embedder: Embedder,
        vector_store: VectorStore,
        repository: ItemRepository,
        reranker: Reranker | None = None,
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.repository = repository
        self.reranker = reranker

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        profile_text: str | None = None,
    ) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        embedding_query = (
            f"User research profile:\n{profile_text}\n\nSearch request:\n{query}"
            if profile_text
            else query
        )
        vector = await self.embedder.embed_query(embedding_query)
        hits = await self.vector_store.search(vector, limit=max(limit, limit * 3))
        if not self.reranker or not hits:
            return hits[:limit]
        documents = ["\n".join(part for part in (hit.title, hit.snippet) if part) for hit in hits]
        scores = await self.reranker.score(embedding_query, documents)
        for hit, score in zip(hits, scores, strict=True):
            hit.score = 0.3 * ((hit.score + 1) / 2) + 0.7 * max(0.0, min(1.0, score))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:limit]
