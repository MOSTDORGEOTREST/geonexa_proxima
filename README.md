# GeoNexa Proxima

Персональный AI-радар научных публикаций, методов, наборов данных и программных
инструментов для геотехники и смежных геонаук. Система собирает материалы из
научных и инженерных источников, нормализует и дедуплицирует их, оценивает
семантическую близость к профилю интересов, ранжирует и доставляет результаты
через API и Telegram.

> Проект находится в активной разработке. Доменные типы, конфигурация и контракты
> уже определены; инфраструктурные адаптеры и прикладные сервисы могут меняться.
> Актуальное состояние интеграции описано в [HANDOFF.md](HANDOFF.md).

## Возможности

- единая типизированная конфигурация из переменных окружения;
- сбор публикаций, ПО и датасетов из заменяемых источников;
- PostgreSQL 16 для метаданных и Qdrant для векторного поиска;
- embeddings и reranking через API либо локальные Qwen-модели;
- отдельные «лёгкая» и «тяжёлая» LLM для ранжирования и глубокого анализа;
- персональная таксономия интересов в `config/taxonomy.yaml`;
- Telegram-регистрация и несколько независимых исследовательских профилей;
- персональное semantic/reranker ранжирование и обучаемый feedback loop;
- FastAPI для интеграций и Telegram-бот для ежедневного дайджеста;
- пороговые уведомления и запрос углублённого анализа.

## Архитектура

GeoNexa Proxima следует портово-адаптерному подходу:

```text
Источники → Collectors → нормализация/дедупликация → PostgreSQL
                                      ↓
Профиль интересов → Embedder → Qdrant → Reranker → Ranker/Analyzer
                                                   ↓
                                            API / Telegram
```

- `domain.py` — независимые доменные модели: публикации, оценки, анализ, результаты поиска;
- `ports.py` — протоколы хранилищ, моделей, сборщиков и прикладных компонентов;
- `config.py` — единственная точка чтения runtime-конфигурации;
- адаптеры БД, провайдеров и сервисы реализуют порты и не меняют доменное ядро.

Подробности и ожидаемые потоки данных: [docs/architecture.md](docs/architecture.md).

## Требования

- Python 3.11–3.14 (для production рекомендуется 3.12+);
- Poetry 2;
- Docker с Compose plugin;
- для локальных моделей: достаточно диска, RAM/VRAM и ML-зависимости проекта.

## Быстрый старт с Poetry

```bash
git clone <repository-url>
cd geonexa_proxima
cp .env.example .env
poetry install --with dev
docker compose up -d postgres qdrant
poetry run alembic upgrade head
poetry run pytest
```

Запуск API:

```bash
poetry run uvicorn geonexa_proxima.api:app --reload --host 0.0.0.0 --port 8000
curl http://localhost:8000/health
```

CLI объявлен как `geonexa`; доступные команды можно проверить так:

```bash
poetry run geonexa --help
```

## Настройка окружения

Скопируйте шаблон и замените только необходимые значения:

```bash
cp .env.example .env
```

Минимально необходимо выбрать режим моделей, указать доступы к LLM API и, для
Telegram, токен бота и разрешённые user ID. Не коммитьте `.env`.

Важные группы переменных:

- `DATABASE_URL`, `QDRANT_URL`, `QDRANT_COLLECTION`,
  `QDRANT_PROFILE_COLLECTION` — основные и производные хранилища;
- `EMBEDDING_*`, `RERANKER_*` — embeddings и reranking;
- `LIGHT_LLM_*`, `HEAVY_LLM_*` — дешёвое ранжирование и глубокий анализ;
- `TELEGRAM_*` — бот, allowlist и webhook;
- `*_EMAIL`, `*_TOKEN` — вежливые лимиты и расширенные квоты источников;
- `*_THRESHOLD` — фильтры дайджеста, глубокого анализа и срочных уведомлений.
- `PERSONAL_*_WEIGHT`, `PERSONALIZATION_CANDIDATE_LIMIT` — прозрачная формула
  персонального ранжирования.

Значения в `.env.example` и `.env.test` фиктивные и не являются рабочими
секретами.

## Режим API для моделей

По умолчанию embeddings и reranker ожидаются как внешние HTTP-сервисы. Это
предпочтительно для небольшого API-контейнера и позволяет независимо масштабировать
GPU inference.

```dotenv
EMBEDDING_MODE=api
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-4B
EMBEDDING_API_BASE_URL=https://embedding.example/v1
EMBEDDING_API_KEY=replace-me

RERANKER_MODE=api
RERANKER_MODEL=Qwen/Qwen3-Reranker-0.6B
RERANKER_API_URL=https://reranker.example/rerank
RERANKER_API_KEY=replace-me
```

Embedding endpoint должен быть OpenAI-совместимым. Контракт reranker endpoint
зависит от адаптера провайдера; до подключения проверьте его формат запроса и
ответа.

## Локальный режим моделей

Установите ML-группу зависимостей:

```bash
poetry install --with dev,ml
```

Установите современный Hugging Face CLI и, если репозиторий модели требует
авторизации, выполните вход:

```bash
curl -LsSf https://hf.co/cli/install.sh | bash -s
hf auth login
```

Скачайте модели в игнорируемую Git директорию `models/`:

```bash
hf download Qwen/Qwen3-Embedding-4B \
  --local-dir models/Qwen3-Embedding-4B

hf download Qwen/Qwen3-Reranker-0.6B \
  --local-dir models/Qwen3-Reranker-0.6B
```

Для более качественных embeddings можно использовать 8B-вариант:

```bash
hf download Qwen/Qwen3-Embedding-8B \
  --local-dir models/Qwen3-Embedding-8B
```

Затем настройте:

```dotenv
EMBEDDING_MODE=local
EMBEDDING_LOCAL_PATH=models/Qwen3-Embedding-4B
EMBEDDING_DIMENSIONS=2560
RERANKER_MODE=local
RERANKER_LOCAL_PATH=models/Qwen3-Reranker-0.6B
```

**Память:** веса 4B в FP16 занимают примерно 8 ГБ только под параметры, а 8B —
примерно 16 ГБ. Реальное потребление RAM/VRAM выше из-за KV-cache, промежуточных
тензоров, tokenizer и runtime. Одновременная загрузка embedding и reranker моделей
требует дополнительного запаса. На машине без достаточной памяти используйте API
или поддерживаемую адаптером квантизацию. Не скачивайте модели в Git.

4B имеет нативную размерность 2560, 8B — 4096. Обе модели поддерживают Matryoshka
Representation Learning: адаптер может обрезать 8B до 2560 с повторной
L2-нормализацией, сохранив текущую Qdrant collection. Если
`EMBEDDING_DIMENSIONS` всё же меняется, создайте новую collection и переиндексируйте
материалы.

## Docker

Запустить PostgreSQL 16 и Qdrant:

```bash
docker compose up -d postgres qdrant
docker compose ps
```

Посмотреть логи и остановить сервисы:

```bash
docker compose logs -f postgres qdrant
docker compose down
```

Удаление данных выполняется только явно:

```bash
docker compose down -v
```

Приложение можно собрать и запустить Compose-профилем `api`:

```bash
docker compose --profile api up --build api
```

API-контейнер рассчитан на API-режим моделей. Локальные GPU-модели лучше запускать
отдельным inference-сервисом; они не включаются в Docker image.

Миграции БД и production-процедуры описаны в [docs/operations.md](docs/operations.md).

## Telegram

1. Создайте бота через `@BotFather` и получите токен.
2. Узнайте свой числовой Telegram user ID.
3. Добавьте значения в локальный `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=replace-with-real-token
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
```

Allowlist обязателен для персонального радара: не оставляйте доступ открытым.
Для локальной разработки используется polling. Для production задайте HTTPS URL
и случайный webhook secret:

```dotenv
TELEGRAM_WEBHOOK_URL=https://example.org/telegram/webhook
TELEGRAM_WEBHOOK_SECRET=replace-with-long-random-value
```

`/start` регистрирует Telegram identity и создаёт профиль по умолчанию. Управление
профилями выполняют `/profiles`, `/profile_new`, `/profile_use`,
`/profile_edit`, `/profile_delete`, `/interests` и `/personalization`. Один
профиль активен для интерактивных команд; каждый профиль с включённой подпиской
получает отдельный плановый дайджест.

Описание вроде «я геотехник и занимаюсь ML для разжижения грунтов» сохраняется
как часть профиля. Оно объединяется с явными интересами и feedback-сигналами,
затем используется для semantic search, reranker и персонального объяснения
релевантности. Глобальная научная оценка статьи при этом остаётся общей.

Плановый запуск всех включённых профилей:

```bash
poetry run geonexa digests
```

Команда создаёт отдельный persisted digest для каждого `digest_enabled` профиля,
подписывает сообщение именем профиля и сохраняет точный `profile_score_id` в
callback. Для проверки без отправки в Telegram используйте
`poetry run geonexa digests --no-deliver`. В production команду или
`personal_digests_flow` следует запускать по расписанию Prefect.

## Тесты и качество

```bash
poetry run pytest
poetry run pytest --cov=geonexa_proxima --cov-report=term-missing
poetry run ruff check .
poetry run ruff format --check .
```

Тестовое окружение содержит только фиктивные значения:

```bash
set -a
source .env.test
set +a
poetry run pytest
```

## Безопасность

- никогда не коммитьте `.env`, токены, дампы БД и загруженные модели;
- ограничивайте Telegram-бота списком пользователей;
- задавайте отдельные пароли БД для production;
- не публикуйте Qdrant и PostgreSQL напрямую в интернет;
- pin-те revision моделей для воспроизводимого production-развёртывания;
- перед использованием источников соблюдайте их rate limits и условия лицензий.

## Документы

- [Архитектура](docs/architecture.md)
- [Эксплуатация](docs/operations.md)
- [Передача проекта и TODO](HANDOFF.md)
