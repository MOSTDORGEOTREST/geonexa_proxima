# Архитектура Проксимы

Проксима — сервис ГЕОНЕКСЫ: радар научных публикаций по инженерной геологии,
геотехнике и применению ИИ в них. Ниже — архитектура как она реализована.

## 1. Что делает система

Один раз в цикл платформа обходит внешние научные источники, отбирает по
глобальному **harvest-профилю** только то, что вообще относится к делу,
нормализует, дедуплицирует, оценивает и складывает в общий корпус. Дальше для
каждого подписчика — человека, группы или канала — независимо строится
персональный дайджест по его профилю интересов, а два отдельных воркера
развозят готовые сообщения по личным чатам и по группам/каналам. Всё это
видно и настраивается в веб-админке.

Ключевой принцип: **сбор общий, оценка глобальная, отбор персональный,
доставка отдельная**. Каждая из четырёх стадий — свой Prefect-флоу со своим
расписанием, своим состоянием в БД и своей страницей в админке.

## 2. Компоненты

```
┌───────────────────────────────────────────────────────────────────────────┐
│  admin-ui (SvelteKit + TS, adapter-node)          :5173                   │
│  Дашборд · Аналитика · Harvest · Расписания · Подписчики · Подписки       │
│  Каналы · Доставки · Модели · Настройки · Аудит                           │
└──────────────────────────────┬────────────────────────────────────────────┘
                               │ REST /api/admin/*  (JWT, логин из .env)
                               │ браузер ходит только в node-слой SvelteKit,
                               │ токен лежит в httpOnly-cookie
┌──────────────────────────────▼────────────────────────────────────────────┐
│  api (FastAPI)                        :8000                               │
│  /api/admin/* · /telegram/webhook · /health · /ready · /metrics           │
│  Прокси к Prefect REST API для расписаний и ручных запусков               │
└───┬──────────────────────┬───────────────────────┬────────────────────────┘
    │                      │                       │
┌───▼──────────────────────────────────┐  ┌────▼─────────────────────────┐
│ PostgreSQL 16 · Timeweb Cloud        │  │ Prefect Server + Worker      │
│ TLS verify-full                      │  │ :4200                        │
│ корпус · векторы (pgvector) ·        │  │ deployments, schedules, runs │
│ подписчики · подписки · очередь ·    │  │ (отдельная БД prefect)       │
│ логи · метрики                       │  │                              │
└──────────────────────────────────────┘  └──────────────────────────────┘
                               ▲
┌──────────────────────────────┴────────────────────────────────────────────┐
│  bot (aiogram)  — polling в dev, webhook через api в prod                 │
└───────────────────────────────────────────────────────────────────────────┘
```

Сервисы docker-compose: `prefect-postgres`, `prefect-server`, `prefect-worker`,
`api`, `bot`, `admin-ui`, плюс `postgres` (с pgvector) только для локальной
разработки.
Профили compose: `local-db` (локальный postgres вместо управляемого),
`orchestration` (prefect), `app` (api+bot), `ui` (admin-ui),
`ml` (опциональный inference-сервис embeddings/reranker).

Отдельного векторного сервиса нет: векторы живут в той же базе, что и корпус,
поэтому материал и его вектор пишутся одной транзакцией и не могут разойтись.
Порт `VectorStore` при этом сохранён — переезд на Qdrant, если объёмы вырастут,
стоит одного адаптера.

### База данных

Основная БД — управляемый PostgreSQL в Timeweb Cloud, подключение по TLS с
`verify-full`. Два следствия, которые видно в коде:

- **Векторы индексируются pgvector.** HNSW и IVFFlat на типе `vector` держат
  2000 измерений, HNSW на `halfvec` — 4000. При 1024 измерениях модели 0.6B
  запас двукратный; переход на 4B (2560) разобран в README.
- **asyncpg не понимает `sslmode` в строке подключения.** Режим задаётся
  `DATABASE_SSL_MODE`, а драйверу уходит готовый `ssl.SSLContext` из
  `db/ssl_support.py`. Если `sslmode` всё-таки окажется в `DATABASE_URL`,
  `Settings` откажется стартовать с внятным сообщением, а не свалится в
  недрах драйвера.
- **Пароль в DSN обязан быть percent-encoded.** Иначе `{`, `+`, `$` и `?`
  ломают разбор URL, а `$` вдобавок интерполируется docker compose.

`verify-ca` и `verify-full` без корневого сертификата отклоняются на старте:
проверка без CA — это её видимость.

Prefect хранит метаданные в своей БД. У управляемой БД выдана одна база
`default_db`, поэтому по умолчанию Prefect работает с контейнером
`prefect-postgres`; если в панели Timeweb завести вторую базу, достаточно
подставить её DSN в `PREFECT_API_DATABASE_CONNECTION_URL`.

Перед первой миграцией:

```bash
poetry run python scripts/check_db.py
```

Скрипт отвечает на вопросы, которые дороже выяснять постфактум: доехали ли,
шифруется ли канал, чем занята база, есть ли `btree_gist` (нужен для
ограничения непересекающихся подписок) и права на `CREATE`.

## 3. Единая модель подписчика

В v1 были `users` (только люди). В v2 вводится **subscriber** — всё, что имеет
telegram chat_id и может получать дайджест:

| kind      | что это                    | chat_id | профиль интересов |
|-----------|----------------------------|---------|-------------------|
| `user`    | личный чат с ботом         | > 0     | свой              |
| `group`   | группа / супергруппа       | < 0     | свой              |
| `channel` | канал, где бот администратор | < 0   | свой              |

Группа и канал — такие же подписчики, со своими профилями, расписаниями и
подпиской. Отличаются только транспортом доставки, форматом сообщения и
лимитами Telegram. Это даёт тематические каналы («канал про разжижение»,
«канал про InSAR») без отдельной ветки логики.

Миграция: `users` → `subscribers`, `user_profiles` → `subscriber_profiles`
(см. [db-schema.md](db-schema.md)).

## 4. Harvest-профиль — что мы вообще ищем

Единственный ответ на вопрос «что тянуть из внешнего мира». Живёт в БД
(`harvest_profiles` / `harvest_term_groups` / `harvest_terms` /
`harvest_queries`), сидируется из [config/harvest.yaml](../config/harvest.yaml),
редактируется в админке, экспортируется обратно в YAML для git.

Структура:

- **группы терминов** с режимом `any_of` / `all_of` / `none_of`, областью
  поиска (title / abstract / keywords), весами по группе и по термину;
- **булево выражение `satisfy`** над id групп — главное правило допуска.
  По умолчанию: `geo_domain and (ai_method or geo_sensing) and not hard_exclude`.
  «Хотя бы одно слово из списка» — это `any_of` с `min_matches: 1`;
  «эти слова обязательны» — группа `must_have` в режиме `all_of`;
- **`keyword_score`** — взвешенная сумма попаданий, нормированная в [0,1],
  минус штраф за soft-exclude. Порог `keyword_score_threshold`;
- **шаблоны запросов по источникам** с приоритетом, лимитом и вкл/выкл.

Первый профиль `geo_ai_core` собран под инженерную геологию + геотехнику +
ML/AI: 323 термина в семи группах, RU и EN, плюс список приоритетных журналов
Crossref, для которых порог понижается.

### Воронка отбора

```
источники   →  Stage 0   →  Stage 1     →  Stage 2      →  Stage 3   →  Stage 4
              запросы     keyword gate   semantic gate    light LLM   heavy LLM
              по профилю  (0 стоимости)  (эмбеддинг)      (скоринг)   (разбор)
   ~10k          ~2k          ~300           ~200            ~200        ~15
```

- **Stage 1** — детерминированный матчинг. Никакой сети, никаких моделей.
  Исходы: `accepted` / `borderline` / `rejected`. Именно он не даёт качать всё
  подряд.
- **Stage 2** — эмбеддинг материала против эмбеддинга harvest-профиля;
  `borderline` спасается, если косинус выше `borderline_semantic_threshold`.
- **Stage 3** — light LLM ставит глобальную научную оценку (`RankResult`).
  Она общая для всех подписчиков и считается один раз.
- **Stage 4** — heavy LLM разбирает только материалы выше
  `deep_analysis_threshold`.

Каждое решение пишется в `harvest_decisions` с matched-терминами и причиной —
это то, на чём в админке потом настраивают пороги, а не на ощущениях.

## 5. Prefect: каталог флоу

| Флоу | Расписание по умолчанию | Параллельность | Что делает |
|---|---|---|---|
| `global-harvest` | `0 3 * * *` | 1 (глобальный lock) | Сбор, гейты, скоринг, запись корпуса |
| `digest-dispatch` | `0 7 * * 1` | 1 | Диспетчер личных чатов (`kinds=["user"]`) |
| `digest-dispatch-chats` | `30 7 * * 1` | 1 | Диспетчер групп и каналов |
| `subscriber-digest` | по подписчику | N параллельно | Один подписчик: отбор, ранжирование, `delivery_jobs` |
| `delivery-personal` | `*/5 * * * *` | 1 воркер, конкурентность внутри | Рассылка в личные чаты |
| `delivery-group` | `*/5 * * * *` | 1 воркер | Рассылка в группы и каналы |
| `chat-monitor` | `0 */6 * * *` | 1 | Проверка, где бот ещё состоит |
| `metrics-rollup` | `15 * * * *` | 1 | Пересчёт суточных агрегатов за последние 3 дня |
| `subscription-maintenance` | `0 5 * * *` | 1 | Истечение подписок и напоминания о продлении |
| `maintenance` | `30 4 * * *` | 1 | Протухшие задания рассылки и уборка сырых событий |

### 5.1 `global-harvest`

Один процесс, один список на всю платформу. Персонализации здесь нет.

```
plan_queries(harvest_profile)          # активные harvest_queries по источникам
  └─ fan-out по источникам (task per source, конкурентно)
       fetch(source, query, cursor)    # с курсорами и rate-limit
       normalize → dedupe
       keyword_gate  → harvest_decisions
       persist(items, item_sources)    # provenance всех источников
  embed(accepted + borderline) → item_vectors (pgvector)
  semantic_gate(borderline)
  llm_rank(passed)                     # роль ranker, light
  llm_analyze(top)                     # роль analyzer, heavy
  finish(harvest_run, stats)
```

Идемпотентность: `item_sources(source, external_id)` уникален; повторный
прогон обновляет `last_seen_at`, не создаёт дубли. Курсоры в `source_cursors`
позволяют доливать историю без перезапуска с нуля. Один одновременный run на
источник гарантирует partial unique index.

### 5.2 `digest-dispatch` → `subscriber-digest`

Диспетчер не считает ничего — он только решает, кому пора, и запускает
`run_deployment` для каждого подходящего профиля **параллельно** (Prefect
concurrency limit ограничивает одновременность, скажем, 8).

Кому пора (`services/dispatch_queries.py::DUE_PROFILES`):
`subscriber_profiles.digest_enabled = true`, у подписчика активная подписка на
момент запуска, и `next_digest_at <= now()` (расписание может быть чаще недели
— вплоть до почасового).

Вид подписчика — параметр запроса, а не константа. Диспетчеров два, и это не
дублирование: у личек и у чатов разные лимиты Bot API, разная частота и разная
цена ошибки, а затык в одном не должен останавливать другой. Для чатов
добавляется условие, которого нет у личек: бот должен всё ещё состоять в чате,
а для канала — иметь право постить. Подписка у группы может быть живой, а
доставлять уже некуда — тогда профиль в выборку не попадает.

`subscriber-digest` для одного профиля:

```
resolve_window(profile)                     # since = last_digest_at или lookback
candidates = corpus.select(since, global_score >= threshold, kinds)
vector = profile_vectors.get(profile, version) or rebuild()
semantic = qdrant.search(vector, limit=PERSONALIZATION_CANDIDATE_LIMIT)
reranked = reranker.score(profile.compiled_text, candidates)
personal = 0.40·semantic + 0.25·reranker + 0.25·global + 0.10·interests
filter(personal >= profile.min_personal_score) → top N
explain(light LLM)                          # роль explainer
digest = digests.create(status='ready')
delivery_jobs.enqueue(digest, channel=personal|group)   ← флоу здесь заканчивается
```

Флоу **не отправляет**. Его результат — строки в `delivery_jobs` со статусом
`queued`. Это и есть «списки на отправки».

### 5.3 `delivery-personal` и `delivery-group`

Два независимых воркера, потому что у них разные лимиты, разный формат и
разные последствия падения. Оба крутят один и тот же цикл:

```
claim = SELECT ... FROM delivery_jobs
        WHERE channel = :channel AND status = 'queued'
          AND scheduled_at <= now()
        ORDER BY priority DESC, scheduled_at
        LIMIT :batch FOR UPDATE SKIP LOCKED       # очередь без Redis
for job in claim:
    render(digest, format=subscriber.delivery_format)
    for message in messages:
        send with rate-limit(global 25/s, chat 1/s, group 18/min)
        delivery_messages.insert(telegram_message_id, status)
    job.status = 'sent' | 'failed'
    on 429 → retry_after; on 403/kicked → subscriber.status='blocked' + chat_event
```

Ретраи: до `DELIVERY_MAX_ATTEMPTS` с экспоненциальным backoff. Каждое отправленное
сообщение — строка в `delivery_messages` с `telegram_message_id`, что даёт
и логи, и возможность потом отредактировать/удалить сообщение.

Очередь на PostgreSQL `FOR UPDATE SKIP LOCKED` — сознательный выбор: Redis
как отдельный компонент не окупается при таких объёмах, а транзакционная
целостность с дайджестами достаётся бесплатно.

### 5.4 `chat-monitor`

Раз в 6 часов проходит по всем `subscribers` типа group/channel, дёргает
`getChat` / `getChatMember`, обновляет `chat_memberships` (статус бота, права
на постинг, число участников) и пишет `chat_events`. Опрос нужен потому, что
Telegram сообщает о выходе бота ровно один раз — апдейтом `my_chat_member`.
Если сервис в этот момент лежал, чат навсегда остался бы «живым», и каждая
рассылка билась бы о `Forbidden`. Правда о правах бота живёт в Telegram, а не
у нас. Плюс realtime-обновление
из апдейта `my_chat_member`: бота добавили в чат — подписчик создаётся
автоматически (если `TELEGRAM_AUTO_REGISTER_CHATS=true`) со статусом
`pending`, и он появляется в админке на модерацию.

### 5.5 `maintenance` и сроки хранения

Агрегаты живут долго и весят мало, сырые события — наоборот.
`harvest_decisions` растёт быстрее всех: при `HARVEST_STORE_REJECTED=true` туда
попадает каждый отклонённый материал, а отклоняется большинство. Это правильно —
без отказов нечем калибровать пороги, — но хранить их вечно незачем.

`metrics/purge.py` описывает правила таблицей: что чистим, по какой колонке и
на основании какой настройки. `HARVEST_DECISION_RETENTION_DAYS` отвечает за
журнал гейта, `METRICS_RETENTION_DAYS` — за события подписчиков, лог вызовов
LLM, события чатов и логи отправленных сообщений. Опечатку в имени колонки
ловит тест против метаданных ORM: `DELETE` по несуществующей колонке иначе
всплыл бы ночью на проде.

## 6. Реестр моделей

Требование: дефолты из `.env`, но можно добавлять свои модели по API и
настраивать reasoning отдельно для лёгких и тяжёлых действий.

Три уровня:

1. **`llm_providers`** — эндпоинт + ключ + тип протокола
   (`openai_compatible` | `anthropic` | `custom`). Ключи провайдеров,
   добавленных в админке, шифруются Fernet-ключом из `SECRET_ENCRYPTION_KEY`.
2. **`llm_models`** — конкретная модель провайдера: имя, поддержка reasoning,
   стиль параметра reasoning (`openai_effort` → `reasoning_effort: low|high|max`,
   `anthropic_effort` → `reasoning.effort`, `thinking_budget`, `none`),
   контекст, лимиты, цены.
3. **`llm_role_bindings`** — привязка **роли** к модели с параметрами.
   Роли (действия) заданы явно:

   | роль | класс | где используется |
   |---|---|---|
   | `ranker` | light | глобальный скоринг материала (Stage 3) |
   | `explainer` | light | персональное объяснение «почему это тебе» |
   | `profile_compiler` | light | сборка `compiled_text` профиля из описания и интересов |
   | `query_expander` | light | расширение harvest-запросов и `/search` |
   | `digest_writer` | light | вводка дайджеста, группировка |
   | `analyzer` | heavy | глубокий разбор (Stage 4) |
   | `deep_dive` | heavy | ответ на кнопку «разобрать глубже» |
   | `chat` | heavy | свободные вопросы в боте |

   У каждой привязки свои `temperature`, `max_tokens`, `reasoning_effort`,
   `json_mode`, `timeout`, `fallback_model_id`. То есть «ризонинг на действия
   лёгкой модели и тяжёлой отдельно» настраивается не двумя тумблерами, а
   по каждой роли.

Разрешение при вызове: `llm_role_bindings` → если пусто, `LIGHT_*`/`HEAVY_*`
из env. Каждый вызов пишется в `llm_call_log` (токены, reasoning-токены,
латентность, стоимость) — админка показывает расход по ролям и по дням.

Сейчас в env обе модели — `deepseek-v4-flash` (light: `reasoning_effort=low`,
heavy: `high`). Переключить heavy на `deepseek-v4-pro` — одна строка в админке,
без деплоя.

**Открытый вопрос:** DeepSeek не отдаёт embeddings. Векторная часть остаётся
на Qwen3 (local или отдельный inference-эндпоинт); в `.env` по умолчанию
стоит локальная `Qwen3-Embedding-0.6B` (1024 dim) как самый лёгкий вариант.

## 6.1 Метрики

Состояние отвечает на вопрос «как сейчас», но не на «что изменилось» — а
именно второй вопрос задают админке. Поэтому вводится событийный лог
`subscriber_activity` (по строке на осмысленное действие подписчика) и шесть
таблиц суточных агрегатов, которые почасово пересчитывает флоу
`metrics-rollup`.

Без лога событий нельзя посчитать ни DAU, ни удержание когорт:
`last_seen_at` знает только последний раз. Без агрегатов дашборд начнёт
сканировать сырьё на каждое открытие.

Роллап идемпотентен и всегда пересчитывает последние три дня: поздние данные
(доставка, дозагруженные цитирования) попадают в статистику, а история не
переписывается бесконечно. Каждый прогон пишется в `metrics_rollup_runs` —
без этого «график встал» и «график правдиво показывает ноль» выглядят
одинаково.

Отдельно от продуктовых метрик живёт Prometheus-экспорт на `/metrics`:
латентность, ошибки, длина очереди доставки. Это для алертов, а не для
админки.

Подробности — [analytics.md](analytics.md): каталог метрик, формулы, спецификации
графиков и провалидированная палитра.

## 7. Настройки: env → БД → админка

`app_settings(key, value, env_default, is_secret, scope)` хранит эффективное
значение любой настройки. Правило разрешения:

```
effective = app_settings.value  ?? env  ?? код
```

Админка показывает три колонки: значение из env, текущее переопределение,
эффективное — и кнопку «вернуть к env». Строго env-only и в БД не попадают:
`DATABASE_URL`, `ADMIN_*`, `SECRET_ENCRYPTION_KEY`, `PREFECT_API_*`.

Изменение настройки инвалидирует кэш через `pg_notify('settings_changed')`,
слушают api, bot и воркеры — перезапуск не нужен.

## 8. Аутентификация админки

Логин и пароль — из `.env` (`ADMIN_USERNAME` / `ADMIN_PASSWORD`, либо
`ADMIN_PASSWORD_HASH` argon2 для production; hash приоритетнее). Таблицы
администраторов нет.

`POST /api/admin/auth/login` → access JWT (12 ч) + refresh (14 дней),
rate-limit 5 попыток/мин на IP. Все мутирующие вызовы пишутся в
`admin_audit_log` с before/after.

## 9. Слои кода

```
src/geonexa_proxima/
├── domain.py              # доменные типы (+ Subscriber, Subscription, DeliveryJob…)
├── ports.py               # протоколы
├── config.py              # Settings: единственная точка чтения env
├── settings_store.py      # NEW: разрешение env → app_settings, кэш, pg_notify
├── harvest/               # NEW
│   ├── profile.py         # загрузка harvest-профиля из БД/YAML
│   ├── matcher.py         # keyword gate: satisfy-выражение, score, matched terms
│   ├── query_planner.py   # harvest_queries → запросы конкретных провайдеров
│   └── seed.py            # YAML ⇄ БД
├── collectors/            # arXiv, OpenAlex, Crossref, GitHub, S2, HF (+ курсоры)
├── ml/                    # embeddings, rerankers
├── llm/
│   ├── registry.py        # NEW: провайдеры/модели/роли из БД, Fernet
│   └── providers.py       # клиенты по протоколам, reasoning-параметры
├── db/                    # модели, репозитории
├── services/              # ingestion, personalization, digest, delivery, subscriptions
├── delivery/              # NEW: очередь, рендереры personal/group, rate limiter
├── telegram/              # бот, роутеры, chat-мониторинг
├── metrics/               # NEW
│   ├── rollups.py         # суточные агрегаты, идемпотентный UPSERT
│   ├── retention.py       # недельные когорты
│   └── prometheus.py      # операционные метрики на /metrics
├── api/
│   ├── app.py
│   └── admin/             # NEW: роутеры админки, включая analytics
├── workflows/             # Prefect: harvest, dispatch, subscriber_digest, delivery_*, monitor, metrics, maintenance
└── cli.py
admin-ui/                  # NEW: SvelteKit + TypeScript (см. docs/admin.md)
```

Доменное ядро по-прежнему не импортирует инфраструктуру. Новое правило: флоу
не содержат бизнес-логики — только оркестрацию сервисов, чтобы всё
тестировалось без Prefect.

## 10. Что меняется в существующем коде

| Файл | Изменение |
|---|---|
| `db/models.py` | `users`→`subscribers` (+kind, chat_id), `user_profiles`→`subscriber_profiles`; 18 новых таблиц |
| `db/user_repository.py` | переименование в `subscriber_repository.py`, kind-aware выборки |
| `config.py` | ~45 новых полей (admin, prefect, delivery, harvest, reasoning) |
| `collectors/*` | приём готового query от `query_planner`, поддержка курсоров |
| `services/ingestion.py` | вставка keyword-gate перед эмбеддингами, запись `harvest_decisions` |
| `workflows/*` | разбиение на десять флоу вместо двух |
| `llm/providers.py` | `reasoning_effort`, выбор модели через registry, `llm_call_log` |
| `telegram/bot.py` | `my_chat_member`, регистрация чатов, команды для групп |
| `api/app.py` | подключение `admin` роутеров, CORS, Prometheus |
| `db/session.py` | TLS-контекст для управляемой БД, запрет `sslmode` в DSN |
| `db/ssl_support.py` | новый: режимы TLS → `ssl.SSLContext` для asyncpg |
| `migrations/env.py` | тот же TLS-контекст для alembic |

## 11. Порядок внедрения

1. Миграция схемы + сидирование harvest-профиля и llm-реестра. *(схема готова)*
2. `harvest/matcher.py` + тесты на реальных заголовках — это самая дешёвая
   точка, где качество корпуса решается.
3. Переработка `global-harvest`, запись `harvest_decisions`.
4. Подписчики/подписки/чаты + `chat-monitor`.
5. `delivery_jobs` + два воркера.
6. Лог активности и роллапы метрик — заводить одновременно с доставкой,
   иначе первые недели работы останутся без статистики навсегда.
7. Admin API, включая аналитику.
8. admin-ui на SvelteKit.
9. Калибровка порогов на накопленных `harvest_decisions`.

## 12. Документы

- [db-schema.md](db-schema.md) — схема БД, 24 новые таблицы, порядок миграций;
- [analytics.md](analytics.md) — метрики, роллапы, графики, палитра;
- [admin.md](admin.md) — admin API и админка на SvelteKit;
- [operations.md](operations.md) — эксплуатация: dev-режим, автоподъём схемы,
  бюджет соединений, запуск флоу;
- [design.md](design.md) — дизайн-код админки.
