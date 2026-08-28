FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app/src"

WORKDIR /app

RUN pip install "poetry>=2,<3"

COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root

COPY src ./src
COPY config ./config
COPY alembic.ini ./
COPY migrations ./migrations

RUN addgroup --system geonexa \
    && adduser --system --ingroup geonexa --home /app geonexa \
    && chown -R geonexa:geonexa /app

USER geonexa

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

# The API module is supplied by the application layer.
CMD ["uvicorn", "geonexa_proxima.api:app", "--host", "0.0.0.0", "--port", "8000"]
