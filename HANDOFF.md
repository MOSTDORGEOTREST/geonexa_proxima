# Передача проекта GeoNexa Proxima

## Что готово

- типизированные runtime-настройки в `src/geonexa_proxima/config.py`;
- независимые доменные модели и формула `RankResult.total_score`;
- порты для collectors, model providers, repository и vector store;
- асинхронные collectors для arXiv, OpenAlex, Crossref и GitHub, а также
  Semantic Scholar enricher;
- нормализация, DOI/arXiv/title/fuzzy-дедупликация и сохранение provenance всех
  источников;
- PostgreSQL-модели, async repositories и Alembic-миграции для материалов,
  пользователей, нескольких профилей, интересов, персональных оценок, дайджестов
  и profile-aware feedback;
- Qdrant adapters для semantic search и версионированного кэша profile embeddings;
- локальные и API-адаптеры Qwen3 Embedding/Reranker с ленивой загрузкой;
- отдельные OpenAI-compatible клиенты лёгкой и тяжёлой LLM со строгим JSON;
- ingestion pipeline, Prefect flow, FastAPI, CLI и Telegram polling/webhook;
- команды Telegram `/daily`, `/week`, `/hot`, `/papers`, `/tools`, `/datasets`,
  `/search`, `/trends`, `/why` и item-level feedback;
- Telegram-регистрация, FSM и команды `/profiles`, `/profile_new`, `/profile_use`,
  `/profile_edit`, `/profile_delete`, `/interests`, `/personalization`;
- несколько профилей с одним активным, отдельными digest settings и подписками;
- детерминированный ProfileCompiler, персональный reranking и формула
  `0.40 semantic + 0.25 reranker + 0.25 global + 0.10 interests/feedback`;
- learned feedback-сигналы хранятся отдельно от заявленных пользователем интересов;
- Prefect flow и CLI `geonexa digests` для отдельных подписанных дайджестов каждого
  включённого профиля;
- русскоязычный README с quickstart, режимами моделей, Docker и Telegram;
- шаблоны `.env.example` и `.env.test` без реальных секретов;
- PostgreSQL 16 и Qdrant в `docker-compose.yml`;
- Dockerfile и Compose profile `api` как scaffold для FastAPI;
- профиль интересов и предметная таксономия в `config/taxonomy.yaml`;
- архитектурная и эксплуатационная документация;
- 30 unit-тестов, включая compiler, scoring, profile vectors, feedback attribution
  и Telegram callback limits;
- пройдены `ruff`, `pytest`, `compileall`, `poetry check --lock`,
  `docker compose config`, CLI/container и Alembic offline/live smoke checks;
- live PostgreSQL/Qdrant проверка регистрации, active-profile constraint,
  интересов, profile scores, feedback и versioned vector cache.

## Что остаётся сделать

- выполнить end-to-end запуск после получения реальных PostgreSQL/Qdrant и API
  реквизитов;
- откалибровать semantic/ranking thresholds на ручной выборке;
- проверить collectors на реальных rate limits и добавить source cursors;
- проверить API/local embedder и reranker на выбранных endpoints/весах;
- проверить Telegram polling/webhook с реальным ботом;
- добавить интеграционные тесты PostgreSQL/Qdrant и contract tests внешних API;
- добавить фоновую обработку запроса «разобрать глубже»;
- добавить knowledge graph после накопления качественного корпуса;
- pin-нуть production images и model revisions;
- настроить Prefect deployment/расписание в целевой инфраструктуре.

## Значения, которые должен предоставить пользователь

Реальные реквизиты намеренно не включены. До рабочего запуска нужно выбрать и
предоставить:

- адрес, имя, пользователя и пароль PostgreSQL;
- адрес и при необходимости API key Qdrant;
- `api` или `local` режим отдельно для embeddings и reranker;
- реальные endpoint/API key для API mode либо заранее скачанные веса для local mode;
- model ID, embedding dimensions и допустимые batch sizes;
- endpoint, model и API key для лёгкой и тяжёлой LLM;
- Telegram bot token, разрешённые user ID и webhook-параметры;
- расписание и digest settings для включённых пользовательских профилей;
- optional keys/email для научных источников.

Локальный пароль `change-me` и все значения `replace-me`/`dummy-*` — заглушки.

## Важные оговорки

- Docker profile `api` использует `geonexa_proxima.api:app`; успешный health ещё
  не означает готовность БД, Qdrant и model providers — для этого проверяйте `/ready`.
- Dockerfile предназначен для API-mode и не включает тяжёлые ML-зависимости/веса.
- Смена модели или `EMBEDDING_DIMENSIONS` требует новой Qdrant collection и
  переиндексации документов; profile collection является rebuildable cache.
- FP16 embedding-модели 4B/8B требуют примерно 8/16 ГБ только под веса; нужен
  дополнительный запас RAM/VRAM.
- Тесты не обращаются к реальным API, не скачивают модели и не требуют Docker.

## Рекомендуемый следующий шаг

После получения реквизитов:

```bash
cp .env.example .env
poetry install
poetry run ruff check .
poetry run pytest
docker compose config
docker compose up -d postgres qdrant
poetry run alembic upgrade head
poetry run geonexa collect
```

Затем проверить `/health`, одну тестовую коллекцию, сохранение/дедупликацию,
векторный поиск, ранжирование и доставку Telegram на фиктивном материале.
