"""Local Qwen3 yes/no reranker and generic HTTP reranking adapter."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from geonexa_proxima.collectors.base import AsyncHTTPProvider, as_dict, as_list
from geonexa_proxima.ml.local_models import preferred_dtype

_SYSTEM = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the "
    'Query and the Instruct provided. Note that the answer can only be "yes" or "no".'
    "<|im_end|>\n<|im_start|>user\n"
)
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
_DEFAULT_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"


class LocalQwen3Reranker:
    """Qwen3-Reranker adapter using the model's next-token yes/no probability."""

    def __init__(
        self,
        *,
        local_path: Path,
        model_id: str,
        batch_size: int = 16,
        hf_token: str | None = None,
        instruction: str = _DEFAULT_INSTRUCTION,
        max_length: int = 8192,
        device: str | None = None,
    ) -> None:
        self.local_path = local_path
        self.model_id = model_id
        self.batch_size = batch_size
        self.hf_token = hf_token
        self.instruction = instruction
        self.max_length = max_length
        self.device = device
        self._model: Any | None = None
        self._tokenizer: Any | None = None
        self._torch: Any | None = None
        self._load_lock = threading.Lock()

    @property
    def selected_model(self) -> str:
        return str(self.local_path) if self.local_path.exists() else self.model_id

    async def score(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        return await asyncio.to_thread(self._score_sync, query, list(documents))

    def _load(self) -> tuple[Any, Any, Any]:
        if self._model is not None:
            return self._model, self._tokenizer, self._torch
        with self._load_lock:
            if self._model is None:
                try:
                    import torch
                    from transformers import AutoModelForCausalLM, AutoTokenizer
                except ImportError as error:
                    raise RuntimeError(
                        "Local reranking requires the optional torch and transformers dependencies"
                    ) from error
                common: dict[str, object] = {}
                if self.hf_token:
                    common["token"] = self.hf_token
                tokenizer = AutoTokenizer.from_pretrained(
                    self.selected_model, padding_side="left", **common
                )
                target = self.device or (
                    "cuda"
                    if torch.cuda.is_available()
                    else "mps"
                    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
                    else "cpu"
                )
                # `torch_dtype="auto"` берёт тип из config.json модели, а он
                # рассчитан на GPU. На процессоре это либо вдвое лишней памяти,
                # либо неподдерживаемый float16 — выбираем сами, по железу.
                model = AutoModelForCausalLM.from_pretrained(
                    self.selected_model, torch_dtype=preferred_dtype(target), **common
                )
                model = model.to(target).eval()
                self._tokenizer, self._model, self._torch = tokenizer, model, torch
        return self._model, self._tokenizer, self._torch

    def _score_sync(self, query: str, documents: list[str]) -> list[float]:
        model, tokenizer, torch = self._load()
        no_id = tokenizer.convert_tokens_to_ids("no")
        yes_id = tokenizer.convert_tokens_to_ids("yes")
        prefix = tokenizer.encode(_SYSTEM, add_special_tokens=False)
        suffix = tokenizer.encode(_SUFFIX, add_special_tokens=False)
        scores: list[float] = []
        for start in range(0, len(documents), self.batch_size):
            prompts = [
                f"<Instruct>: {self.instruction}\n<Query>: {query}\n<Document>: {document}"
                for document in documents[start : start + self.batch_size]
            ]
            tokenized = tokenizer(
                prompts,
                truncation=True,
                max_length=self.max_length - len(prefix) - len(suffix),
                add_special_tokens=False,
                padding=False,
                return_attention_mask=False,
            )
            tokenized["input_ids"] = [
                prefix + input_ids + suffix for input_ids in tokenized["input_ids"]
            ]
            inputs = tokenizer.pad(
                tokenized, padding=True, max_length=self.max_length, return_tensors="pt"
            )
            inputs = {key: value.to(model.device) for key, value in inputs.items()}
            with torch.no_grad():
                logits = model(**inputs).logits[:, -1, [no_id, yes_id]]
                batch_scores = torch.softmax(logits, dim=-1)[:, 1].float().cpu().tolist()
            scores.extend(float(value) for value in batch_scores)
        return scores


class HTTPReranker(AsyncHTTPProvider):
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        model: str,
        batch_size: int = 16,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.url = url
        self.api_key = api_key
        self.model = model
        self.batch_size = batch_size

    async def score(self, query: str, documents: Sequence[str]) -> list[float]:
        scores: list[float] = []
        for start in range(0, len(documents), self.batch_size):
            batch = list(documents[start : start + self.batch_size])
            response = await self._request(
                "POST",
                self.url,
                json={
                    "model": self.model,
                    "query": query,
                    "documents": batch,
                    "top_n": len(batch),
                },
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            scores.extend(_parse_scores(response.json(), len(batch)))
        return scores


def _parse_scores(payload: object, expected: int) -> list[float]:
    if isinstance(payload, list) and all(isinstance(value, int | float) for value in payload):
        scores = [float(value) for value in payload]
    else:
        body = as_dict(payload)
        direct = body.get("scores")
        if isinstance(direct, list):
            scores = []
            for value in direct:
                candidate = (
                    as_dict(value).get("relevance_score", as_dict(value).get("score"))
                    if isinstance(value, dict)
                    else value
                )
                if isinstance(candidate, int | float):
                    scores.append(float(candidate))
        else:
            rows = as_list(body.get("results") or body.get("data") or body.get("rerank"))
            indexed = [0.0] * expected
            scores = []
            for fallback_index, value in enumerate(rows):
                row = as_dict(value)
                score = row.get("relevance_score", row.get("score"))
                if not isinstance(score, int | float):
                    continue
                index = row.get("index", fallback_index)
                if isinstance(index, int) and 0 <= index < expected:
                    indexed[index] = float(score)
            if rows:
                scores = indexed
    if len(scores) != expected:
        raise ValueError(f"Rerank API returned {len(scores)} scores for {expected} documents")
    return scores
