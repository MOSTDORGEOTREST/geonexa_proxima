"""Сборка эмбеддера и реранкера по настройкам."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from geonexa_proxima.config import ProviderMode, Settings
from geonexa_proxima.ml.embeddings import LocalQwen3Embedder, OpenAICompatibleEmbedder
from geonexa_proxima.ml.local_models import require_local_model
from geonexa_proxima.ml.rerankers import HTTPReranker, LocalQwen3Reranker
from geonexa_proxima.ports import Embedder, Reranker

log = logging.getLogger(__name__)

#: Внутри контейнера loopback указывает на сам контейнер, а не на машину.
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _warn_if_loopback(url: str | None, *, what: str) -> None:
    """Предупредить о недостижимом адресе до первого запроса.

    В API-режиме адрес вида ``http://localhost:8001/v1`` работает, только если
    сервис моделей поднят в том же процессном пространстве. В Docker это почти
    всегда опечатка: loopback внутри контейнера — сам контейнер, там ничего не
    слушает, и сбор падает на первом же материале с «connection refused».
    Ошибкой не считаем: локальный vLLM на этом адресе — законный сценарий.
    """

    host = urlparse(str(url or "")).hostname
    if host in _LOOPBACK:
        log.warning(
            "%s работает в режиме api и смотрит на %s. Внутри контейнера это "
            "адрес самого контейнера: если сервис моделей поднят не рядом, "
            "запросы будут отвергнуты. Проверь адрес или переключись на local.",
            what,
            url,
        )


def _require_ml_stack(what: str) -> None:
    """Убедиться, что локальный режим вообще исполним в этом образе.

    Веса лежат на смонтированном томе, поэтому «файлы на месте» ничего не
    говорит о наличии torch: образ, собранный без `INSTALL_ML=true`, проходил
    проверку готовности и падал только внутри ночного сбора — через сутки
    после выкатки и без единой связи с ней.
    """

    try:
        import sentence_transformers  # noqa: F401
    except ImportError as error:
        raise RuntimeError(
            f"{what} в режиме local, но в образе нет ML-зависимостей. "
            "Пересоберите образ с INSTALL_ML=true либо переключите режим на api."
        ) from error


def create_embedder(settings: Settings) -> Embedder:
    if settings.embedding_mode == ProviderMode.LOCAL:
        _require_ml_stack("Эмбеддер")
        model = require_local_model(
            settings.embedding_local_path,
            settings.models_root,
            kind="embedding",
            expected_dimensions=settings.embedding_dimensions,
        )
        return LocalQwen3Embedder(
            local_path=model.path,
            model_id=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            batch_size=settings.embedding_batch_size,
            hf_token=settings.hf_token.get_secret_value() if settings.hf_token else None,
            query_instruction=settings.query_instruction(),
            native_dimensions=settings.native_embedding_dimensions,
        )
    _warn_if_loopback(settings.embedding_api_base_url, what="Эмбеддер")
    return OpenAICompatibleEmbedder(
        base_url=settings.embedding_api_base_url,
        api_key=settings.embedding_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        batch_size=settings.embedding_batch_size,
        query_instruction=settings.query_instruction(),
        native_dimensions=settings.native_embedding_dimensions,
    )


def create_reranker(settings: Settings) -> Reranker:
    if settings.reranker_mode == ProviderMode.LOCAL:
        _require_ml_stack("Реранкер")
        model = require_local_model(
            settings.reranker_local_path,
            settings.models_root,
            kind="reranker",
        )
        return LocalQwen3Reranker(
            local_path=model.path,
            model_id=settings.reranker_model,
            batch_size=settings.reranker_batch_size,
            hf_token=settings.hf_token.get_secret_value() if settings.hf_token else None,
            instruction=settings.reranker_instruction,
            max_length=settings.reranker_max_length,
        )
    _warn_if_loopback(settings.reranker_api_url, what="Реранкер")
    return HTTPReranker(
        url=settings.reranker_api_url,
        api_key=settings.reranker_api_key.get_secret_value(),
        model=settings.reranker_model,
        batch_size=settings.reranker_batch_size,
    )
