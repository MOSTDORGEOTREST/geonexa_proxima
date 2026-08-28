# Эксплуатация GeoNexa Proxima

## Локальная среда

```bash
cp .env.example .env
poetry install --with dev
docker compose up -d postgres qdrant
docker compose ps
```

Проверка инфраструктуры:

```bash
docker compose exec postgres pg_isready -U geonexa -d geonexa
curl http://localhost:6333/readyz
```

Пароль Compose по умолчанию предназначен только для локальной разработки.
Измените `POSTGRES_PASSWORD` и согласуйте его с `DATABASE_URL`.

## Запуск приложения

Во время разработки:

```bash
poetry run uvicorn geonexa_proxima.api:app \
  --reload --host 127.0.0.1 --port 8000
```

Контейнерный запуск:

```bash
docker compose --profile api up --build -d
docker compose --profile api logs -f api
```

Dockerfile не включает локальные веса и ML-группу. Контейнер рассчитан на
`EMBEDDING_MODE=api` и `RERANKER_MODE=api`.

Один проход персональных дайджестов:

```bash
poetry run geonexa digests
```

Для production создайте Prefect deployment для
`geonexa_proxima.workflows.digests:personal_digests_flow`. Каждый запуск
обрабатывает все включённые профили независимо; ошибка одного профиля не
останавливает остальные. `--no-deliver` сохраняет готовые дайджесты без отправки
и удобен для smoke test.

## Миграции

Перед первым запуском и каждым совместимым релизом:

```bash
poetry run alembic upgrade head
```

Перед production-миграцией:

1. проверьте migration SQL на копии данных;
2. сделайте backup PostgreSQL;
3. оцените lock duration для крупных таблиц;
4. мигрируйте одним экземпляром приложения;
5. запустите smoke test API и фонового pipeline.

Откат схемы выполняйте только если downgrade конкретной миграции проверен.
Откат приложения вперёд- и назад-совместимой схемой безопаснее слепого downgrade.

## Конфигурация production

Обязательные пользовательские значения:

- уникальные `POSTGRES_PASSWORD` и `DATABASE_URL`;
- закрытый Qdrant endpoint и, если используется, `QDRANT_API_KEY`;
- реальные endpoint/model/key для обеих LLM;
- API или local mode для embedder и reranker;
- `TELEGRAM_BOT_TOKEN` и allowlist пользователей;
- публичный HTTPS URL и webhook secret при webhook-режиме.

Секреты храните в secret manager платформы, а не в Compose YAML, image или Git.
Запускайте контейнер от непривилегированного пользователя и ограничивайте сетевой
доступ к PostgreSQL/Qdrant.

## Модели

Для API mode отдельно контролируйте:

- совместимость схемы endpoint с адаптером;
- model ID и revision;
- размерность embeddings;
- timeout, retry и лимиты провайдера;
- политику передачи потенциально чувствительных текстов.

Для local mode:

- заранее скачайте веса через `hf download`;
- проверьте лицензию и checksum/revision;
- измерьте пиковое потребление RAM/VRAM;
- не запускайте несколько worker, каждый из которых загружает свою копию весов;
- прогрейте модель перед включением readiness.

Смена embedding-размерности требует новой Qdrant collection. Безопасная схема:

1. создать collection с новым именем;
2. переиндексировать все документы;
3. сравнить полноту и качество поиска;
4. атомарно переключить конфигурацию чтения;
5. удалить старую collection только после периода наблюдения.

## Наблюдаемость

Минимальный набор сигналов:

- количество собранных, новых, дублированных и отклонённых материалов;
- latency/error rate по каждому внешнему источнику и model endpoint;
- длина очереди, возраст последнего успешного pipeline run;
- распределение semantic и total score;
- число глубоких анализов и стоимость LLM;
- размер PostgreSQL и Qdrant collection;
- Telegram delivery success/failure.
- количество активных профилей, время персонального reranking и долю cache hit
  profile embeddings;
- распределение personal score отдельно от глобального scientific score.

Не логируйте API keys, Telegram token, webhook secret и полный `.env`.

## Резервное копирование

PostgreSQL — обязательный backup:

```bash
docker compose exec -T postgres \
  pg_dump -U geonexa -d geonexa -Fc > geonexa.dump
```

Пример восстановления в заранее созданную пустую БД:

```bash
docker compose exec -T postgres \
  pg_restore -U geonexa -d geonexa --clean --if-exists < geonexa.dump
```

Команды содержат локальные имена по умолчанию; подставьте production-реквизиты.
Храните backup зашифрованно и регулярно проверяйте восстановление.

Qdrant — производный индекс, но для быстрого recovery полезны snapshots. Даже при
наличии snapshot должна сохраняться возможность полной переиндексации из
PostgreSQL.

Collection документов задаётся `QDRANT_COLLECTION`, а кэш пользовательских
профилей — `QDRANT_PROFILE_COLLECTION`. Удаление profile collection не приводит
к потере настроек: embeddings будут восстановлены из `user_profiles.compiled_text`
и `version`.

## Пользовательские профили

Регистрация выполняется через `/start` только после прохождения Telegram
allowlist. PostgreSQL хранит минимальные Telegram-атрибуты, профили, явные
интересы и learned feedback. Не записывайте в профиль секретные данные.

Перед миграцией `0002_user_profiles` обязательно сделайте backup: она создаёт
default profile для существующих пользователей, переносит `user_interests` и
добавляет profile attribution к историческому feedback/digests.

Если профиль отредактирован, его `version` увеличивается. Старые
`profile_item_scores` сохраняются для аудита, но не должны использоваться для
нового дайджеста; Qdrant cache с предыдущей версией считается miss.

## Обновление и откат

1. зафиксируйте версии image и model revision;
2. выполните unit и integration tests;
3. сделайте backup;
4. примените миграции;
5. обновите один экземпляр и проверьте health/readiness;
6. разверните остальные экземпляры;
7. проверьте сбор, поиск и доставку Telegram.

При деградации остановите фоновые задания, откатите application image и только
после анализа принимайте решение об откате данных.

## Диагностика

### PostgreSQL недоступен

```bash
docker compose ps postgres
docker compose logs postgres
```

Проверьте, что пароль в `DATABASE_URL` совпадает с `POSTGRES_PASSWORD`. Изменение
переменной после создания volume не меняет пароль уже инициализированной БД.

### Qdrant недоступен

```bash
docker compose ps qdrant
docker compose logs qdrant
curl http://localhost:6333/readyz
```

### API не стартует

Проверьте импорт `geonexa_proxima.api:app`, применённые миграции, readiness
зависимостей и фактические переменные процесса. Health подтверждает работу
HTTP-процесса, но для обработки данных нужен успешный `/ready`.

### Ошибка размерности vectors

`EMBEDDING_DIMENSIONS`, фактический размер вектора модели и размерность Qdrant
collection должны совпадать. Не изменяйте размерность существующей collection
«на месте» — создайте новую и переиндексируйте.
