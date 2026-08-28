"""Lazy-loadable embedding and reranking providers."""

from geonexa_proxima.ml.embeddings import LocalQwen3Embedder, OpenAICompatibleEmbedder
from geonexa_proxima.ml.factory import create_embedder, create_reranker
from geonexa_proxima.ml.rerankers import HTTPReranker, LocalQwen3Reranker

Qwen3Embedder = LocalQwen3Embedder
Qwen3Reranker = LocalQwen3Reranker
OpenAIEmbedder = OpenAICompatibleEmbedder

__all__ = [
    "HTTPReranker",
    "LocalQwen3Embedder",
    "LocalQwen3Reranker",
    "OpenAICompatibleEmbedder",
    "OpenAIEmbedder",
    "Qwen3Embedder",
    "Qwen3Reranker",
    "create_embedder",
    "create_reranker",
]
