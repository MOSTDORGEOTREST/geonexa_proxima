from pathlib import Path

import pytest
from pydantic import ValidationError

from geonexa_proxima.config import Environment, ProviderMode, Settings


def test_settings_parse_environment_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "GeoNexa Test")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("EMBEDDING_MODE", "local")
    monkeypatch.setenv("EMBEDDING_LOCAL_PATH", "models/custom-embedding")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "32")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "101, 202")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy-token")

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
        Settings(_env_file=None, **{field: value})


def test_settings_reject_personalization_weights_that_do_not_sum_to_one() -> None:
    with pytest.raises(ValidationError, match=r"must sum to 1\.0"):
        Settings(_env_file=None, personal_semantic_weight=0.2)
