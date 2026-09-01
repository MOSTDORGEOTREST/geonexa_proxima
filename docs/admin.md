# Админка: API и экраны

Бэкенд — роутеры FastAPI под `/api/admin`, фронтенд — приложение
`admin-ui/` на SvelteKit + TypeScript.

> **Что реализовано.** Ниже — полный проект API. Построены и проверены против
> живой базы: вход и токены, дашборд, заявки и модерация, подписчики и
> профили (включая правку описания и тем с перекомпиляцией), чаты, тарифы и
> подписки, профиль сбора с пробой гейта, курсоры источников, расписания с
> запуском флоу, реестр моделей с привязкой ролей, настройки, доставки,
> аналитика, аудит и здоровье. Не построены: экспорт CSV, SSE для живой
> очереди, экраны корпуса и отдельного профиля, слияние дубликатов.
> Актуальный список эндпоинтов — в `/openapi.json` работающего сервиса.

## Аутентификация

Логин и пароль берутся из `.env` (`ADMIN_USERNAME` / `ADMIN_PASSWORD`; в
production — `ADMIN_PASSWORD_HASH`, argon2, приоритетнее plaintext). Таблицы
администраторов нет.

```
POST   /api/admin/auth/login      {username, password} -> {access_token, refresh_token, expires_in}
POST   /api/admin/auth/refresh    {refresh_token}
POST   /api/admin/auth/logout
GET    /api/admin/auth/me
```

Access JWT — 12 ч (`ADMIN_JWT_TTL_MINUTES`), refresh — 14 дней. Rate-limit
5 попыток входа в минуту на IP; неудачные попытки пишутся в `admin_audit_log`.
Все остальные эндпоинты требуют `Authorization: Bearer`.

Браузер этих токенов не видит. SvelteKit кладёт их в httpOnly-cookie и сам
подставляет заголовок в серверных `load` и `actions`; наружу FastAPI вообще не
торчит — ходит только node-слой по `API_INTERNAL_URL`. Отсюда и CORS почти не
нужен: он остаётся только для локальной разработки, когда `vite dev` работает
на 5173, а API на 8000.

`hooks.server.ts` держит один интерцептор: 401 от API → попытка refresh →
повтор запроса → при неудаче редирект на `/login` с `?next=`.

---

## 1. Дашборд

```
GET /api/admin/dashboard/summary
    -> {corpus: {items, new_24h, ranked, analyzed},
        harvest: {last_run, status, funnel: {fetched, accepted, borderline, rejected}},
        subscribers: {total, active, users, groups, channels, pending},
        subscriptions: {active, expiring_7d, expired},
        delivery: {queued, sent_24h, failed_24h},
        llm: {calls_24h, tokens_24h, cost_24h},
        errors: [...]}
GET /api/admin/dashboard/funnel?days=30     -- воронка по дням для графика
GET /api/admin/dashboard/timeline?days=30   -- runs + доставки на одной оси
```

---

## 2. Harvest — что мы ищем

```
GET    /api/admin/harvest/profiles
POST   /api/admin/harvest/profiles
GET    /api/admin/harvest/profiles/{id}
PATCH  /api/admin/harvest/profiles/{id}          -- в т.ч. satisfy_expr, пороги
POST   /api/admin/harvest/profiles/{id}/activate
POST   /api/admin/harvest/profiles/{id}/clone
DELETE /api/admin/harvest/profiles/{id}

GET    /api/admin/harvest/profiles/{id}/groups
POST   /api/admin/harvest/profiles/{id}/groups
PATCH  /api/admin/harvest/groups/{id}
DELETE /api/admin/harvest/groups/{id}

GET    /api/admin/harvest/groups/{id}/terms?q=&enabled=
POST   /api/admin/harvest/groups/{id}/terms      -- один или массив
POST   /api/admin/harvest/groups/{id}/terms/bulk -- вставка списком, по строке на термин
PATCH  /api/admin/harvest/terms/{id}
DELETE /api/admin/harvest/terms/{id}
GET    /api/admin/harvest/terms/stats            -- hit_count, мёртвые и шумные термины

GET    /api/admin/harvest/queries?source=
POST   /api/admin/harvest/queries
PATCH  /api/admin/harvest/queries/{id}
DELETE /api/admin/harvest/queries/{id}
POST   /api/admin/harvest/queries/{id}/preview   -- дёрнуть источник, вернуть 20 заголовков БЕЗ записи в БД

POST   /api/admin/harvest/test
    {title, abstract, keywords[]} ->
    {decision, keyword_score, satisfy_result, groups: [{key, matched: [...], satisfied}], blocked_by}
POST   /api/admin/harvest/test/batch             -- прогнать по последним N материалам корпуса
POST   /api/admin/harvest/profiles/{id}/export   -> YAML
POST   /api/admin/harvest/profiles/{id}/import   <- YAML

GET    /api/admin/harvest/runs?status=&limit=
GET    /api/admin/harvest/runs/{id}
POST   /api/admin/harvest/runs                   -- запустить вручную {since?, sources?, dry_run?}
POST   /api/admin/harvest/runs/{id}/cancel
GET    /api/admin/harvest/decisions?run_id=&decision=&source=&q=&blocked_by=&page=
GET    /api/admin/harvest/decisions/export.csv
```

`POST /harvest/test` — самый полезный эндпоинт при настройке: вставляешь
заголовок и аннотацию реальной статьи и сразу видишь, какие термины сработали,
какая группа не выполнилась и почему материал отвергли.

---

## 3. Корпус

```
GET    /api/admin/items?q=&kind=&source=&min_score=&since=&has_analysis=&page=
GET    /api/admin/items/{id}                     -- + источники, авторы, оценки, кому уходил
PATCH  /api/admin/items/{id}                     -- ручная правка kind, тем, флага retracted
DELETE /api/admin/items/{id}
POST   /api/admin/items/{id}/rescore             -- пересчитать глобальную оценку
POST   /api/admin/items/{id}/analyze             -- запустить heavy-разбор
POST   /api/admin/items/{id}/reembed
GET    /api/admin/items/duplicates               -- кандидаты на слияние
POST   /api/admin/items/merge  {keep_id, merge_ids[]}
```

---

## 4. Подписчики и модерация

Схема групп-центричная: `/start` в личке и добавление бота в чат заводят
подписчика со статусом `pending` — заявку, а не доступ. Диспетчер `pending` не
видит (`s.status = 'active'` в выборке), поэтому неподтверждённый чат не может
получить дайджест по недосмотру.

Профиль пишется прямо в карточке заявки, поэтому там же стоит подсказка о том,
как его писать, и ссылка на экран `/guide`.

Подтверждение — одно действие из четырёх шагов: статус, профиль, пробная
подписка и сообщение в чат. Разносить их по разным кнопкам нельзя: подписчик без
профиля или без действующей подписки в дайджест не попадает, и снаружи это
выглядит как молчащий бот. Шаги 2–4 не критичны по отдельности — их результат
возвращается в поле `approval`, чтобы неудача была видна, а не додумывалась.

```
GET    /api/admin/subscribers?kind=&status=&q=&has_active_subscription=&page=
GET    /api/admin/subscribers/pending            -- очередь заявок: люди и чаты одним списком
POST   /api/admin/subscribers                    -- завести вручную по chat_id
GET    /api/admin/subscribers/{id}
PATCH  /api/admin/subscribers/{id}               -- status, notes, timezone, is_owner
DELETE /api/admin/subscribers/{id}
POST   /api/admin/subscribers/{id}/approve       {notify, grant_trial, description}
POST   /api/admin/subscribers/{id}/reject        {reason} -- статус blocked, дайджесты выключены
POST   /api/admin/subscribers/{id}/block
POST   /api/admin/subscribers/{id}/message       -- отправить произвольное сообщение
GET    /api/admin/subscribers/{id}/activity      -- фидбек, дайджесты, доставки одной лентой

GET    /api/admin/subscribers/{id}/profiles
POST   /api/admin/subscribers/{id}/profiles
GET    /api/admin/profiles/{id}
PATCH  /api/admin/profiles/{id}                  -- description, digest_enabled, пороги, формат, расписание
DELETE /api/admin/profiles/{id}
POST   /api/admin/profiles/{id}/activate
POST   /api/admin/profiles/{id}/recompile        -- пересобрать compiled_text и вектор
POST   /api/admin/profiles/{id}/preview          -- сухой прогон дайджеста: что бы ушло сейчас
POST   /api/admin/profiles/{id}/run              -- построить дайджест немедленно {deliver: bool}

GET    /api/admin/profiles/guide                 -- инструкция «как писать профиль»
POST   /api/admin/profiles/preview               {description} -> темы + замечания
GET    /api/admin/profiles/{id}/preview          -- то же для сохранённого профиля

GET    /api/admin/profiles/{id}/interests
POST   /api/admin/profiles/{id}/interests        {query, polarity, weight}
DELETE /api/admin/profiles/{id}/interests/{interest_id}
PATCH  /api/admin/interests/{id}
GET    /api/admin/profiles/{id}/signals          -- выученные из фидбека
DELETE /api/admin/signals/{id}
GET    /api/admin/profiles/{id}/scores?limit=    -- последние персональные оценки
```

---

## 5. Подписки

```
GET    /api/admin/plans
POST   /api/admin/plans
PATCH  /api/admin/plans/{id}
DELETE /api/admin/plans/{id}

GET    /api/admin/subscriptions?status=&plan=&expiring_days=&subscriber_id=&page=
POST   /api/admin/subscriptions  {subscriber_id, plan_id, starts_at, ends_at, status, notes}
GET    /api/admin/subscriptions/{id}
PATCH  /api/admin/subscriptions/{id}
POST   /api/admin/subscriptions/{id}/extend      {days | until}
POST   /api/admin/subscriptions/{id}/cancel
GET    /api/admin/subscriptions/{id}/events
GET    /api/admin/subscriptions/expiring?days=7
```

Пересечение активных периодов у одного подписчика отклоняется на уровне БД
(exclusion constraint) — API возвращает 409 с текстом о конфликте дат.

---

## 6. Каналы и группы

```
GET    /api/admin/chats?bot_status=&type=&page=  -- всё, куда добавили бота
GET    /api/admin/chats/{id}
POST   /api/admin/chats/{id}/refresh             -- getChat/getChatMember прямо сейчас
POST   /api/admin/chats/{id}/leave               -- бот выходит из чата
POST   /api/admin/chats/{id}/test-message
GET    /api/admin/chats/{id}/events
POST   /api/admin/chats/monitor/run              -- запустить chat-monitor вручную
```

---

## 7. Доставки

```
GET    /api/admin/deliveries/jobs?channel=&status=&subscriber_id=&date_from=&page=
GET    /api/admin/deliveries/jobs/{id}
POST   /api/admin/deliveries/jobs/{id}/retry
POST   /api/admin/deliveries/jobs/{id}/cancel
GET    /api/admin/deliveries/jobs/{id}/messages
GET    /api/admin/deliveries/messages?status=&chat_id=&date_from=&page=
GET    /api/admin/deliveries/queue               -- сводка очереди по каналам
POST   /api/admin/deliveries/queue/pause         {channel}
POST   /api/admin/deliveries/queue/resume        {channel}
GET    /api/admin/deliveries/stats?days=30       -- отправлено/провалено по дням
GET    /api/admin/deliveries/export.csv

GET    /api/admin/digests?subscriber_id=&kind=&status=&page=
GET    /api/admin/digests/{id}                   -- состав и отрендеренный текст
POST   /api/admin/digests/{id}/resend
DELETE /api/admin/digests/{id}
```

---

## 8. Расписания и Prefect

```
GET    /api/admin/schedules?kind=&enabled=
POST   /api/admin/schedules
GET    /api/admin/schedules/{id}
PATCH  /api/admin/schedules/{id}                 {cron | interval_seconds, timezone, enabled, parameters}
DELETE /api/admin/schedules/{id}
POST   /api/admin/schedules/{id}/run             -- запустить сейчас
POST   /api/admin/schedules/{id}/toggle
POST   /api/admin/schedules/validate  {cron}     -> {valid, human: "каждый понедельник в 07:00", next_5: [...]}

GET    /api/admin/prefect/deployments
GET    /api/admin/prefect/flow-runs?kind=&state=&limit=
GET    /api/admin/prefect/flow-runs/{id}
GET    /api/admin/prefect/flow-runs/{id}/logs
POST   /api/admin/prefect/flow-runs/{id}/cancel
GET    /api/admin/prefect/health
```

Правка `schedules` синхронно пушится в Prefect REST API; при недоступности
Prefect возвращается 503 и запись помечается `sync_pending`, а `maintenance`
досинхронизирует.

---

## 9. Модели

```
GET    /api/admin/llm/providers
POST   /api/admin/llm/providers  {key, name, protocol, base_url, api_key}
PATCH  /api/admin/llm/providers/{id}
DELETE /api/admin/llm/providers/{id}
POST   /api/admin/llm/providers/{id}/test        -- пробный вызов, вернуть латентность и ответ
GET    /api/admin/llm/providers/{id}/models/discover  -- GET /v1/models у провайдера

GET    /api/admin/llm/models
POST   /api/admin/llm/models
PATCH  /api/admin/llm/models/{id}                -- tier, reasoning_style, лимиты, цены
DELETE /api/admin/llm/models/{id}
POST   /api/admin/llm/models/{id}/test  {prompt}

GET    /api/admin/llm/roles                      -- все 8 ролей с текущими привязками
PUT    /api/admin/llm/roles/{role}
    {model_id, fallback_model_id, temperature, max_tokens,
     reasoning_effort: 'none'|'low'|'high'|'max', json_mode, timeout_seconds,
     concurrency, system_prompt_override}
POST   /api/admin/llm/roles/{role}/test          -- прогнать роль на реальном материале
GET    /api/admin/llm/usage?days=30&group_by=role|model|day
GET    /api/admin/llm/calls?role=&status=&page=
```

Экран ролей — это и есть «настройка ризонинга на действия лайт-модели и
хард-модели отдельно»: восемь строк, в каждой своя модель и свой
`reasoning_effort`.

---

## 9.1 Аналитика

Полный список эндпоинтов, форматы ответов и спецификации графиков —
[analytics.md](analytics.md). Кратко:

```
GET  /api/admin/analytics/overview?days=30
GET  /api/admin/analytics/subscribers?days=90&kind=&granularity=day|week
GET  /api/admin/analytics/subscribers/retention?weeks=12&kind=
GET  /api/admin/analytics/subscribers/top?metric=activity|feedback&limit=20
GET  /api/admin/analytics/harvest/funnel?days=30&source=
GET  /api/admin/analytics/harvest/sources?days=30
GET  /api/admin/analytics/harvest/blocked-reasons?days=30
GET  /api/admin/analytics/harvest/terms?order=hits|dead
GET  /api/admin/analytics/corpus?days=90&group_by=kind|source|score_bucket
GET  /api/admin/analytics/delivery?days=30&channel=
GET  /api/admin/analytics/delivery/errors?days=30
GET  /api/admin/analytics/engagement?days=90
GET  /api/admin/analytics/engagement/feedback?days=90
GET  /api/admin/analytics/llm?days=30&group_by=role|model|day
GET  /api/admin/analytics/export.csv?report=...
POST /api/admin/analytics/rollup            -- пересчитать вручную
GET  /api/admin/analytics/rollup/status
```

Все временные ряды отдаются одной формой ответа, включая `color_slot` на
серию: слот приходит с бэка, а не выбирается на фронте по индексу массива —
так цвет остаётся закреплён за сущностью и фильтр не перекрашивает
оставшиеся серии.

## 10. Настройки

```
GET    /api/admin/settings?scope=                -- key, env_default, value, effective, is_env_only
PUT    /api/admin/settings/{key}  {value}
DELETE /api/admin/settings/{key}                 -- вернуть к env
POST   /api/admin/settings/reload                -- инвалидировать кэш во всех процессах
GET    /api/admin/settings/env-diff              -- что переопределено относительно .env
```

Секреты отдаются маскированными (`sk-7902…14f2`).

---

## 11. Аудит и здоровье

```
GET    /api/admin/audit?actor=&entity_type=&action=&date_from=&page=
GET    /api/admin/health                         -- postgres, qdrant, prefect, telegram, llm, embeddings
GET    /api/admin/health/telegram                -- getMe + состояние webhook
```

---

## Разделы

Двенадцать равноправных пунктов в шапке — это не навигация, а список всего,
что умеет система. Пункты собраны в пять разделов по одному вопросу: в каком
качестве администратор сюда пришёл.

| Раздел | Страницы | Зачем заходят |
|---|---|---|
| **Дашборд** | — | Посмотреть, всё ли живо |
| **Аудитория** | Заявки · Подписчики · Чаты · Подписки | Вести подписчиков: подтвердить, настроить профиль, проверить права бота и оплату |
| **Сбор** | — | Настроить, что платформа ищет во внешнем мире |
| **Работа платформы** | Запуски · Доставки | Следить, что запускается и доезжает |
| **Система** | Модели · Настройки · Аудит | Чинить саму платформу |

Второй уровень показывается строкой вкладок под шапкой и только там, где в
разделе больше одной страницы: пустая полоска обещала бы выбор, которого нет.
Счётчик заявок один на всю админку и виден дважды — на разделе и на вкладке,
чтобы очередь была заметна с любого экрана.

Адреса страниц при этом не менялись: раздел — способ их показать, а не
переезд. Закладки и ссылки из переписки продолжают работать. Единственное
исключение — `/schedules`: он отвечает постоянным редиректом на `/runs`, куда
расписания переехали.

Карта разделов живёт в `admin-ui/src/lib/nav.ts` одним объектом, и
`tests/test_admin_ui_navigation.py` следит, чтобы она не разъехалась с
маршрутами: страница без раздела не видна в шапке, пункт без страницы даёт 404.

## Экраны admin-ui

| Экран | Что на нём |
|---|---|
| **Дашборд** | KPI-строка со спарклайнами, воронка сбора за 30 дней, материалы по дням, доставки по дням, лента ошибок |
| **Аналитика → Подписчики** | Рост базы по типам, DAU/WAU/MAU, тепловая карта удержания по когортам, распределение по планам, топ активных |
| **Аналитика → Контент** | Источники (принято против отклонённого), топ причин отклонения, распределение оценок, статистика терминов |
| **Аналитика → Вовлечённость** | Диверген-график фидбека, engagement rate, средний размер дайджеста, пустые дайджесты |
| **Аналитика → Модели** | Расход по ролям и дням, стоимость на дайджест и на подписчика, латентность p50/p95, доля ошибок |
| **Harvest → Профиль** | Редактор групп и терминов с инлайн-правкой и bulk-вставкой, поле `satisfy` с валидацией, пороги. Панель «Проверить материал»: вставляешь title и abstract → подсвеченные совпадения и вердикт |
| **Harvest → Запросы** | Таблица по источникам, вкл/выкл, приоритет, лимиты, Preview с живой выдачей источника |
| **Harvest → Прогоны** | История `harvest_runs` с воронкой по стадиям, «Запустить сейчас», лог решений с фильтрами (`rejected` + `blocked_by`) |
| **Корпус** | Таблица материалов, фильтры, карточка с оценками, источниками и историей доставок |
| **Заявки** | Очередь `pending` — люди и чаты одним списком, с числом участников и правом бота постить. Подтверждение сразу заполняет профиль интересов; отказ пишет причину в аудит. Счётчик очереди висит в шапке: экран, за которым нужно специально заходить, не разбирается никогда |
| **Подписчики** | Три вкладки: Люди / Группы / Каналы. Статусы, `pending` на подтверждение. Карточка: профили, интересы, фидбек, дайджесты, доставки, график личной активности |
| **Профиль подписчика** | Описание, интересы ±вес, выученные сигналы, формат доставки, расписание, пороги, «Пересобрать», «Превью дайджеста», «Отправить сейчас». Под полем описания — живой разбор: на какие темы оно распадётся и что в нём не сработает |
| **Как писать профиль** | `/guide` — правила с примерами «так работает / так не работает». Текст приходит из `/api/admin/profiles/guide`, тот же самый показывает бот по `/howto`: две копии одной инструкции однажды разойдутся |
| **Подписки** | Планы; таблица подписок с полосой действия (from → until), фильтр «истекают через N дней», продление одной кнопкой, таймлайн событий |
| **Каналы** | Все чаты с ботом, статус и права, число участников, Refresh / Test / Leave, лента `chat_events` |
| **Доставки** | Очередь по каналам с паузой, лог заданий и отдельных сообщений (message_id, ошибка, retry_after), ретрай, экспорт CSV, график отправок |
| **Запуски** | Кнопки ручного запуска по этапам конвейера, все расписания одной таблицей с редактором cron и ближайшими запусками, история `flow_runs` с фильтрами и логами. Раньше это были две страницы — «Расписания» и «Прогоны», — и обе отвечали на один вопрос разными словами: запустил здесь, смотрю там |
| **Модели** | Провайдеры (добавить свой по base_url и ключу, Discover models), модели, экран ролей с reasoning на каждую роль, тестовый вызов, расход и стоимость |
| **Настройки** | Все ключи с колонками env / override / effective, сброс к env, поиск, группировка по scope |
| **Аудит** | Журнал действий администратора с before/after |

## Устройство admin-ui

SvelteKit с `adapter-node`: приложение отдаётся node-сервером, который заодно
держит сессию в httpOnly-cookie и ходит в FastAPI по внутреннему адресу.
Данные грузятся серверными `load`, а не запросами из браузера — на первую
отрисовку страница приезжает уже с данными, и токен не покидает сервер.

```
admin-ui/
├── src/
│   ├── app.html
│   ├── app.css                    # токены темы: поверхности, ink, сетка, слоты серий
│   ├── hooks.server.ts            # сессия из cookie, refresh при 401, редирект на /login
│   ├── lib/
│   │   ├── api/
│   │   │   ├── client.ts          # тонкий fetch-обёртка поверх event.fetch
│   │   │   └── schema.d.ts        # сгенерировано openapi-typescript из /openapi.json
│   │   ├── charts/                # графики на слоях: <svg> + d3-scale, без чарт-библиотеки
│   │   │   ├── LineChart.svelte
│   │   │   ├── AreaChart.svelte
│   │   │   ├── StackedBar.svelte
│   │   │   ├── DivergingBar.svelte
│   │   │   ├── FunnelBar.svelte
│   │   │   ├── Heatmap.svelte
│   │   │   ├── Sparkline.svelte
│   │   │   ├── StatTile.svelte
│   │   │   ├── Legend.svelte
│   │   │   ├── Tooltip.svelte
│   │   │   └── palette.ts         # слоты, ординальная и диверген-шкалы, статусы
│   │   ├── components/            # таблица, фильтры, drawer, поля форм, тосты
│   │   └── stores/                # период, тема, флаги очереди
│   └── routes/
│       ├── +layout.svelte         # шапка (эмблема + ПРОКСИМА), навигация, тема
│       ├── +layout.server.ts      # гейт авторизации на всё дерево
│       ├── login/
│       ├── (app)/
│       │   ├── +page.svelte                    # дашборд
│       │   ├── analytics/{subscribers,content,engagement,models}/
│       │   ├── harvest/{profile,queries,runs,decisions}/
│       │   ├── corpus/[id]/
│       │   ├── subscribers/[id]/profiles/[profileId]/
│       │   ├── subscriptions/{plans}/
│       │   ├── chats/[id]/
│       │   ├── deliveries/{jobs,messages}/
│       │   ├── schedules/[id]/
│       │   ├── models/{providers,roles}/
│       │   ├── settings/
│       │   └── audit/
│       └── api/                   # только то, что должно жить на node: logout, скачивание CSV
├── static/
├── svelte.config.js
├── vite.config.ts
└── package.json
```

Решения, которые стоит зафиксировать:

- **Типы генерируются, а не пишутся.** `openapi-typescript` из `/openapi.json`
  на каждой сборке; расхождение фронта с бэком становится ошибкой компиляции,
  а не багом в проде.
- **Формы — через `actions` и прогрессивное улучшение.** Валидация схемой
  (`zod` или `valibot`) на сервере, `use:enhance` на клиенте; форма работает и
  без JS, что бесплатно достаётся от SvelteKit.
- **Графики рисуются руками на `<svg>` + `d3-scale`.** Чарт-библиотека
  притащила бы свою палитру и своё представление о легендах, а
  [analytics.md](analytics.md) задаёт и то, и другое. Слои чарта — оси, сетка,
  марки, подписи, ховер — это ровно те компоненты, что перечислены выше; их
  меньше, чем адаптеров под чужой API.
- **Тема.** Светлая — крем сайта, тёмная — навь. Токены объявлены на `:root`,
  тёмные значения подобраны отдельно, а не выведены инверсией; переключатель
  ставит `data-theme` и побеждает системную настройку в обе стороны.
- **Дизайн-код** — [design.md](design.md): токены, шрифты, логотип, виджеты и
  палитра данных. Шрифты и эмблема лежат в `admin-ui/static/`, скопированы с
  сайта; своей версии дизайн-кода Проксима не заводит.
- **Таблицы.** Пагинация и сортировка серверные: `harvest_decisions` и
  `delivery_messages` растут быстро, клиентская сортировка на них — ловушка.
- **Реалтайм.** Очередь доставки и активные прогоны обновляются через SSE с
  `/api/admin/events`; остальное — по `invalidate` после мутаций.
