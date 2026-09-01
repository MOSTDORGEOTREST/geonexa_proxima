import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from geonexa_proxima.config import Environment, ProviderMode, Settings

# Платформа не стартует без учётных данных админки, поэтому каждая проверка
# конфигурации подставляет их явно — иначе валидатор перекрывает тот отказ,
# который тест собирался поймать.
ADMIN = {"admin_password": "test-password", "admin_username": "test-admin"}


def test_settings_parse_environment_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "GeoNexa Test")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("EMBEDDING_MODE", "local")
    monkeypatch.setenv("EMBEDDING_LOCAL_PATH", "models/custom-embedding")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "32")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "101, 202")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$argon2id$dummy")
    monkeypatch.setenv("ADMIN_JWT_SECRET", "test-jwt-secret")

    settings = Settings(_env_file=None)

    assert settings.app_name == "GeoNexa Test"
    assert settings.environment is Environment.PRODUCTION
    assert settings.embedding_mode is ProviderMode.LOCAL
    assert settings.embedding_local_path == Path("models/custom-embedding")
    assert settings.embedding_batch_size == 32
    assert settings.telegram_allowed_user_ids == [101, 202]
    assert settings.telegram_bot_token.get_secret_value() == "dummy-token"


def test_settings_parse_comma_separated_user_ids() -> None:
    assert Settings.parse_user_ids("11, 22,33") == [11, 22, 33]
    assert Settings.parse_user_ids("  ") == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("embedding_batch_size", 0),
        ("reranker_batch_size", 129),
        ("semantic_threshold", 1.1),
        ("digest_score_threshold", -0.1),
    ],
)
def test_settings_reject_invalid_limits(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **ADMIN, **{field: value})


def test_settings_reject_personalization_weights_that_do_not_sum_to_one() -> None:
    with pytest.raises(ValidationError, match=r"must sum to 1\.0"):
        Settings(_env_file=None, **ADMIN, personal_semantic_weight=0.2)


def test_production_refuses_plaintext_admin_password() -> None:
    """В production пароль открытым текстом — отказ на старте, а не в проде."""

    with pytest.raises(ValidationError, match="ADMIN_PASSWORD_HASH"):
        Settings(_env_file=None, environment="production", admin_password="plain")


def test_dimensions_above_native_are_rejected() -> None:
    """Matryoshka режет вниз: 2560 у модели 0.6B невозможны."""

    with pytest.raises(ValidationError, match="Matryoshka"):
        Settings(
            _env_file=None,
            **ADMIN,
            embedding_model="Qwen/Qwen3-Embedding-0.6B",
            embedding_dimensions=2560,
        )


def test_dimensions_beyond_pgvector_index_limit_are_rejected() -> None:
    """2560 не индексируются как hnsw на vector — потолок 2000."""

    with pytest.raises(ValidationError, match="не помещается в индекс"):
        Settings(
            _env_file=None,
            **ADMIN,
            embedding_model="Qwen/Qwen3-Embedding-4B",
            embedding_dimensions=2560,
        )


def test_halfvec_lifts_the_index_ceiling_to_4000() -> None:
    settings = Settings(
        _env_file=None,
        **ADMIN,
        embedding_model="Qwen/Qwen3-Embedding-4B",
        embedding_dimensions=2560,
        vector_column_type="halfvec",
    )
    assert settings.embedding_dimensions == 2560
    assert settings.embedding_is_truncated is False


def test_matryoshka_truncation_is_reported() -> None:
    settings = Settings(
        _env_file=None,
        **ADMIN,
        embedding_model="Qwen/Qwen3-Embedding-4B",
        embedding_dimensions=1536,
    )
    assert settings.native_embedding_dimensions == 2560
    assert settings.embedding_is_truncated is True


def test_sslmode_inside_dsn_is_rejected() -> None:
    with pytest.raises(ValidationError, match="sslmode"):
        Settings(
            _env_file=None,
            **ADMIN,
            database_url="postgresql+asyncpg://u:p@h:5432/d?sslmode=require",
        )


def test_orm_imports_without_configuration() -> None:
    """ORM должен импортироваться, даже когда конфигурация не собирается.

    Раньше `db/models.py` звал `get_settings()` на импорте. Один неверный путь
    к сертификату — и весь persistence-слой становился неимпортируемым, причём
    разработчик видел не «сертификат не найден», а вторичное «Table 'items' is
    already defined»: pytest пробовал импортировать модуль второй раз уже после
    того, как половина таблиц зарегистрировалась в MetaData.

    Проверка идёт в отдельном интерпретаторе: только так видно поведение
    настоящего холодного импорта, а не остатки уже загруженных модулей.
    """

    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(root / "src"),
        # Заведомо сломанная конфигурация: несуществующий .env и путь к CA,
        # которого нет, при режиме, требующем сертификат.
        "GEONEXA_ENV_FILE": str(root / ".env.absent"),
        "DATABASE_SSL_MODE": "verify-full",
        "DATABASE_SSL_ROOT_CERT": "/nope/does-not-exist.crt",
    }
    probe = (
        "from geonexa_proxima.db.models import ItemModel, ItemVectorModel;"
        "from geonexa_proxima.config import get_settings;"
        "print(ItemModel.__tablename__, ItemVectorModel.__tablename__);"
        "\ntry:\n get_settings()\n print('SETTINGS-OK')\n"
        "except Exception as e: print('SETTINGS-FAILED')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(root),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "items item_vectors" in result.stdout
    # Настройки при этом действительно сломаны — иначе тест ничего не доказывает.
    assert "SETTINGS-FAILED" in result.stdout


def test_orm_vector_defaults_match_settings() -> None:
    """Дефолты продублированы в двух местах — расхождение ловим здесь."""

    from geonexa_proxima.db import models

    defaults = Settings.model_fields
    assert defaults["embedding_dimensions"].default == models.DEFAULT_EMBEDDING_DIMENSIONS
    assert defaults["vector_column_type"].default.value == models.DEFAULT_VECTOR_COLUMN_TYPE


def test_blank_env_value_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """`LOG_JSON=` в .env — «по умолчанию», а не ошибка разбора.

    Пустое значение — естественный способ записать «не задано», и на нём
    сервис падал при старте с невнятным `bool_parsing`.
    """

    monkeypatch.setenv("LOG_JSON", "")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "")
    monkeypatch.setenv("DATABASE_SSL_ROOT_CERT", "")

    settings = Settings(_env_file=None, **ADMIN)

    assert settings.log_json is None
    assert settings.telegram_webhook_url is None
    assert settings.database_ssl_root_cert is None


def test_blank_value_still_fails_for_required_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Послабление касается только полей, которые допускают None."""

    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "")

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **ADMIN)


def test_loopback_model_endpoint_is_flagged(caplog) -> None:
    """Адрес моделей на loopback внутри контейнера — почти всегда опечатка.

    `EMBEDDING_MODE=api` с `http://localhost:8001/v1` собирается без ошибок и
    падает только на первом реальном запросе, посреди сбора. Предупреждение
    при сборке стоит того, чтобы не искать причину в логах воркера.
    """

    import logging

    from geonexa_proxima.ml.factory import _warn_if_loopback

    with caplog.at_level(logging.WARNING):
        _warn_if_loopback("http://localhost:8001/v1", what="Эмбеддер")
    assert any("localhost:8001" in record.getMessage() for record in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _warn_if_loopback("https://models.example.com/v1", what="Эмбеддер")
    assert not caplog.records


def test_cpu_gets_bfloat16_not_float16() -> None:
    """float16 на процессоре — ловушка, а не оптимизация.

    Половина операций не имеет нативной реализации для Half: часть версий
    torch падает с «not implemented for Half», остальные считают через
    эмуляцию — вдвое меньше памяти ценой многократного замедления. bfloat16
    даёт ту же экономию без этой платы.
    """

    from geonexa_proxima.ml.local_models import preferred_dtype

    assert preferred_dtype("cpu") == "bfloat16"
    assert preferred_dtype("cuda") == "float16"
    assert preferred_dtype("mps:0") == "float16"
