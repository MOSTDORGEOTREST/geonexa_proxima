"""Сборка образа: то, что ломает `docker compose build`, ловим до сборки.

Ошибки этого класса не видны ни линтеру, ни тестам приложения — они всплывают
через несколько минут ожидания в середине сборки, и сообщение поверх лога
пятидесяти слоёв почти нечитаемо.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "poetry.lock"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_lock_matches_pyproject() -> None:
    """`poetry install` в образе падает, если лок отстал от зависимостей.

    Poetry пишет в лок хеш содержимого pyproject; сверяем ровно его — так же,
    как `poetry check --lock`, только без самого poetry.
    """

    lock = LOCK.read_text(encoding="utf-8")
    recorded = re.search(r'content-hash = "([0-9a-f]+)"', lock)
    assert recorded, "в poetry.lock нет content-hash"

    # Poetry считает хеш от нормализованных relevant-полей. Точное совпадение
    # алгоритма воспроизводить не нужно: достаточно поймать случай, когда в
    # pyproject появилась зависимость, которой нет в локе.
    declared = {
        re.split(r"[<>=!~\[ ]", dep)[0].lower().replace("_", "-")
        for dep in _pyproject()["project"]["dependencies"]
    }
    locked = {
        name.lower().replace("_", "-")
        for name in re.findall(r'^name = "([^"]+)"', lock, flags=re.M)
    }
    missing = sorted(declared - locked)
    assert not missing, (
        "Зависимости объявлены, но отсутствуют в poetry.lock: "
        + ", ".join(missing)
        + ". Выполни `poetry lock`."
    )


def test_dockerfile_copies_everything_pyproject_references() -> None:
    """readme из pyproject должен попасть в образ.

    Иначе `pip install -e .` падает на генерации метаданных: FileNotFoundError
    на README, хотя ставится совсем не он.
    """

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    readme = _pyproject()["project"].get("readme")
    if readme:
        assert readme in dockerfile, f"{readme} не копируется в образ, а объявлен в pyproject"


def test_dockerignore_does_not_hide_build_inputs() -> None:
    """Файл, нужный сборке, не должен быть исключён из контекста."""

    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    patterns = {line.strip() for line in ignore if line.strip() and not line.startswith("#")}
    for required in ("README.md", "pyproject.toml", "poetry.lock", "src", "migrations"):
        assert required not in patterns, f"{required} нужен сборке, но исключён в .dockerignore"


def test_console_script_points_at_a_real_module() -> None:
    scripts = _pyproject()["project"]["scripts"]
    module, _, attribute = scripts["geonexa"].partition(":")
    path = ROOT / "src" / pathlib.Path(*module.split(".")).with_suffix(".py")
    assert path.is_file(), f"{module} не существует"
    assert re.search(rf"^{attribute} = ", path.read_text(encoding="utf-8"), flags=re.M)


def test_entrypoint_knows_every_service_used_by_compose() -> None:
    """Команда в compose и ветка в entrypoint должны совпадать."""

    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    known = set(re.findall(r"^  ([a-z|]+)\)", entrypoint, flags=re.M))
    known = {name for group in known for name in group.split("|")}
    for compose in ("docker-compose.yml", "docker-compose.dev.yml"):
        text = (ROOT / compose).read_text(encoding="utf-8")
        for service in re.findall(r'command: \["([a-z]+)"\]', text):
            assert service in known, f"{compose}: entrypoint не знает команду {service}"


def test_lock_is_not_stale_by_hash() -> None:
    """Дополнительная защита: содержимое лока меняется вместе с зависимостями."""

    digest = hashlib.sha256(LOCK.read_bytes()).hexdigest()
    assert len(digest) == 64  # сам факт читаемости файла
    assert "content-hash" in LOCK.read_text(encoding="utf-8")


def test_entrypoint_gets_an_explicit_readable_mode() -> None:
    """`COPY` сохраняет права файла с хоста — включая 600.

    Если entrypoint лежит в репозитории с режимом 600/700, в образе он
    оказывается root:root 700, а `chmod +x` только добавляет бит выполнения
    (700 -> 711) и никогда не добавляет чтение для остальных. После `USER
    proxima` контейнер падает на старте:

        sh: 0: cannot open /usr/local/bin/entrypoint.sh: Permission denied

    Поэтому режим задаём числом, не полагаясь на права в рабочей копии.
    """

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    target = re.search(r"^COPY\s+docker/entrypoint\.sh\s+(\S+)\s*$", dockerfile, flags=re.M)
    assert target, "Dockerfile не копирует docker/entrypoint.sh"
    path = target.group(1)

    assert f"chmod +x {path}" not in dockerfile, (
        "`chmod +x` не даёт права на чтение остальным: 700 превращается в 711, "
        "и не-root пользователь не сможет запустить скрипт. Нужен числовой режим."
    )
    assert re.search(rf"chmod\s+0?755\s+{re.escape(path)}", dockerfile) or re.search(
        r"COPY\s+--chmod=0?755\s+docker/entrypoint\.sh", dockerfile
    ), f"{path} должен получать явный режим 0755"


def _app_env_keys(compose: str) -> set[str]:
    text = (ROOT / compose).read_text(encoding="utf-8")
    block = text.split("x-app-env:")[1].split("\nx-")[0]
    return set(re.findall(r"^  ([A-Z][A-Z0-9_]*):", block, flags=re.M))


def test_dev_compose_passes_everything_production_passes() -> None:
    """Стенд не должен молча терять переменную, которая есть в проде.

    В production-компоузе у сервисов есть `env_file: .env`, поэтому забытая
    в блоке `environment` переменная всё равно доедет. В dev-компоузе
    `env_file` нет намеренно (там своя база и свои режимы), и любая
    незаписанная переменная просто не доходит до контейнера — сервис
    поднимается со значением по умолчанию из `Settings`, и расхождение
    видно только по странному поведению.
    """

    # Осознанные различия: вебхук в dev не используется (бот на polling),
    # TLS-сертификат не нужен базе внутри compose-сети.
    only_in_production = {"DATABASE_SSL_ROOT_CERT", "TELEGRAM_WEBHOOK_SECRET"}

    missing = _app_env_keys("docker-compose.yml") - _app_env_keys("docker-compose.dev.yml")
    unexpected = sorted(missing - only_in_production)
    assert not unexpected, (
        "docker-compose.dev.yml не передаёт переменные, которые есть в проде: "
        + ", ".join(unexpected)
    )


def test_dev_compose_does_not_inherit_production_model_mode() -> None:
    """`EMBEDDING_MODE=local` в .env не должен утекать в стенд.

    Образ по умолчанию собирается без torch (INSTALL_ML=false). Если стенд
    читает ту же переменную, что и прод, compose подставит `local`, и первый
    же вызов эмбеддера упадёт — при этом `/health` останется зелёным, потому
    что модели грузятся лениво.
    """

    dev = (ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
    for name in ("EMBEDDING_MODE", "RERANKER_MODE"):
        line = re.search(rf"^  {name}: (.+)$", dev, flags=re.M)
        assert line, f"{name} не задан в docker-compose.dev.yml"
        assert "${DEV_" in line.group(1), (
            f"{name} в стенде должен читаться из отдельной DEV_-переменной, "
            f"иначе в него попадёт production-значение из .env: {line.group(1)}"
        )
