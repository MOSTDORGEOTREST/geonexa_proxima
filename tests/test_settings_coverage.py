"""Каждая объявленная настройка либо действует, либо числится в списке ожидающих.

Настройка, которая есть в `.env` и ни на что не влияет, хуже её отсутствия:
администратор меняет значение, перезапускает сервис и делает ложный вывод о
поведении системы. Этот тест не даёт такому списку расти незаметно.
"""

from __future__ import annotations

import pathlib
import re

from geonexa_proxima.config import Settings

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Объявлены, но ещё не подключены — по частям, которые не построены.
#: Список можно только сокращать. Добавление сюда означает: настройка врёт.
NOT_WIRED_YET = {
    # шифрование секретов в app_settings: сейчас ключи провайдеров хранятся
    # в .env, а в базу попадает только маскированное значение
    "secret_encryption_key",
    # учёт расхода токенов и лог вызовов LLM пишутся не приложением
    "llm_daily_token_budget",
    "llm_log_calls",
    # окно «активного подписчика» считается роллапом, а не настройкой
    "metrics_active_window_days",
    # экспорт Prometheus
    "prometheus_enabled",
    "prometheus_path",
    # читаются docker compose, а не Python-кодом
    "prefect_api_database_connection_url",
}


def _source() -> str:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for folder in ("src", "scripts", "migrations", "tests")
        for path in (ROOT / folder).rglob("*.py")
        if "__pycache__" not in str(path) and path.name != pathlib.Path(__file__).name
    )
    # Объявления полей — не использование.
    return re.sub(r"^\s{4}[a-z_]+: .*$", "", text, flags=re.M)


def test_no_setting_is_silently_ignored() -> None:
    code = _source()
    unused = {name for name in Settings.model_fields if not re.search(rf"\b{name}\b", code)}
    surprises = sorted(unused - NOT_WIRED_YET)
    assert not surprises, "Настройки объявлены, но нигде не читаются: " + ", ".join(surprises)


def test_wired_settings_are_not_listed_as_pending() -> None:
    """Обратная сторона: подключил — вычеркни из списка."""

    code = _source()
    stale = sorted(name for name in NOT_WIRED_YET if re.search(rf"\b{name}\b", code))
    assert not stale, "Уже используются, но числятся неподключёнными: " + ", ".join(stale)


def test_pending_list_only_mentions_real_settings() -> None:
    unknown = sorted(NOT_WIRED_YET - set(Settings.model_fields))
    assert not unknown, "В списке несуществующие настройки: " + ", ".join(unknown)


def test_every_setting_appears_in_env_example() -> None:
    """`.env.example` — это и документация, и чек-лист развёртывания.

    Настройка, которой там нет, существует только в исходниках: администратор
    узнает о ней, когда что-то сломается.
    """

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    declared = set(re.findall(r"^([A-Z_]+)=", example, flags=re.M))
    missing = sorted(name.upper() for name in Settings.model_fields if name.upper() not in declared)
    assert not missing, "Нет в .env.example: " + ", ".join(missing)


def test_env_example_holds_no_real_secrets() -> None:
    """Пример конфигурации уезжает в git — настоящих ключей в нём быть не может."""

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    # Токен бота Telegram: <цифры>:<35 символов>. Формат узнаваемый, и это к лучшему.
    assert not re.search(r"^TELEGRAM_BOT_TOKEN=\d{6,}:[\w-]{30,}", example, flags=re.M)
    # Ключи DeepSeek/OpenAI начинаются с sk-.
    assert not re.search(r"^[A-Z_]*API_KEY=sk-[\w-]{16,}", example, flags=re.M)
    # DSN с непустым паролем.
    assert not re.search(r"^DATABASE_URL=.+://[^:]+:[^@\s]{8,}@(?!HOST)", example, flags=re.M)


def test_env_example_covers_compose_requirements() -> None:
    """Каждая переменная, без которой compose не стартует, есть в примере.

    `${VAR:?...}` в compose означает «без этого не запускаемся». Если такой
    переменной нет в `.env.example`, развёртывание падает на первой же команде,
    и узнаёт об этом тот, кто разворачивает, а не тот, кто писал compose.
    """

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    declared = set(re.findall(r"^([A-Z_]+)=", example, flags=re.M))
    missing: set[str] = set()
    for name in ("docker-compose.yml", "docker-compose.dev.yml"):
        path = ROOT / name
        if not path.exists():
            continue
        required = set(re.findall(r"\$\{([A-Z_]+):\?", path.read_text(encoding="utf-8")))
        missing |= required - declared
    assert not missing, "Требуются compose, но нет в .env.example: " + ", ".join(sorted(missing))
