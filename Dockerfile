# Образ приложения: api, bot и prefect-worker поднимаются из одного слоя,
# отличаясь только командой. Веса моделей в образ не кладутся — они
# монтируются томом, иначе каждый билд тащил бы 2.4 ГБ.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=2.1.1 \
    POETRY_VIRTUALENVS_CREATE=false

# Локальные модели тянут torch — это ~2.5 ГБ в образе. По умолчанию их нет:
# большинству установок хватает API-режима. Стенд с весами в models/ собирается
# как `--build-arg INSTALL_ML=true`, иначе EMBEDDING_MODE=local не заработает
# и сервис останется в состоянии not_ready.
ARG INSTALL_ML=false

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

# Зависимости отдельным слоем: правка исходников не пересобирает их заново.
# README нужен здесь же: pyproject объявляет его как readme проекта, и без
# файла установка пакета падает на генерации метаданных.
COPY pyproject.toml poetry.lock README.md ./
RUN poetry check --lock --no-interaction \
 && if [ "${INSTALL_ML}" = "true" ]; then \
      poetry install --only main,ml --no-root --no-interaction --no-ansi; \
    else \
      poetry install --only main --no-root --no-interaction --no-ansi; \
    fi

COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY scripts/ ./scripts/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN pip install --no-cache-dir --no-deps -e . \
 && chmod 0755 /usr/local/bin/entrypoint.sh \
 && useradd --create-home --uid 10001 proxima \
 && chown -R proxima:proxima /app

USER proxima

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
