"""Единая типизированная конфигурация приложения из переменных окружения."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ProviderMode(StrEnum):
    LOCAL = "local"
    API = "api"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Настройки считываются только здесь; остальные модули получают Settings явно."""

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "GeoNexa Proxima"
    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    timezone: str = "Europe/Moscow"

    database_url: str = "postgresql+asyncpg://geonexa:change-me@localhost:5432/geonexa"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = "geonexa_items"
    qdrant_profile_collection: str = "geonexa_profiles"

    embedding_mode: ProviderMode = ProviderMode.API
    embedding_local_path: Path = Path("models/Qwen3-Embedding-4B")
    embedding_model: str = "Qwen/Qwen3-Embedding-4B"
    embedding_api_base_url: str = "http://localhost:8001/v1"
    embedding_api_key: SecretStr = SecretStr("test-key")
    embedding_dimensions: int = 2560
    embedding_batch_size: int = Field(default=16, ge=1, le=256)

    reranker_mode: ProviderMode = ProviderMode.API
    reranker_local_path: Path = Path("models/Qwen3-Reranker-0.6B")
    reranker_model: str = "Qwen/Qwen3-Reranker-0.6B"
    reranker_api_url: str = "http://localhost:8002/rerank"
    reranker_api_key: SecretStr = SecretStr("test-key")
    reranker_batch_size: int = Field(default=16, ge=1, le=128)

    light_llm_base_url: str = "https://api.openai.com/v1"
    light_llm_api_key: SecretStr = SecretStr("test-key")
    light_llm_model: str = "gpt-4.1-mini"
    heavy_llm_base_url: str = "https://api.openai.com/v1"
    heavy_llm_api_key: SecretStr = SecretStr("test-key")
    heavy_llm_model: str = "gpt-5"
    llm_timeout_seconds: float = Field(default=120, gt=0)

    telegram_bot_token: SecretStr = SecretStr("test-token")
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_webhook_url: str | None = None
    telegram_webhook_secret: SecretStr | None = None

    openalex_email: str | None = None
    crossref_email: str | None = None
    semantic_scholar_api_key: SecretStr | None = None
    github_token: SecretStr | None = None
    hf_token: SecretStr | None = None

    taxonomy_path: Path = Path("config/taxonomy.yaml")
    semantic_threshold: float = Field(default=0.45, ge=-1, le=1)
    digest_score_threshold: float = Field(default=6.5, ge=0, le=10)
    deep_analysis_threshold: float = Field(default=8.0, ge=0, le=10)
    alert_score_threshold: float = Field(default=9.0, ge=0, le=10)
    collection_lookback_hours: int = Field(default=30, ge=1, le=720)
    max_items_per_source: int = Field(default=200, ge=1, le=1000)
    personalization_candidate_limit: int = Field(default=100, ge=10, le=1000)
    personal_semantic_weight: float = Field(default=0.40, ge=0, le=1)
    personal_reranker_weight: float = Field(default=0.25, ge=0, le=1)
    personal_global_weight: float = Field(default=0.25, ge=0, le=1)
    personal_interest_weight: float = Field(default=0.10, ge=0, le=1)

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def parse_user_ids(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                return []
            return [int(item.strip()) for item in value.split(",")]
        return value

    @model_validator(mode="after")
    def validate_personalization_weights(self) -> Settings:
        total = (
            self.personal_semantic_weight
            + self.personal_reranker_weight
            + self.personal_global_weight
            + self.personal_interest_weight
        )
        if abs(total - 1.0) > 1e-9:
            raise ValueError("personalization weights must sum to 1.0")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Вернуть единственный неизменяемый источник runtime-конфигурации."""

    return Settings()
