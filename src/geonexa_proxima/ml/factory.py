"""Embedding and reranking factories selected by Settings."""

from __future__ import annotations

from geonexa_proxima.config import ProviderMode, Settings
from geonexa_proxima.ml.embeddings import LocalQwen3Embedder, OpenAICompatibleEmbedder
from geonexa_proxima.ml.rerankers import HTTPReranker, LocalQwen3Reranker
from geonexa_proxima.ports import Embedder, Reranker


def create_embedder(settings: Settings) -> Embedder:
    if settings.embedding_mode == ProviderMode.LOCAL:
        return LocalQwen3Embedder(
            local_path=settings.embedding_local_path,
            model_id=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            batch_size=settings.embedding_batch_size,
            hf_token=settings.hf_token.get_secret_value() if settings.hf_token else None,
        )
    return OpenAICompatibleEmbedder(
        base_url=settings.embedding_api_base_url,
        api_key=settings.embedding_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        batch_size=settings.embedding_batch_size,
    )


def create_reranker(settings: Settings) -> Reranker:
    if settings.reranker_mode == ProviderMode.LOCAL:
        return LocalQwen3Reranker(
            local_path=settings.reranker_local_path,
            model_id=settings.reranker_model,
            batch_size=settings.reranker_batch_size,
            hf_token=settings.hf_token.get_secret_value() if settings.hf_token else None,
        )
    return HTTPReranker(
        url=settings.reranker_api_url,
        api_key=settings.reranker_api_key.get_secret_value(),
        model=settings.reranker_model,
        batch_size=settings.reranker_batch_size,
    )
