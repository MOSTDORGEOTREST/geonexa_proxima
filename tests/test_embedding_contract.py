"""Контракт эмбеддера: асимметрия запрос/документ, Matryoshka, нормализация."""

from __future__ import annotations

import math

import httpx
import pytest
import respx

from geonexa_proxima.ml.embeddings import OpenAICompatibleEmbedder, _truncate_and_normalize
from geonexa_proxima.ml.instructions import format_document, format_query, query_prompt_prefix

INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"


def test_query_format_matches_model_card() -> None:
    """Формат обязан совпадать с карточкой модели байт в байт, без пробела."""

    assert (
        format_query("What is the capital of China?", INSTRUCTION)
        == f"Instruct: {INSTRUCTION}\nQuery:What is the capital of China?"
    )


def test_document_never_gets_an_instruction() -> None:
    assert format_document("Beijing is the capital of China.") == "Beijing is the capital of China."


def test_prefix_and_text_reassemble_the_query() -> None:
    """Локальный путь клеит префикс сам — он должен давать ту же строку."""

    text = "soil liquefaction"
    assert query_prompt_prefix(INSTRUCTION) + text == format_query(text, INSTRUCTION)


def test_no_instruction_leaves_query_untouched() -> None:
    assert format_query("plain", None) == "plain"
    assert format_query("plain", "   ") == "plain"


def test_truncation_renormalizes_to_unit_length() -> None:
    vector = [3.0, 4.0, 12.0, 84.0]
    result = _truncate_and_normalize(vector, 2)
    assert len(result) == 2
    assert math.isclose(math.sqrt(sum(v * v for v in result)), 1.0, rel_tol=1e-9)


def test_truncation_rejects_short_vectors() -> None:
    with pytest.raises(ValueError, match="fewer than configured"):
        _truncate_and_normalize([1.0, 0.0], 4)


def test_truncation_rejects_zero_vector() -> None:
    with pytest.raises(ValueError, match="zero or non-finite"):
        _truncate_and_normalize([0.0, 0.0, 0.0], 3)


def test_dimensions_above_native_are_rejected() -> None:
    with pytest.raises(ValueError, match="только вниз"):
        OpenAICompatibleEmbedder(
            base_url="http://embed/v1",
            api_key="k",
            model="Qwen/Qwen3-Embedding-0.6B",
            dimensions=2560,
            native_dimensions=1024,
        )


@respx.mock
async def test_api_embedder_instructs_queries_but_not_documents() -> None:
    """Главная ловушка: инструкция уходит только с запросом."""

    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen.append(body["input"])
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [1.0, 0.0, 0.0, 0.0]}
                    for i in range(len(body["input"]))
                ]
            },
        )

    respx.post("http://embed/v1/embeddings").mock(side_effect=handler)
    embedder = OpenAICompatibleEmbedder(
        base_url="http://embed/v1",
        api_key="k",
        model="m",
        dimensions=4,
        query_instruction=INSTRUCTION,
    )
    await embedder.embed_documents(["первый документ", "второй документ"])
    await embedder.embed_query("разжижение грунтов")
    await embedder.aclose()

    documents, queries = seen[0], seen[1]
    assert documents == ["первый документ", "второй документ"]
    assert not any(text.startswith("Instruct:") for text in documents)
    assert queries == [f"Instruct: {INSTRUCTION}\nQuery:разжижение грунтов"]


@respx.mock
async def test_api_embedder_truncates_and_normalizes() -> None:
    respx.post("http://embed/v1/embeddings").mock(
        return_value=httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [3.0, 4.0, 99.0, 99.0]}]}
        )
    )
    embedder = OpenAICompatibleEmbedder(
        base_url="http://embed/v1", api_key="k", model="m", dimensions=2
    )
    vector = await embedder.embed_query("x")
    await embedder.aclose()
    assert len(vector) == 2
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)
    assert math.isclose(vector[0], 0.6, rel_tol=1e-9)
