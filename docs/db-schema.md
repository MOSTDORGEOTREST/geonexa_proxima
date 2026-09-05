# Схема БД Проксимы

PostgreSQL 16. Всё на UUID PK, `timestamptz`, JSONB для гибких полей.
Миграции: `0003_platform_v2.py` (платформа), `0004_metrics.py` (метрики),
`0005_pgvector.py` (векторы), `0006_profile_facets.py` (грани профиля),
`0007_facet_text_hash.py` (отпечаток текста грани), `0008_daily_harvest.py`
(верхняя граница окна прогона и расписания), `0009_russian_sources_bilingual_profiles.py`
(источники `cyberleninka` и `oai` в CHECK-ограничениях `source`; у
`subscriber_profiles` — `description_en` и `translation_source_hash`).

Служебные поля в `harvest_runs.stats` (JSONB): `heartbeat_at`, `days_done`,
`days_planned` — отметка живого прогона после каждых суток; по ней
`reclaim_stale_runs` отличает долгий сбор от брошенного.

Основная БД — управляемый PostgreSQL 16 в Timeweb Cloud, TLS `verify-full`.
`sslmode` в DSN не передаётся: asyncpg его не понимает, режим задаётся
`DATABASE_SSL_MODE`, а TLS-контекст собирается в `db/ssl_support.py`.

## Карта

```
                     ┌──────────── КОРПУС (общий) ────────────┐
  harvest_profiles ──┤ items ── item_sources                  │
    ├ harvest_term_groups      ├ item_authors ── authors      │
    │   └ harvest_terms        ├ item_topics ── topics        │
    ├ harvest_queries          ├ item_repositories ── repositories
    │   └ source_cursors       └ item_datasets ── datasets    │
    └ harvest_runs ── harvest_decisions ─────────────────────┘
                                    │
                     ┌──────────── ПОДПИСЧИКИ ────────────────┐
  subscribers ───────┤ subscriber_profiles                    │
    ├ chat_memberships    ├ profile_interests                 │
    ├ chat_events         ├ profile_interest_signals          │
    ├ subscriptions       ├ profile_item_scores ── items      │
    │   └ subscription_events   └ feedback                    │
    └ schedules ─────────────────────────────────────────────┘
                                    │
                     ┌──────────── ДОСТАВКА ──────────────────┐
  digests ── digest_items ── delivery_jobs ── delivery_messages│
                     └────────────────────────────────────────┘

  llm_providers ── llm_models ── llm_role_bindings ── llm_call_log
  app_settings · admin_audit_log · flow_runs

                     ┌──────────── МЕТРИКИ (0004) ────────────┐
  subscriber_activity ── metrics_subscribers_daily             │
                      ├─ metrics_retention                     │
  harvest_decisions ──── metrics_harvest_daily                 │
  delivery_messages ──── metrics_delivery_daily                │
  feedback ───────────── metrics_engagement_daily              │
  llm_call_log ───────── llm_usage_daily                       │
                         metrics_rollup_runs ──────────────────┘
```

---

## 1. Корпус — что меняется

Существующие `items`, `item_sources`, `authors`, `item_authors`, `topics`,
`item_topics`, `repositories`, `datasets` остаются как есть. Добавляются
колонки в `items`:

| колонка | тип | смысл |
|---|---|---|
| `keyword_score` | `float` | результат Stage 1, [0,1] |
| `matched_terms` | `jsonb` | `{"geo_domain":["liquefaction"],"ai_method":["PINN"]}` |
| `harvest_profile_id` | `uuid` | по какому профилю принят |
| `gate_stage` | `text` | `keyword` / `semantic` / `manual` |
| `language` | `text` | детект языка |
| `is_preprint` | `bool` | |
| `retracted_at` | `timestamptz` | |
| `content_hash` | `text` | sha256(normalized_title + doi) для быстрой дедупликации |

`collection_runs` заменяется на `harvest_runs` (шире и привязан к Prefect).

---

## 2. Harvest

### `harvest_profiles`
```
id uuid pk
key text unique not null            -- 'geo_ai_core'
name text not null
description text
version int not null default 1      -- растёт при любой правке терминов
is_active bool not null default false
satisfy_expr text not null          -- 'geo_domain and (ai_method or geo_sensing) and not hard_exclude'
keyword_score_threshold float not null default 0.35
borderline_semantic_threshold float not null default 0.52
languages text[] not null default '{en,ru}'
item_kinds text[] not null default '{paper,method,software,dataset}'
config jsonb not null default '{}'  -- funnel, приоритетные журналы и пр.
created_at, updated_at timestamptz
```
`unique index where is_active` — активный профиль ровно один.

### `harvest_term_groups`
```
id uuid pk
harvest_profile_id uuid fk -> harvest_profiles on delete cascade
key text not null                   -- 'geo_domain'
name text
mode text not null check in ('any_of','all_of','none_of')
min_matches int not null default 1 check >= 0
fields text[] not null default '{title,abstract,keywords}'
weight float not null default 0 check between 0 and 1
is_hard bool not null default false      -- для none_of: блокирует безусловно
penalty float not null default 0         -- для мягких стоп-слов
affects_satisfy bool not null default true
enabled bool not null default true
position int not null default 0
unique (harvest_profile_id, key)
```

### `harvest_terms`
```
id uuid pk
group_id uuid fk -> harvest_term_groups on delete cascade
term text not null
normalized_term text not null            -- lower, схлопнутые пробелы
match_type text not null check in ('phrase','token','prefix','regex')
lang text                                -- 'en' | 'ru' | null
weight float not null default 1.0
enabled bool not null default true
hit_count bigint not null default 0      -- сколько раз сработал: для чистки мусорных терминов
last_hit_at timestamptz
unique (group_id, normalized_term, match_type)
index (group_id) where enabled
```
`hit_count` — практическая вещь: через месяц видно, какие термины не сработали
ни разу и какие тянут шум.

### `harvest_queries`
```
id uuid pk
harvest_profile_id uuid fk
source text not null check in ('arxiv','openalex','crossref','semantic_scholar','github','huggingface')
key text not null                        -- 'ax_geo_ai'
query text not null
params jsonb not null default '{}'       -- categories, filters, min_stars…
priority int not null default 5
max_items int not null default 200
lookback_hours int
enabled bool not null default true
last_run_at timestamptz
last_stats jsonb not null default '{}'   -- {fetched, accepted, rejected}
unique (harvest_profile_id, source, key)
```

### `source_cursors`
```
id uuid pk
harvest_query_id uuid fk -> harvest_queries on delete cascade
cursor jsonb not null default '{}'       -- {"next_cursor":"...","last_date":"..."}
last_external_id text
last_success_at timestamptz
unique (harvest_query_id)
```
Позволяет доливать историю и переживать падения без перезапуска с нуля.

### `harvest_runs`
```
id uuid pk
harvest_profile_id uuid fk
prefect_flow_run_id text                 -- связь с Prefect UI
trigger text not null check in ('schedule','manual','api','backfill')
status text not null check in ('running','succeeded','failed','cancelled')
started_at timestamptz not null default now()
finished_at timestamptz
since timestamptz
stats jsonb not null default '{}'        -- полная воронка по стадиям и источникам
error text
triggered_by text                        -- 'admin:geonexa_proxima_admin' | 'schedule'
index (status, started_at desc)
unique index where status='running'      -- один глобальный сбор одновременно
```

### `harvest_decisions`
```
id bigserial pk                          -- большой объём, bigint дешевле uuid
harvest_run_id uuid fk -> harvest_runs on delete cascade
source text not null
external_id text not null
item_id uuid fk -> items on delete set null
stage text not null check in ('keyword','semantic','llm','dedup')
decision text not null check in ('accepted','borderline','rejected','duplicate')
keyword_score float
semantic_score float
matched_terms jsonb not null default '{}'
blocked_by text                          -- key группы, которая отсекла
title text                               -- чтобы админка не джойнила с items для rejected
reason text
created_at timestamptz not null default now()
index (harvest_run_id, decision)
index (created_at)
index (decision, created_at desc)
```
Ретеншен `HARVEST_DECISION_RETENTION_DAYS` (90), чистит `maintenance`.
Это главный источник данных для калибровки порогов.

---

## 3. Подписчики

### `subscribers` (переименование `users` + расширение)
```
id uuid pk
kind text not null check in ('user','group','channel')
telegram_chat_id bigint not null unique  -- было external_user_id
telegram_user_id bigint                  -- для kind='user' совпадает с chat_id
username text
title text                               -- имя человека или название чата
language_code text
status text not null check in ('pending','active','paused','blocked','left')
is_owner bool not null default false     -- из TELEGRAM_OWNER_IDS
added_by_subscriber_id uuid fk -> subscribers  -- кто добавил бота в чат
timezone text not null default 'Europe/Moscow'
notes text                               -- заметки админа
meta jsonb not null default '{}'
first_seen_at, last_seen_at, created_at, updated_at timestamptz
index (kind, status)
index (status) where status='active'
```
`pending` — бота добавили в чат, но админ ещё не подтвердил. При
`TELEGRAM_REGISTRATION_MODE=open` регистрация сразу в `active`.

### `subscriber_profiles` (переименование `user_profiles` + расширение)
Всё из v1 плюс:
```
schedule_id uuid fk -> schedules
timezone text
delivery_format text not null default 'cards'  check in ('cards','compact','single_message','digest_post')
max_items int not null default 20 check between 1 and 100
min_personal_score float not null default 0.5 check between 0 and 1
min_global_score float                        -- override DIGEST_SCORE_THRESHOLD
kinds text[] not null default '{paper,method,software,dataset}'
quiet_hours jsonb not null default '{}'       -- {"from":"23:00","to":"08:00"}
last_digest_at timestamptz
next_digest_at timestamptz
paused_until timestamptz
```
`delivery_format='digest_post'` — один длинный пост, формат для каналов.

`profile_interests`, `profile_interest_signals`, `profile_item_scores`,
`feedback` — без изменений, кроме переименования FK.

### Kind-aware выборки: `db/subscriber_repository.py`

Человек, группа и канал лежат в одной таблице, и это правильно: у них общий
`chat_id`, общие профили, общие подписки и общая доставка. Но почти любой
вопрос к этой таблице задаётся про конкретный вид, поэтому в репозитории
`kinds` — обязательный параметр с явным значением по умолчанию, а не фильтр,
который легко забыть.

| Метод | Про что |
|---|---|
| `list_subscribers(kinds=…, statuses=…, search=…, with_active_subscription=…)` | Постраничный список для админки |
| `count_subscribers(...)` | То же число без выборки строк |
| `breakdown()` | Разрез «вид × статус» — верх дашборда |
| `list_delivery_targets(kinds=…)` | Активные с действующей подпиской; для чатов ещё и с ботом внутри |
| `register_chat(ChatIdentity, bot_status=…)` | Идемпотентная регистрация группы или канала |
| `update_bot_status(chat_id, status)` | Смена прав бота; при `kicked`/`left` гасит подписчика и его дайджесты |
| `list_chats(present_only=…)` | Чаты вместе с правами бота и числом профилей |
| `grant / extend / cancel_subscription` | Подписки с событиями в `subscription_events` |
| `expire_due()`, `list_expiring(within=…)` | Гашение просроченных и напоминания |
| `limits(subscriber_id)` | Действующие лимиты: из подписки либо из тарифа по умолчанию |

Три правила, которые репозиторий держит за вызывающего:

1. **Личный чат нельзя завести как группу.** `register_chat` принимает только
   отрицательные `chat_id` видов `group`/`channel`; `private` — ошибка, а не
   молчаливая запись не туда.
2. **Вторая подписка поверх первой — ошибка, а не операция.** В БД стоит
   `EXCLUDE USING gist` на пересечение действующих периодов. `grant_subscription`
   либо закрывает предыдущую (`replace_current=True`, по умолчанию), либо падает
   с `SubscriptionOverlapError`. Тариф без `allow_group_chats` не выдаётся чату.
3. **Канал без права постить — не адресат.** `ChatRecord.can_deliver` разводит
   «бот в чате» и «боту есть чем писать»: в группе достаточно членства, в
   канале нужен `can_post_messages`.

Живая проверка против настоящего PostgreSQL — `scripts/check_subscribers.py`:
заводит временных подписчика, группу, канал и пару тарифов с меткой прогона,
прогоняет весь сценарий (регистрация, смена прав бота, выдача и замена
подписки, истечение, выборка диспетчера) и убирает за собой.


---

## 4. Подписки

### `subscription_plans`
```
id uuid pk
key text unique not null                 -- 'free' | 'pro' | 'team'
name text not null
description text
max_profiles int not null default 1
max_items_per_digest int not null default 20
min_interval_hours int not null default 168   -- как часто можно получать дайджест
deep_analysis_quota_per_month int not null default 0
allow_group_chats bool not null default false
features jsonb not null default '{}'
is_default bool not null default false
enabled bool not null default true
```

### `subscriptions`
```
id uuid pk
subscriber_id uuid fk -> subscribers on delete cascade
plan_id uuid fk -> subscription_plans
status text not null check in ('pending','trial','active','expired','cancelled')
starts_at timestamptz not null           -- «начало действия»
ends_at timestamptz                      -- null = бессрочная
grace_until timestamptz
auto_renew bool not null default false
source text not null default 'admin' check in ('admin','trial','import','payment')
price_amount numeric(12,2)               -- опционально, если появится оплата
price_currency text
external_payment_id text
notes text
created_by text                          -- 'admin:<username>'
created_at, updated_at timestamptz
index (subscriber_id, status)
index (ends_at) where status in ('active','trial')

EXCLUDE USING gist (
  subscriber_id WITH =,
  tstzrange(starts_at, coalesce(ends_at,'infinity')) WITH &&
) WHERE (status IN ('active','trial'))
```
Exclusion constraint (нужно `CREATE EXTENSION btree_gist`) не даёт двум
активным подпискам пересечься по времени — БД, а не код, гарантирует, что у
подписчика в любой момент ровно одна действующая подписка.

Проверка «имеет ли право получать дайджест прямо сейчас»:
```sql
SELECT 1 FROM subscriptions
WHERE subscriber_id = :id AND status IN ('active','trial')
  AND starts_at <= now()
  AND (ends_at IS NULL OR coalesce(grace_until, ends_at) >= now());
```

### `subscription_events`
```
id uuid pk
subscription_id uuid fk on delete cascade
event text not null check in ('created','activated','extended','downgraded','upgraded','expired','cancelled','reminded')
payload jsonb not null default '{}'
actor text
created_at timestamptz not null default now()
```

---

## 5. Чаты и каналы

### `chat_memberships`
```
id uuid pk
subscriber_id uuid fk -> subscribers on delete cascade   -- kind in ('group','channel')
bot_status text not null check in ('creator','administrator','member','restricted','left','kicked')
can_post_messages bool
can_edit_messages bool
can_delete_messages bool
member_count int
chat_type text                          -- 'group'|'supergroup'|'channel'
invite_link text
added_by_user_id bigint
added_at timestamptz
removed_at timestamptz
last_checked_at timestamptz
error text                              -- последняя ошибка getChat
unique (subscriber_id)
index (bot_status)
```

### `chat_events`
```
id bigserial pk
subscriber_id uuid fk on delete cascade
event_type text not null                -- 'bot_added','bot_removed','promoted','demoted','title_changed','migrated','permissions_changed'
old_value jsonb, new_value jsonb
raw_update jsonb
occurred_at timestamptz not null default now()
index (subscriber_id, occurred_at desc)
```

---

## 6. Доставка

### `digests` (расширение)
```
+ subscriber_id uuid fk -> subscribers
+ kind text not null default 'personal' check in ('personal','group','broadcast')
+ schedule_id uuid fk -> schedules
+ prefect_flow_run_id text
+ item_count int not null default 0
+ error text
status check in ('pending','building','ready','queued','sent','partial','failed','skipped')
```

### `digest_items` (расширение)
```
+ personal_score float
+ global_score float
+ profile_score_id uuid fk -> profile_item_scores
```

### `delivery_jobs` — «списки на отправки»
```
id uuid pk
digest_id uuid fk -> digests on delete cascade
subscriber_id uuid fk -> subscribers on delete cascade
channel text not null check in ('personal','group')     -- какой воркер берёт
target_chat_id bigint not null
status text not null check in ('queued','claimed','sending','sent','failed','skipped','cancelled')
priority int not null default 0
attempts int not null default 0
max_attempts int not null default 5
scheduled_at timestamptz not null default now()
claimed_at, started_at, finished_at timestamptz
claimed_by text                                          -- id воркера
next_retry_at timestamptz
last_error text
payload jsonb not null default '{}'                      -- отрендеренные блоки
prefect_flow_run_id text
created_at, updated_at timestamptz

index (channel, status, scheduled_at) where status = 'queued'
index (subscriber_id, created_at desc)
unique (digest_id, target_chat_id)
```
Выборка воркером — `FOR UPDATE SKIP LOCKED`, что делает таблицу полноценной
очередью без Redis.

### `delivery_messages` — логи рассылок
```
id bigserial pk
delivery_job_id uuid fk -> delivery_jobs on delete cascade
item_id uuid fk -> items on delete set null
chat_id bigint not null
telegram_message_id bigint
position int not null default 0
status text not null check in ('sent','failed','skipped','deleted','edited')
attempt int not null default 1
error_code int                          -- HTTP код Telegram
error text
retry_after int                         -- из 429
text_preview text                       -- первые 200 символов, чтобы видеть что ушло
sent_at timestamptz
index (delivery_job_id)
index (chat_id, sent_at desc)
index (status, sent_at desc)
```

---

## 7. Расписания и Prefect

### `schedules`
```
id uuid pk
key text unique not null
name text not null
kind text not null check in ('global_harvest','digest_dispatch','subscriber_digest','delivery_personal','delivery_group','chat_monitor','maintenance')
subscriber_profile_id uuid fk -> subscriber_profiles on delete cascade  -- для персональных
prefect_deployment_id text
prefect_schedule_id text
cron text                               -- взаимоисключающе с interval_seconds
interval_seconds int
timezone text not null default 'Europe/Moscow'
anchor_date timestamptz
enabled bool not null default true
parameters jsonb not null default '{}'
last_run_at, next_run_at timestamptz
created_at, updated_at timestamptz
check (cron is not null or interval_seconds is not null)
index (kind, enabled)
index (next_run_at) where enabled
```
Админка правит `schedules`; сервис пушит изменение в Prefect REST API и
сохраняет `prefect_schedule_id`. Prefect остаётся источником истины по
исполнению, БД — по намерению.

### `flow_runs` — локальное зеркало
```
id uuid pk
prefect_flow_run_id text unique not null
flow_name text not null
kind text
schedule_id uuid fk -> schedules on delete set null
subscriber_id uuid fk -> subscribers on delete set null
state text not null                     -- Prefect state name
started_at, finished_at timestamptz
duration_seconds float
stats jsonb not null default '{}'
error text
index (kind, started_at desc)
index (state)
```
Чтобы дашборд админки не ходил в Prefect API на каждый чих.

---

## 8. Модели

### `llm_providers`
```
id uuid pk
key text unique not null                -- 'deepseek'
name text not null
protocol text not null check in ('openai_compatible','anthropic','custom')
base_url text not null
api_key_encrypted bytea                 -- Fernet, ключ из SECRET_ENCRYPTION_KEY
api_key_env_var text                    -- альтернатива: имя переменной окружения
default_headers jsonb not null default '{}'
enabled bool not null default true
is_managed_by_env bool not null default false   -- строка засеяна из .env
created_at, updated_at timestamptz
```

### `llm_models`
```
id uuid pk
provider_id uuid fk -> llm_providers on delete cascade
key text not null                       -- 'deepseek-v4-flash'
model_name text not null                -- что уходит в API
display_name text
tier text check in ('light','heavy','both')
supports_reasoning bool not null default false
reasoning_style text not null default 'none'
  check in ('none','openai_effort','anthropic_effort','thinking_budget')
reasoning_levels text[] default '{low,high,max}'
supports_json_mode bool not null default true
supports_tools bool not null default false
context_window int
max_output_tokens int
input_price_per_1m numeric(10,4)
output_price_per_1m numeric(10,4)
enabled bool not null default true
unique (provider_id, key)
```

### `llm_role_bindings`
```
id uuid pk
role text unique not null
  check in ('ranker','explainer','profile_compiler','query_expander','digest_writer','analyzer','deep_dive','chat')
model_id uuid fk -> llm_models on delete restrict
fallback_model_id uuid fk -> llm_models on delete set null
temperature float not null default 0.1 check between 0 and 2
top_p float
max_tokens int
reasoning_effort text check in ('none','low','high','max')
json_mode bool not null default true
timeout_seconds int not null default 180
concurrency int not null default 4
system_prompt_override text
enabled bool not null default true
updated_by text
updated_at timestamptz
```

### `llm_call_log`
```
id bigserial pk
role text not null
model_id uuid fk -> llm_models on delete set null
item_id uuid fk -> items on delete set null
subscriber_id uuid fk -> subscribers on delete set null
prompt_tokens, completion_tokens, reasoning_tokens int
cost_usd numeric(12,6)
latency_ms int
status text not null check in ('ok','error','timeout','rate_limited')
error text
created_at timestamptz not null default now()
index (role, created_at desc)
index (created_at)
```
Ретеншен 30 дней + суточная агрегация в `llm_usage_daily` (role, model, date,
calls, tokens, cost).

---

## 9. Настройки и аудит

### `app_settings`
```
key text pk                             -- 'DIGEST_SCORE_THRESHOLD'
value jsonb not null
value_type text not null check in ('string','int','float','bool','json','secret')
env_default jsonb                       -- что было в .env на момент старта
scope text not null default 'general'   -- 'harvest'|'delivery'|'llm'|'telegram'|'general'
description text
is_secret bool not null default false
is_env_only bool not null default false -- нельзя переопределить (DATABASE_URL, ADMIN_*)
updated_by text
updated_at timestamptz
```
Изменение шлёт `pg_notify('geonexa_settings', key)`.

### `admin_audit_log`
```
id bigserial pk
actor text not null                     -- ADMIN_USERNAME
action text not null                    -- 'update','create','delete','run','login','login_failed'
entity_type text not null
entity_id text
before jsonb, after jsonb
ip inet
user_agent text
created_at timestamptz not null default now()
index (created_at desc)
index (entity_type, entity_id)
```

---

## 10. Миграция 0003 — порядок

```
1.  CREATE EXTENSION IF NOT EXISTS btree_gist;
2.  ALTER TABLE users RENAME TO subscribers;
    ALTER TABLE subscribers RENAME COLUMN external_user_id TO telegram_chat_id;
    ALTER TABLE subscribers RENAME COLUMN display_name TO title;
    + kind (default 'user', потом NOT NULL), telegram_user_id, is_owner,
      added_by_subscriber_id, timezone, notes, first_seen_at
    UPDATE subscribers SET telegram_user_id = telegram_chat_id, kind = 'user';
    статусы: 'inactive' -> 'paused'
3.  ALTER TABLE user_profiles RENAME TO subscriber_profiles;
    ALTER TABLE subscriber_profiles RENAME COLUMN user_id TO subscriber_id;
    + schedule_id, delivery_format, max_items, min_personal_score, kinds,
      quiet_hours, last_digest_at, next_digest_at, paused_until
4.  digests / feedback: user_id -> subscriber_id
5.  items: + keyword_score, matched_terms, harvest_profile_id, gate_stage,
      language, is_preprint, retracted_at, content_hash
6.  CREATE TABLE ... (harvest_*, subscription_*, chat_*, delivery_*, llm_*,
      schedules, flow_runs, app_settings, admin_audit_log)
7.  DROP TABLE collection_runs  (данных нет, заменяется harvest_runs)
8.  Сидирование: subscription_plans(free), harvest-профиль из config/harvest.yaml,
      llm_providers/llm_models/llm_role_bindings из .env, schedules из SCHEDULE_*_CRON,
      app_settings из .env
```

`downgrade()` реализуется зеркально; таблицы v2 просто дропаются.

Проект на первом коммите и без production-данных, поэтому переименование
выбрано вместо параллельной иерархии — «пользователь» и «чат» это одна
сущность, и вводить супертип поверх `users` значило бы тащить компромисс
навсегда.

---

## 11. Метрики (миграция 0004)

### `subscriber_activity`
```
id bigserial pk
subscriber_id uuid fk -> subscribers on delete cascade
profile_id uuid fk -> subscriber_profiles on delete set null
kind text check in ('registered','command','search','feedback','digest_received',
                    'link_click','profile_edit','deep_dive','subscription_changed',
                    'blocked_bot','chat_joined','chat_left')
item_id uuid fk -> items on delete set null
digest_id uuid fk -> digests on delete set null
payload jsonb not null default '{}'
occurred_at timestamptz not null default now()

index (subscriber_id, occurred_at desc)
index (kind, occurred_at desc)
index (occurred_at, subscriber_id)     -- range scan + count(distinct) без heap
```
Событийный лог: без него DAU и удержание когорт не восстановить —
`last_seen_at` знает только последний раз. Индекса по выражению-дате нет
намеренно: день считается в `METRICS_TIMEZONE`, и при смене зоны такой индекс
молча перестал бы использоваться.

### Суточные роллапы

| Таблица | Уникальность | Что внутри |
|---|---|---|
| `metrics_harvest_daily` | (day, source) | fetched, accepted, borderline, rejected, duplicates, rescued_by_semantic, ranked, analyzed, stored, avg_keyword_score, avg_global_score, top_blocked_by |
| `metrics_subscribers_daily` | (day, kind) | registered, activated, churned, blocked, total, total_active, with_subscription, dau, wau, mau, digest_enabled_profiles |
| `metrics_delivery_daily` | (day, channel) | jobs_created/sent/failed/skipped, messages_sent/failed, rate_limited, recipients, avg/p95_queue_seconds, top_errors |
| `metrics_engagement_daily` | (day) | digests_sent, items_delivered, фидбек по видам, unique_reactors, empty_digests, engagement_rate, avg_items_per_digest, avg_personal_score |
| `metrics_retention` | (cohort_week, week_offset, kind) | cohort_size, retained, retention_rate |
| `metrics_rollup_runs` | — | scope, day_from, day_to, status, duration_seconds, rows_written, prefect_flow_run_id, error |

Все счётчики `integer not null default 0` — на графике ноль и отсутствие
данных должны различаться, а не сливаться в `null`.

`metrics_engagement_daily.engagement_rate` ограничен диапазоном [0,1],
`metrics_retention.retained <= cohort_size` — БД не даёт записать метрику,
которая не может быть правдой.

Роллап идемпотентен: `INSERT ... ON CONFLICT DO UPDATE` по ключу
уникальности, всегда за последние три дня.

---

## 12. Векторы (миграции 0005, 0006, 0007)

```
item_vectors
  item_id     uuid pk fk -> items on delete cascade
  embedding   vector(N) | halfvec(N)   -- N из EMBEDDING_DIMENSIONS
  model       text                     -- какой моделью посчитан
  dimensions  int
  created_at, updated_at timestamptz
  index hnsw (embedding vector_cosine_ops) with (m=16, ef_construction=64)

profile_vectors
  profile_id  uuid  ┐
  version     int   ├ pk
  facet       int   ┘  -- 0 = весь профиль, дальше его отдельные темы
  text_hash   text     -- отпечаток текста грани
  embedding   vector(N) | halfvec(N)
  model       text
  index hnsw (embedding vector_cosine_ops)
  index (profile_id)   -- для уборки прошлых версий
```

Векторов у профиля не один, а набор. Профиль из нескольких областей в одном
векторе даёт центроид между ними, и статья, глубоко попадающая в одну тему,
получает средний косинус. Поэтому грань 0 — весь профиль, а дальше идут его
темы, по которым ищут независимо; близость берётся максимумом. Версия входит в
ключ: правка описания перестраивает весь набор целиком — половина старых граней
рядом с новыми была бы хуже, чем их отсутствие.

Отпечаток (миграция 0007) — вторая половина ключа. Версии профиля мало: номер
грани позиционный, и какой текст под ним окажется, зависит ещё и от
`PROFILE_FACET_MIN_CHARS`, `PROFILE_FACET_LIMIT` и самого алгоритма разбиения, а
версия про них не знает. Без отпечатка смена настройки подсовывала бы под старым
номером чужой вектор — молча и только в поиске. Прошлые версии убираются при
первой же записи: правка профиля и каждое нажатие кнопки обратной связи поднимают
версию, и без уборки таблица росла бы на набор векторов за клик.

Миграция 0006 расширяет первичный ключ и добавляет `profile_item_scores.
matched_facet` — какой гранью материал был найден. Текстом, а не номером:
номера меняются при правке описания, и ответ на «почему это показали» после
первой же правки стал бы враньём.

Размерность зашита в тип колонки, поэтому смена модели — это миграция колонок
и переиндексация, а не тихая запись рядом. Чтобы расхождение конфигурации со
схемой было видно запросом, а не по странной выдаче поиска, миграция пишет
слепок в `app_settings`:

```sql
SELECT value FROM app_settings WHERE key = 'VECTOR_SCHEMA';
-- {"dimensions": 1024, "column_type": "vector", "index_kind": "hnsw",
--  "model": "Qwen/Qwen3-Embedding-0.6B"}
```

Индексы pgvector объявлены сырым SQL: operator class SQLAlchemy описать не
умеет. Чтобы `alembic check` из-за этого не был постоянно красным, такие
индексы исключены из сравнения в `migrations/env.py` — всё остальное
сверяется, и сейчас модели совпадают с базой один в один.

Векторы нормализованы (обрезка Matryoshka всегда сопровождается повторной
L2-нормализацией), поэтому косинусная дистанция и внутреннее произведение
эквивалентны. Косинус выбран явно: так ненормализованный вектор из чужого
источника не испортит выдачу молча.
