"""Local Qwen3 and OpenAI-compatible embedding providers."""

from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from geonexa_proxima.collectors.base import AsyncHTTPProvider, as_dict, as_list


class LocalQwen3Embedder:
    """Lazy sentence-transformers adapter; ML packages are imported on first use."""

    def __init__(
        self,
        *,
        local_path: Path,
        model_id: str,
        dimensions: int,
        batch_size: int = 16,
        hf_token: str | None = None,
        query_prompt: str | None = None,
        document_prompt: str | None = None,
        device: str | None = None,
    ) -> None:
        self.local_path = local_path
        self.model_id = model_id
        self._dimensions = dimensions
        self.batch_size = batch_size
        self.hf_token = hf_token
        self.query_prompt = query_prompt
        self.document_prompt = document_prompt
        self.device = device
        self._model: Any | None = None
        self._load_lock = threading.Lock()

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def selected_model(self) -> str:
        return str(self.local_path) if self.local_path.exists() else self.model_id

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._encode, list(texts), False)

    async def embed_query(self, text: str) -> list[float]:
        vectors = await asyncio.to_thread(self._encode, [text], True)
        return vectors[0]

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as error:
                    raise RuntimeError(
                        "Local embeddings require the optional sentence-transformers dependency"
                    ) from error
                kwargs: dict[str, object] = {
                    "truncate_dim": self._dimensions,
                    "model_kwargs": {"torch_dtype": "float16"},
                }
                if self.device:
                    kwargs["device"] = self.device
                if self.hf_token:
                    kwargs["token"] = self.hf_token
                self._model = SentenceTransformer(self.selected_model, **kwargs)
        return self._model

    def _encode(self, texts: list[str], is_query: bool) -> list[list[float]]:
        model = self._load()
        kwargs: dict[str, object] = {
            "batch_size": self.batch_size,
            "normalize_embeddings": True,
            "show_progress_bar": False,
        }
        explicit_prompt = self.query_prompt if is_query else self.document_prompt
        prompt_name = "query" if is_query else "document"
        if explicit_prompt:
            kwargs["prompt"] = explicit_prompt
        elif prompt_name in (getattr(model, "prompts", {}) or {}):
            kwargs["prompt_name"] = prompt_name
        vectors = model.encode(texts, **kwargs)
        result = vectors.tolist() if hasattr(vectors, "tolist") else vectors
        return [_truncate_and_normalize(vector, self._dimensions) for vector in result]


class OpenAICompatibleEmbedder(AsyncHTTPProvider):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        batch_size: int = 16,
        request_dimensions: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.url = f"{base_url.rstrip('/')}/embeddings"
        self.api_key = api_key
        self.model = model
        self._dimensions = dimensions
        self.batch_size = batch_size
        self.request_dimensions = request_dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(await self._embed(list(texts[start : start + self.batch_size])))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text]))[0]

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        body: dict[str, object] = {"model": self.model, "input": texts, "encoding_format": "float"}
        if self.request_dimensions:
            body["dimensions"] = self._dimensions
        response = await self._request(
            "POST",
            self.url,
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        data = sorted(
            (as_dict(value) for value in as_list(as_dict(response.json()).get("data"))),
            key=lambda value: int(value.get("index", 0)),
        )
        if len(data) != len(texts):
            raise ValueError(f"Embedding API returned {len(data)} vectors for {len(texts)} texts")
        return [
            _truncate_and_normalize(as_list(value.get("embedding")), self._dimensions)
            for value in data
        ]


def _truncate_and_normalize(vector: Sequence[object], dimensions: int) -> list[float]:
    """Apply Qwen3 MRL truncation and mandatory post-truncation L2 normalization."""

    if len(vector) < dimensions:
        raise ValueError(
            f"Embedding has {len(vector)} dimensions, fewer than configured {dimensions}"
        )
    result = [float(component) for component in vector[:dimensions]]
    norm = math.sqrt(sum(component * component for component in result))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Embedding API returned a zero or non-finite vector")
    return [component / norm for component in result]
