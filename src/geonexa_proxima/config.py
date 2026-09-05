"""Единая типизированная конфигурация приложения из переменных окружения."""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, get_args
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from geonexa_proxima.tls import SSLMode


class ProviderMode(StrEnum):
    LOCAL = "local"
    API = "api"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class VectorBackend(StrEnum):
    PGVECTOR = "pgvector"
    QDRANT = "qdrant"


class VectorColumnType(StrEnum):
    """Тип колонки pgvector. halfvec появился в pgvector 0.7."""

    VECTOR = "vector"
    HALFVEC = "halfvec"


class VectorIndexKind(StrEnum):
    HNSW = "hnsw"
    IVFFLAT = "ivfflat"
    NONE = "none"


class RegistrationMode(StrEnum):
    OPEN = "open"
    ALLOWLIST = "allowlist"
    INVITE = "invite"


class ReasoningEffort(StrEnum):
    NONE = "none"
    LOW = "low"
    HIGH = "high"
    MAX = "max"


# Нативные размерности семейства Qwen3-Embedding. Matryoshka режет только вниз:
# запросить у модели больше её скрытого размера невозможно.
NATIVE_EMBEDDING_DIMENSIONS: dict[str, int] = {
    "qwen3-embedding-0.6b": 1024,
    "qwen3-embedding-4b": 2560,
    "qwen3-embedding-8b": 4096,
}

# Потолки индексов pgvector: HNSW и IVFFlat на vector — 2000 измерений,
# HNSW на halfvec — 4000. Сам тип vector хранит до 16000, но без индекса.
PGVECTOR_INDEX_LIMITS: dict[tuple[VectorColumnType, VectorIndexKind], int] = {
    (VectorColumnType.VECTOR, VectorIndexKind.HNSW): 2000,
    (VectorColumnType.VECTOR, VectorIndexKind.IVFFLAT): 2000,
    (VectorColumnType.HALFVEC, VectorIndexKind.HNSW): 4000,
    (VectorColumnType.HALFVEC, VectorIndexKind.IVFFLAT): 4000,
}

DEFAULT_QUERY_INSTRUCTION = (
    "Given a research profile in geotechnics, engineering geology and machine "
    "learning, retrieve scientific papers, methods, software and datasets that "
    "match this profile"
)


def _native_dimensions(model: str) -> int | None:
    """Определить нативную размерность по идентификатору модели."""

    name = model.strip().lower().rsplit("/", 1)[-1]
    return NATIVE_EMBEDDING_DIMENSIONS.get(name)


#: Куски, по которым узнаётся незаполненный секрет из шаблона.
_PLACEHOLDERS = ("change-me", "changeme", "generate-with", "replace-me", "your-", "example")


def _looks_like_placeholder(value: str) -> bool:
    """Похоже ли значение на подсказку из `.env.example`, а не на секрет."""

    lowered = value.strip().casefold()
    return not lowered or any(marker in lowered for marker in _PLACEHOLDERS)


def zone_of(timezone: str) -> ZoneInfo:
    """Пояс платформы по имени.

    Живёт рядом с настройкой `TIMEZONE`, а не в модуле сбора: сбор считает
    сутки в UTC (даты у источников — UTC), а пояс нужен там, где время видит
    человек, — во времени недельной рассылки.

    Неизвестный пояс — не повод останавливать платформу: считаем в UTC.
    """

    try:
        return ZoneInfo(timezone)
    except Exception:
        return ZoneInfo("UTC")


class Settings(BaseSettings):
    """Настройки считываются только здесь; остальные модули получают Settings явно."""

    # GEONEXA_ENV_FILE переопределяет файл настроек. Нужен там, где ".env"
    # рядом с рабочим каталогом — не тот файл: тесты, отдельный стенд,
    # разбор чужого окружения.
    model_config = SettingsConfigDict(
        env_file=(os.getenv("GEONEXA_ENV_FILE") or ".env",),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- приложение ---------------------------------------------------------
    app_name: str = "GeoNexa Proxima"
    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    # В контейнере логи собирает не человек, а сборщик: JSON по умолчанию
    # включается для production в валидаторе ниже.
    log_json: bool | None = None
    timezone: str = "Europe/Moscow"
    public_base_url: str = "http://localhost:8000"

    # --- база данных --------------------------------------------------------
    database_url: str = "postgresql+asyncpg://geonexa:change-me@localhost:5432/geonexa"
    database_ssl_mode: SSLMode = SSLMode.PREFER
    database_ssl_root_cert: Path | None = None
    database_statement_timeout_ms: int = Field(default=30_000, ge=0)

    # Пул под управляемую БД: у неё жёсткий max_connections, общий на все
    # процессы платформы. Значения намеренно маленькие — ждать соединение
    # пятнадцать секунд лучше, чем получить отказ сервера.
    db_pool_size: int = Field(default=2, ge=1, le=50)
    db_max_overflow: int = Field(default=0, ge=0, le=50)
    db_connection_budget: int = Field(default=2, ge=1, le=100)
    db_pool_timeout: int = Field(default=15, ge=1, le=300)
    db_pool_recycle: int = Field(default=1800, ge=60, le=86_400)
    db_connect_timeout: float = Field(default=10, gt=0, le=120)
    db_command_timeout: float = Field(default=30, gt=0, le=600)
    db_application_name: str = "geonexa-proxima"
    # Подъём на чистой базе. В dev включено, в production миграции обычно
    # катят отдельным шагом деплоя — тогда DB_AUTO_MIGRATE=false и сервис
    # просто откажется стартовать против несовпадающей схемы.
    db_auto_migrate: bool = True
    db_auto_seed: bool = True
    db_wait_attempts: int = Field(default=30, ge=1, le=300)
    db_wait_delay_seconds: float = Field(default=2.0, gt=0, le=60)

    # --- векторное хранилище ------------------------------------------------
    vector_backend: VectorBackend = VectorBackend.PGVECTOR
    vector_column_type: VectorColumnType = VectorColumnType.VECTOR
    vector_index_kind: VectorIndexKind = VectorIndexKind.HNSW
    vector_hnsw_m: int = Field(default=16, ge=2, le=100)
    vector_hnsw_ef_construction: int = Field(default=64, ge=4, le=1000)
    # Не меньше самой широкой выборки: HNSW возвращает не больше ef_search
    # соседей, и остаток LIMIT теряется молча. Сверяется валидатором.
    vector_hnsw_ef_search: int = Field(default=120, ge=1, le=1000)
    vector_ivfflat_lists: int = Field(default=100, ge=1, le=32_768)
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = "geonexa_items"
    qdrant_profile_collection: str = "geonexa_profiles"

    # --- Prefect ------------------------------------------------------------
    prefect_api_url: str = "http://localhost:4200/api"
    prefect_api_key: SecretStr | None = None
    prefect_work_pool: str = "geonexa-pool"
    prefect_work_queue: str = "default"
    prefect_api_database_connection_url: str | None = None
    # Кроны считаются в поясе платформы (`TIMEZONE`), а не в UTC: строка
    # расписания уезжает в Prefect вместе с ним.
    #: Каждую ночь за вчерашние сутки.
    schedule_global_harvest_cron: str = "0 1 * * *"
    #: Личные чаты работают только с ручного запуска, поэтому строка заводится
    #: выключенной — крон здесь на случай, когда её включат в админке.
    schedule_digest_dispatch_cron: str = "0 7 * * 1"
    #: Дайджест групп и каналов собирается в понедельник в полночь…
    schedule_digest_dispatch_chats_cron: str = "0 0 * * 1"
    schedule_delivery_personal_cron: str = "*/5 * * * *"
    #: Воркеры рассылки крутятся часто и просто развозят то, что уже можно
    #: слать. Время недельной рассылки задаёт не крон воркера, а `scheduled_at`
    #: задания (`deliver_at_hour` у диспетчера чатов): иначе повторная попытка
    #: после сбоя ждала бы следующего понедельника и протухала по TTL.
    schedule_delivery_group_cron: str = "*/5 * * * *"
    schedule_chat_monitor_cron: str = "0 */6 * * *"
    schedule_maintenance_cron: str = "30 4 * * *"
    schedule_subscription_maintenance_cron: str = "0 5 * * *"

    # --- админка ------------------------------------------------------------
    #: Верить ли заголовку `X-Forwarded-For`. Включать только когда перед
    #: сервисом действительно стоит доверенный прокси и он этот заголовок
    #: перезаписывает: иначе адрес в аудите и ключ ограничения попыток входа
    #: подделываются одной строкой.
    trust_proxy_headers: bool = False

    admin_username: str = "admin"
    admin_password: SecretStr | None = None
    admin_password_hash: str | None = None
    admin_jwt_secret: SecretStr = SecretStr("change-me-in-production")
    admin_jwt_ttl_minutes: int = Field(default=720, ge=5, le=10_080)
    admin_refresh_ttl_days: int = Field(default=14, ge=1, le=365)
    admin_cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    admin_login_rate_limit_per_minute: int = Field(default=5, ge=1, le=100)
    secret_encryption_key: SecretStr | None = None

    # --- LLM ----------------------------------------------------------------
    default_llm_provider_key: str = "deepseek"
    default_llm_base_url: str = "https://api.deepseek.com/v1"
    default_llm_api_key: SecretStr = SecretStr("test-key")

    light_llm_base_url: str = "https://api.deepseek.com/v1"
    light_llm_api_key: SecretStr = SecretStr("test-key")
    light_llm_model: str = "deepseek-v4-flash"
    light_llm_reasoning_effort: ReasoningEffort = ReasoningEffort.LOW
    light_llm_temperature: float = Field(default=0.1, ge=0, le=2)
    light_llm_max_tokens: int = Field(default=2048, ge=1)
    light_llm_json_mode: bool = True
    light_llm_concurrency: int = Field(default=8, ge=1, le=64)

    heavy_llm_base_url: str = "https://api.deepseek.com/v1"
    heavy_llm_api_key: SecretStr = SecretStr("test-key")
    heavy_llm_model: str = "deepseek-v4-flash"
    heavy_llm_reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH
    heavy_llm_temperature: float = Field(default=0.2, ge=0, le=2)
    heavy_llm_max_tokens: int = Field(default=8192, ge=1)
    heavy_llm_json_mode: bool = True
    heavy_llm_concurrency: int = Field(default=2, ge=1, le=64)

    llm_timeout_seconds: float = Field(default=180, gt=0)
    llm_max_retries: int = Field(default=3, ge=0, le=10)
    llm_log_calls: bool = True
    llm_daily_token_budget: int = Field(default=0, ge=0)

    # --- embeddings ---------------------------------------------------------
    embedding_mode: ProviderMode = ProviderMode.LOCAL
    # Каталог с весами. Точное имя подкаталога не важно: веса ищутся по
    # содержимому, иначе каждая перекачка ломала бы конфигурацию.
    models_root: Path = Path("models")
    embedding_local_path: Path = Path("models/Qwen3-Embedding-0.6B")
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_api_base_url: str = "http://localhost:8001/v1"
    embedding_api_key: SecretStr = SecretStr("test-key")
    embedding_dimensions: int = Field(default=1024, ge=32, le=16_000)
    embedding_batch_size: int = Field(default=16, ge=1, le=256)
    # Запросы подаются с инструкцией, документы — без неё. Это требование
    # Qwen3-Embedding, а не украшение: асимметрия заложена в обучение.
    embedding_query_instruction: str = DEFAULT_QUERY_INSTRUCTION
    embedding_instruction_enabled: bool = True

    # --- reranker -----------------------------------------------------------
    reranker_mode: ProviderMode = ProviderMode.LOCAL
    reranker_local_path: Path = Path("models/Qwen3-Reranker-0.6B")
    reranker_model: str = "Qwen/Qwen3-Reranker-0.6B"
    reranker_api_url: str = "http://localhost:8002/rerank"
    reranker_api_key: SecretStr = SecretStr("test-key")
    reranker_batch_size: int = Field(default=16, ge=1, le=128)
    reranker_instruction: str = DEFAULT_QUERY_INSTRUCTION
    reranker_max_length: int = Field(default=8192, ge=512, le=131_072)

    # --- Telegram -----------------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("test-token")
    telegram_owner_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_registration_mode: RegistrationMode = RegistrationMode.ALLOWLIST
    telegram_allow_group_chats: bool = True
    telegram_auto_register_chats: bool = True
    telegram_webhook_url: str | None = None
    telegram_webhook_secret: SecretStr | None = None
    #: Как бот получает апдейты. `polling` — отдельный контейнер `bot`
    #: опрашивает Telegram; `webhook` — их приносит API, и контейнер `bot`
    #: запускать нельзя. Включать оба канала разом нельзя технически: Telegram
    #: отдаёт апдейты кому-то одному.
    telegram_update_mode: str = Field(default="polling", pattern="^(polling|webhook)$")
    telegram_global_rate_per_second: float = Field(default=25, gt=0, le=30)
    telegram_chat_rate_per_second: float = Field(default=1, gt=0, le=30)
    telegram_group_rate_per_minute: float = Field(default=18, gt=0, le=20)

    # --- доставка -----------------------------------------------------------
    delivery_batch_size: int = Field(default=50, ge=1, le=1000)
    delivery_max_attempts: int = Field(default=5, ge=1, le=20)
    delivery_retry_backoff_seconds: int = Field(default=60, ge=1, le=86_400)
    delivery_job_ttl_hours: int = Field(default=72, ge=1, le=720)
    #: Через сколько минут забранное задание считать брошенным и вернуть в
    #: очередь. Должно быть заведомо больше времени отправки одной пачки:
    #: пятьдесят постов по три части при паузе 3,3 с — почти девять минут.
    delivery_stale_minutes: int = Field(default=45, ge=5, le=720)
    delivery_dry_run: bool = False

    # --- harvest ------------------------------------------------------------
    harvest_profile_key: str = "geo_ai_core"
    harvest_config_path: Path = Path("config/harvest.yaml")
    taxonomy_path: Path = Path("config/taxonomy.yaml")
    #: Запасной режим: одно открытое окно вместо нарезки по суткам. Плановый
    #: сбор им не пользуется — он ходит сутками.
    collection_lookback_hours: int = Field(default=192, ge=1, le=8760)
    #: Сколько пропущенных суток добирает один плановый прогон. Глубже —
    #: только руками с указанием дат: сотня запросов подряд после долгого
    #: простоя приносит бан, а не материалы.
    harvest_max_catchup_days: int = Field(default=7, ge=1, le=400)
    max_items_per_source: int = Field(default=1000, ge=1, le=5000)
    harvest_keyword_threshold: float = Field(default=0.0, ge=0, le=1)
    harvest_store_rejected: bool = True
    harvest_decision_retention_days: int = Field(default=90, ge=1, le=3650)
    #: Через сколько минут «выполняющийся» прогон считать оборванным. Частичный
    #: уникальный индекс не даёт запустить второй сбор параллельно, поэтому
    #: запись, которую упавший процесс не закрыл, блокирует сбор до тех пор,
    #: пока её кто-нибудь не подберёт. В минутах, а не в часах: сбор редко идёт
    #: дольше получаса, а каждый час ожидания — это час без новых материалов.
    harvest_run_stale_minutes: int = Field(default=90, ge=5, le=10080)

    # --- пороги пайплайна ---------------------------------------------------
    semantic_threshold: float = Field(default=0.25, ge=-1, le=1)
    digest_score_threshold: float = Field(default=6.5, ge=0, le=10)
    deep_analysis_threshold: float = Field(default=8.0, ge=0, le=10)
    alert_score_threshold: float = Field(default=9.0, ge=0, le=10)
    personalization_candidate_limit: int = Field(default=100, ge=10, le=1000)
    personal_semantic_weight: float = Field(default=0.40, ge=0, le=1)
    personal_reranker_weight: float = Field(default=0.25, ge=0, le=1)
    personal_global_weight: float = Field(default=0.25, ge=0, le=1)
    personal_interest_weight: float = Field(default=0.10, ge=0, le=1)

    # --- грани профиля ------------------------------------------------------
    # Профиль из нескольких тем даёт вектор-центроид между ними, и статья,
    # глубоко попадающая в одну тему, проигрывает статье, слегка похожей на
    # всё сразу. Поэтому ищем ещё и каждой темой отдельно, а близость берём
    # максимумом. `PROFILE_FACET_LIMIT=0` возвращает прежнее поведение с
    # одним вектором на весь профиль.
    profile_facet_limit: int = Field(default=16, ge=0, le=64)
    profile_facet_min_chars: int = Field(default=16, ge=4, le=200)
    # Сколько кандидатов тянуть одной гранью. Меньше, чем полным профилем:
    # граней несколько, и общий объём выборки растёт их числом.
    personalization_facet_candidate_limit: int = Field(default=40, ge=5, le=500)
    # Сколько кандидатов доживает до реранкера. Кросс-энкодер считает пару
    # «запрос-документ» на каждый материал, а граней бывает до тридцати двух:
    # без потолка одна рассылка означала бы тысячи пар на профиль. Отсечение
    # идёт по той же формуле личной оценки, только без вклада реранкера.
    personalization_rerank_limit: int = Field(default=150, ge=10, le=1000)
    # Косинус, с которого материал допускается в дайджест мимо общего научного
    # порога. Это и есть случай «в интерес попал, а в общем не прошёл»: узкая
    # тема почти всегда весит меньше, чем средняя оценка статьи.
    #
    # Именно косинус, как SEMANTIC_THRESHOLD: 0 — ортогонально, 1 — совпадение.
    # Нижняя граница не нулевая намеренно. Отрицательный порог означал бы
    # «пропускать вообще всё», причём молча: материал, не имеющий к профилю
    # никакого отношения, имеет косинус около нуля. Выключается значением 1.
    personal_facet_override_score: float = Field(default=0.60, gt=0, le=1)
    # Мягкая квота: сколько мест в выдаче гарантировано каждой грани, прежде
    # чем остаток разыгрывается по общему баллу. Иначе одна «горячая» тема
    # забирает весь дайджест, и остальные интересы человек просто не видит.
    personal_facet_min_slots: int = Field(default=1, ge=0, le=20)

    # --- подписки -----------------------------------------------------------
    default_subscription_plan: str = "free"
    # Интервал между дайджестами для подписчика без действующего тарифа.
    # Профиль может просить реже, но не чаще: иначе ограничение плана
    # обходилось бы правкой собственных настроек.
    digest_default_interval_hours: int = Field(default=168, ge=1, le=8760)
    #: Насколько раньше срока диспетчер готов взять профиль. Срок считается от
    #: момента постановки в очередь, а расписание срабатывает по часам: без
    #: допуска недельный дайджест сползает на неделю вперёд после первого же
    #: прогона, занявшего пару минут.
    digest_due_grace_minutes: int = Field(default=90, ge=0, le=1440)
    default_trial_days: int = Field(default=14, ge=0, le=365)
    subscription_grace_days: int = Field(default=3, ge=0, le=90)

    # --- источники ----------------------------------------------------------
    openalex_email: str | None = None
    crossref_email: str | None = None
    semantic_scholar_api_key: SecretStr | None = None
    github_token: SecretStr | None = None
    hf_token: SecretStr | None = None

    # --- метрики ------------------------------------------------------------
    metrics_enabled: bool = True
    metrics_timezone: str = "Europe/Moscow"
    metrics_rollup_cron: str = "15 * * * *"
    metrics_retention_days: int = Field(default=730, ge=1, le=3650)
    metrics_active_window_days: int = Field(default=30, ge=1, le=365)
    metrics_cohort_weeks: int = Field(default=12, ge=1, le=104)
    metrics_rollup_lookback_days: int = Field(default=3, ge=1, le=90)
    prometheus_enabled: bool = True
    prometheus_path: str = "/metrics"

    # ------------------------------------------------------------------ #
    # Валидаторы                                                          #
    # ------------------------------------------------------------------ #

    def llm_api_key(self, *, heavy: bool) -> str:
        """Ключ роли, а если он не задан — общий DEFAULT_LLM_API_KEY.

        Обе роли обычно живут у одного провайдера, и держать один ключ в трёх
        переменных — способ однажды обновить две из трёх.
        """

        specific = (self.heavy_llm_api_key if heavy else self.light_llm_api_key).get_secret_value()
        placeholder = {"", "test-key", "replace-me"}
        if specific and specific not in placeholder:
            return specific
        return self.default_llm_api_key.get_secret_value()

    def webhook_endpoint(self) -> str | None:
        """Полный URL вебхука: явный TELEGRAM_WEBHOOK_URL либо PUBLIC_BASE_URL.

        ``None`` в режиме polling: два канала доставки апдейтов одновременно
        включить нельзя. Раньше адрес возвращался, как только `PUBLIC_BASE_URL`
        переставал быть локальным, — и в проде, где рядом работает контейнер
        бота, Telegram переставал отдавать апдейты через `getUpdates`, а бот
        замолкал целиком.
        """

        if self.telegram_update_mode != "webhook":
            return None
        if self.telegram_webhook_url:
            return self.telegram_webhook_url.rstrip("/")
        base = (self.public_base_url or "").rstrip("/")
        if not base or base.startswith("http://localhost") or base.startswith("http://127."):
            # Telegram не сможет достучаться до localhost — молчаливая регистрация
            # такого вебхука выглядела бы как рабочая настройка.
            return None
        return f"{base}/telegram/webhook"

    @field_validator("telegram_allowed_user_ids", "telegram_owner_ids", mode="before")
    @classmethod
    def parse_user_ids(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                return []
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        return value

    @field_validator("admin_cors_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("database_ssl_root_cert", mode="before")
    @classmethod
    def expand_cert_path(cls, value: object) -> object:
        """Развернуть ~ в пути до корневого сертификата."""

        if isinstance(value, str):
            stripped = value.strip()
            return Path(stripped).expanduser() if stripped else None
        return value

    @model_validator(mode="before")
    @classmethod
    def treat_blank_as_unset(cls, values: object) -> object:
        """Пустое значение в .env означает «не задано», а не пустую строку.

        `LOG_JSON=` — естественный способ записать «по умолчанию», и падать на
        нём нельзя. Правило применяется только к полям, которые вообще
        допускают None: у обязательных пустое значение по-прежнему ошибка.
        """

        if not isinstance(values, dict):
            return values
        cleaned = dict(values)
        for name, field in cls.model_fields.items():
            for key in (name, name.upper()):
                value = cleaned.get(key)
                if isinstance(value, str) and not value.strip() and _optional(field):
                    cleaned[key] = None
        return cleaned

    @model_validator(mode="after")
    def validate_database_tls(self) -> Settings:
        """verify-ca и verify-full без корневого сертификата — молчаливая фикция."""

        if self.database_ssl_mode in (SSLMode.VERIFY_CA, SSLMode.VERIFY_FULL):
            if self.database_ssl_root_cert is None:
                raise ValueError(
                    f"DATABASE_SSL_MODE={self.database_ssl_mode} требует DATABASE_SSL_ROOT_CERT"
                )
            if not self.database_ssl_root_cert.is_file():
                raise ValueError(f"Корневой сертификат не найден: {self.database_ssl_root_cert}")
        if "sslmode=" in self.database_url:
            raise ValueError(
                "asyncpg не понимает sslmode внутри DSN: удали параметр из "
                "DATABASE_URL и задай режим через DATABASE_SSL_MODE"
            )
        return self

    @model_validator(mode="after")
    def validate_connection_budget(self) -> Settings:
        """Пул не должен молча превышать бюджет соединений процесса."""

        requested = self.db_pool_size + self.db_max_overflow
        if requested > self.db_connection_budget:
            raise ValueError(
                f"Пул PostgreSQL превышает DB_CONNECTION_BUDGET: DB_POOL_SIZE "
                f"({self.db_pool_size}) + DB_MAX_OVERFLOW ({self.db_max_overflow}) "
                f"= {requested} > {self.db_connection_budget}"
            )
        return self

    @model_validator(mode="after")
    def validate_embedding_dimensions(self) -> Settings:
        """Размерность не может превышать нативную: Matryoshka режет только вниз."""

        native = _native_dimensions(self.embedding_model)
        if native is not None and self.embedding_dimensions > native:
            raise ValueError(
                f"EMBEDDING_DIMENSIONS={self.embedding_dimensions} больше нативной "
                f"размерности модели {self.embedding_model} ({native}). Matryoshka "
                f"обрезает вектор, но не удлиняет его: возьми модель крупнее или "
                f"уменьши размерность."
            )
        return self

    @model_validator(mode="after")
    def validate_vector_index(self) -> Settings:
        """Размерность должна помещаться в выбранный индекс pgvector."""

        if self.vector_backend is not VectorBackend.PGVECTOR:
            return self
        if self.vector_index_kind is VectorIndexKind.NONE:
            return self
        limit = PGVECTOR_INDEX_LIMITS[(self.vector_column_type, self.vector_index_kind)]
        if self.embedding_dimensions > limit:
            hint = (
                "перейди на VECTOR_COLUMN_TYPE=halfvec (потолок 4000, нужен pgvector 0.7+)"
                if self.vector_column_type is VectorColumnType.VECTOR
                else "обрежь вектор Matryoshka до 4000 или ниже"
            )
            raise ValueError(
                f"EMBEDDING_DIMENSIONS={self.embedding_dimensions} не помещается в индекс "
                f"{self.vector_index_kind} на колонке {self.vector_column_type} "
                f"(потолок {limit}): {hint}"
            )
        return self

    @model_validator(mode="after")
    def validate_admin_credentials(self) -> Settings:
        """В production пароль админки должен быть хешем, а не открытым текстом."""

        if not self.admin_password and not self.admin_password_hash:
            raise ValueError("Нужен ADMIN_PASSWORD или ADMIN_PASSWORD_HASH")
        if self.environment is Environment.PRODUCTION:
            if not self.admin_password_hash:
                raise ValueError("В production задай ADMIN_PASSWORD_HASH вместо ADMIN_PASSWORD")
            secret = self.admin_jwt_secret.get_secret_value()
            # Сравнения с одним умолчанием мало: развёртывание по инструкции —
            # это `cp .env.example .env`, и подсказка из шаблона проходила
            # проверку так же гладко, как настоящий секрет. Секретом подписи
            # токенов админки в этом случае становится строка из репозитория.
            if _looks_like_placeholder(secret):
                raise ValueError(
                    "В production задай собственный ADMIN_JWT_SECRET: "
                    "значение из шаблона не годится (openssl rand -base64 48)"
                )
        return self

    @model_validator(mode="after")
    def validate_webhook_secret(self) -> Settings:
        """В режиме вебхука секрет обязателен: без него ручку дёрнет кто угодно.

        Проверка секрета в обработчике пропускается, если он не задан, — то
        есть пустое значение превращает `/telegram/webhook` в открытый вход:
        поддельный апдейт от имени владельца проходит политику доступа и
        выполняет любую команду.
        """

        if self.telegram_update_mode != "webhook":
            return self
        secret = (
            self.telegram_webhook_secret.get_secret_value() if self.telegram_webhook_secret else ""
        )
        if not secret or _looks_like_placeholder(secret):
            raise ValueError(
                "TELEGRAM_UPDATE_MODE=webhook требует собственный "
                "TELEGRAM_WEBHOOK_SECRET: без него вебхук принимает запросы от кого угодно"
            )
        return self

    @model_validator(mode="after")
    def validate_search_breadth(self) -> Settings:
        """`ef_search` не должен быть меньше запрашиваемого числа соседей.

        HNSW возвращает не больше, чем размер списка кандидатов обхода: при
        `ef_search=80` и `LIMIT 100` придёт восемьдесят строк, и двадцать
        процентов выдачи потеряются молча — ни ошибки, ни строки в логе, просто
        поиск станет хуже. Проверка ловит это на старте, а не по жалобе на
        «дайджест стал скучнее».
        """

        if self.vector_index_kind is not VectorIndexKind.HNSW:
            return self
        widest = max(
            self.personalization_candidate_limit,
            self.personalization_facet_candidate_limit,
        )
        if self.vector_hnsw_ef_search < widest:
            raise ValueError(
                f"VECTOR_HNSW_EF_SEARCH={self.vector_hnsw_ef_search} меньше, чем "
                f"самая широкая выборка ({widest}): HNSW вернёт не больше "
                f"ef_search соседей, и остаток выдачи потеряется без ошибки. "
                f"Подними ef_search до {widest} или опусти лимиты выборки."
            )
        return self

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

    # ------------------------------------------------------------------ #
    # Производные значения                                                #
    # ------------------------------------------------------------------ #

    @property
    def database_pool_size(self) -> int:
        """Историческое имя; источник истины — db_pool_size."""

        return self.db_pool_size

    @property
    def database_max_overflow(self) -> int:
        return self.db_max_overflow

    @property
    def database_connect_timeout_seconds(self) -> float:
        return self.db_connect_timeout

    @property
    def native_embedding_dimensions(self) -> int | None:
        """Нативная размерность модели, если она известна."""

        return _native_dimensions(self.embedding_model)

    @property
    def embedding_is_truncated(self) -> bool:
        """Режем ли мы вектор Matryoshka относительно нативной размерности."""

        native = self.native_embedding_dimensions
        return native is not None and self.embedding_dimensions < native

    def query_instruction(self) -> str | None:
        """Инструкция для запросов; для документов всегда None."""

        if not self.embedding_instruction_enabled:
            return None
        return self.embedding_query_instruction.strip() or None


def _optional(field: object) -> bool:
    """Допускает ли поле None. Union вида `X | None` — самый частый случай."""

    annotation = getattr(field, "annotation", None)
    if annotation is None:
        return False
    return type(None) in get_args(annotation)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Вернуть единственный неизменяемый источник runtime-конфигурации."""

    return Settings()
