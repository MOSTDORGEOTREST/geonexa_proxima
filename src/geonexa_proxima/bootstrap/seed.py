"""Обязательные записи, без которых платформа не работает.

Сидирование идемпотентно: каждый вызов приводит базу к нужному состоянию и
ничего не ломает, если записи уже есть. Поэтому его безопасно выполнять на
каждом старте контейнера, а не один раз руками.

Что именно засевается и почему:

* **harvest-профиль** — без него сбор не знает, что искать, и первый прогон
  скачает всё подряд;
* **план подписки по умолчанию** — иначе некому выдать подписку новому
  подписчику, и он молча не получит ни одного дайджеста;
* **реестр моделей** — роли должны быть привязаны к модели до первого вызова
  LLM, иначе флоу упадёт на середине сбора;
* **расписания** — их читает диспетчер; пустая таблица означает, что не
  запустится ничего;
* **app_settings** — снимок значений из `.env`, чтобы админка показывала, что
  именно переопределено.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from geonexa_proxima.config import Settings
from geonexa_proxima.flow_catalog import FLOWS
from geonexa_proxima.harvest import load_harvest_profile

# Настройки, которые в БД не попадают: их читают до подключения к ней либо
# они дают доступ к самой базе. Переопределять такое через админку нельзя.
ENV_ONLY = frozenset(
    {
        "DATABASE_URL",
        "DATABASE_SSL_MODE",
        "DATABASE_SSL_ROOT_CERT",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "DB_CONNECTION_BUDGET",
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "ADMIN_PASSWORD_HASH",
        "ADMIN_JWT_SECRET",
        "SECRET_ENCRYPTION_KEY",
        "PREFECT_API_URL",
        "PREFECT_API_KEY",
    }
)

SETTINGS_IN_DB: dict[str, str] = {
    "SEMANTIC_THRESHOLD": "harvest",
    "DIGEST_SCORE_THRESHOLD": "harvest",
    "DEEP_ANALYSIS_THRESHOLD": "harvest",
    "ALERT_SCORE_THRESHOLD": "harvest",
    "HARVEST_KEYWORD_THRESHOLD": "harvest",
    "COLLECTION_LOOKBACK_HOURS": "harvest",
    "MAX_ITEMS_PER_SOURCE": "harvest",
    "PERSONALIZATION_CANDIDATE_LIMIT": "harvest",
    "DELIVERY_BATCH_SIZE": "delivery",
    "DELIVERY_MAX_ATTEMPTS": "delivery",
    "DELIVERY_RETRY_BACKOFF_SECONDS": "delivery",
    "DELIVERY_DRY_RUN": "delivery",
    "TELEGRAM_GLOBAL_RATE_PER_SECOND": "telegram",
    "TELEGRAM_CHAT_RATE_PER_SECOND": "telegram",
    "TELEGRAM_GROUP_RATE_PER_MINUTE": "telegram",
    "TELEGRAM_REGISTRATION_MODE": "telegram",
    "METRICS_ROLLUP_LOOKBACK_DAYS": "general",
    "EMBEDDING_QUERY_INSTRUCTION": "llm",
    "EMBEDDING_INSTRUCTION_ENABLED": "llm",
}

LLM_ROLES: dict[str, tuple[str, str]] = {
    "ranker": ("light", "глобальная научная оценка материала"),
    "explainer": ("light", "персональное объяснение релевантности"),
    "profile_compiler": ("light", "сборка текста профиля из описания и интересов"),
    "query_expander": ("light", "расширение поисковых запросов"),
    "digest_writer": ("light", "вводка дайджеста и группировка"),
    "analyzer": ("heavy", "глубокий разбор материала"),
    "deep_dive": ("heavy", "ответ на кнопку «разобрать глубже»"),
    "chat": ("heavy", "свободные вопросы в боте"),
}


@dataclass
class SeedReport:
    created: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)

    def note(self, name: str, *, created: int = 0, skipped: int = 0) -> None:
        if created:
            self.created[name] = self.created.get(name, 0) + created
        if skipped:
            self.skipped[name] = self.skipped.get(name, 0) + skipped

    def as_dict(self) -> dict[str, Any]:
        return {"created": self.created, "skipped": self.skipped}

    @property
    def changed(self) -> bool:
        return bool(self.created)


async def seed_all(engine: AsyncEngine, settings: Settings) -> SeedReport:
    """Привести обязательные записи к нужному состоянию. Идемпотентно."""

    report = SeedReport()
    async with engine.begin() as connection:
        await _seed_plans(connection, settings, report)
        await _seed_harvest_profile(connection, settings, report)
        await _seed_llm_registry(connection, settings, report)
        await _seed_schedules(connection, settings, report)
        await _seed_settings(connection, settings, report)
    return report


async def _seed_plans(connection: Any, settings: Settings, report: SeedReport) -> None:
    result = await connection.execute(
        text(
            """
            INSERT INTO subscription_plans (
                id, key, name, description, max_profiles, max_items_per_digest,
                min_interval_hours, allow_group_chats, is_default)
            VALUES (gen_random_uuid(), :key, 'Базовый',
                    'План по умолчанию для новых подписчиков', 1, 20, 168, true, true)
            ON CONFLICT (key) DO NOTHING
            RETURNING id
            """
        ),
        {"key": settings.default_subscription_plan},
    )
    created = len(result.fetchall())
    report.note("subscription_plans", created=created, skipped=1 - created)


async def _seed_harvest_profile(connection: Any, settings: Settings, report: SeedReport) -> None:
    """Загрузить профиль сбора из YAML. Существующий не трогаем: его правят в админке."""

    exists = await connection.scalar(
        text("SELECT id FROM harvest_profiles WHERE key = :key"),
        {"key": settings.harvest_profile_key},
    )
    if exists:
        report.note("harvest_profiles", skipped=1)
        return
    if not settings.harvest_config_path.is_file():
        raise FileNotFoundError(
            f"Профиль сбора не найден: {settings.harvest_config_path}. "
            f"Без него первый прогон скачает всё подряд."
        )
    profile = load_harvest_profile(settings.harvest_config_path)
    profile_id = await connection.scalar(
        text(
            """
            INSERT INTO harvest_profiles (
                id, key, name, description, satisfy_expr, keyword_score_threshold,
                borderline_semantic_threshold, languages, item_kinds, is_active)
            VALUES (gen_random_uuid(), :key, :name, :description, :satisfy,
                    :keyword_threshold, :semantic_threshold,
                    CAST(:languages AS text[]), CAST(:kinds AS text[]), true)
            RETURNING id
            """
        ),
        {
            "key": profile.key,
            "name": profile.name,
            "description": profile.description,
            "satisfy": profile.satisfy_expr,
            "keyword_threshold": profile.keyword_score_threshold,
            "semantic_threshold": profile.borderline_semantic_threshold,
            "languages": list(profile.languages),
            "kinds": list(profile.item_kinds),
        },
    )
    groups = terms = 0
    for position, group in enumerate(profile.groups):
        group_id = await connection.scalar(
            text(
                """
                INSERT INTO harvest_term_groups (
                    id, harvest_profile_id, key, name, mode, min_matches, fields,
                    weight, is_hard, penalty, affects_satisfy, enabled, position)
                VALUES (gen_random_uuid(), :profile_id, :key, :name, :mode, :min_matches,
                        CAST(:fields AS text[]), :weight, :is_hard, :penalty,
                        :affects_satisfy, :enabled, :position)
                RETURNING id
                """
            ),
            {
                "profile_id": profile_id,
                "key": group.key,
                "name": group.name or None,
                "mode": group.mode.value,
                "min_matches": group.min_matches,
                "fields": list(group.fields),
                "weight": group.weight,
                "is_hard": group.is_hard,
                "penalty": group.penalty,
                "affects_satisfy": group.affects_satisfy,
                "enabled": group.enabled,
                "position": position,
            },
        )
        groups += 1
        for term in group.terms:
            await connection.execute(
                text(
                    """
                    INSERT INTO harvest_terms (
                        id, group_id, term, normalized_term, match_type, lang, weight)
                    VALUES (gen_random_uuid(), :group_id, :term, :normalized, :match_type,
                            :lang, :weight)
                    ON CONFLICT (group_id, normalized_term, match_type) DO NOTHING
                    """
                ),
                {
                    "group_id": group_id,
                    "term": term.term,
                    "normalized": term.pattern.pattern
                    if term.match_type.value == "regex"
                    else _normalized(term.term),
                    "match_type": term.match_type.value,
                    "lang": term.lang,
                    "weight": term.weight,
                },
            )
            terms += 1
    report.note("harvest_profiles", created=1)
    report.note("harvest_term_groups", created=groups)
    report.note("harvest_terms", created=terms)


def _normalized(term: str) -> str:
    from geonexa_proxima.harvest import normalize

    return normalize(term)


async def _seed_llm_registry(connection: Any, settings: Settings, report: SeedReport) -> None:
    """Провайдер и модели из .env плюс привязка всех восьми ролей."""

    provider_id = await connection.scalar(
        text("SELECT id FROM llm_providers WHERE key = :key"),
        {"key": settings.default_llm_provider_key},
    )
    if provider_id is None:
        provider_id = await connection.scalar(
            text(
                """
                INSERT INTO llm_providers (
                    id, key, name, protocol, base_url, api_key_env_var,
                    is_managed_by_env, enabled)
                VALUES (gen_random_uuid(), :key, :name, 'openai_compatible', :base_url,
                        'DEFAULT_LLM_API_KEY', true, true)
                RETURNING id
                """
            ),
            {
                "key": settings.default_llm_provider_key,
                "name": settings.default_llm_provider_key.title(),
                "base_url": settings.default_llm_base_url,
            },
        )
        report.note("llm_providers", created=1)
    else:
        report.note("llm_providers", skipped=1)

    models: dict[str, Any] = {}
    for tier, name, effort in (
        ("light", settings.light_llm_model, settings.light_llm_reasoning_effort.value),
        ("heavy", settings.heavy_llm_model, settings.heavy_llm_reasoning_effort.value),
    ):
        model_id = await connection.scalar(
            text("SELECT id FROM llm_models WHERE provider_id = :p AND key = :k"),
            {"p": provider_id, "k": name},
        )
        if model_id is None:
            model_id = await connection.scalar(
                text(
                    """
                    INSERT INTO llm_models (
                        id, provider_id, key, model_name, display_name, tier,
                        supports_reasoning, reasoning_style, supports_json_mode)
                    VALUES (gen_random_uuid(), :provider_id, :key, :key, :key, :tier,
                            true, 'openai_effort', true)
                    RETURNING id
                    """
                ),
                {"provider_id": provider_id, "key": name, "tier": tier},
            )
            report.note("llm_models", created=1)
        else:
            report.note("llm_models", skipped=1)
        models[tier] = (model_id, effort)

    created = skipped = 0
    for role, (tier, _description) in LLM_ROLES.items():
        model_id, effort = models[tier]
        result = await connection.execute(
            text(
                """
                INSERT INTO llm_role_bindings (
                    id, role, model_id, temperature, max_tokens, reasoning_effort,
                    json_mode, timeout_seconds, concurrency, updated_by)
                VALUES (gen_random_uuid(), :role, :model_id, :temperature, :max_tokens,
                        :effort, :json_mode, :timeout, :concurrency, 'seed')
                ON CONFLICT (role) DO NOTHING
                RETURNING id
                """
            ),
            {
                "role": role,
                "model_id": model_id,
                "temperature": settings.light_llm_temperature
                if tier == "light"
                else settings.heavy_llm_temperature,
                "max_tokens": settings.light_llm_max_tokens
                if tier == "light"
                else settings.heavy_llm_max_tokens,
                "effort": effort,
                "json_mode": settings.light_llm_json_mode
                if tier == "light"
                else settings.heavy_llm_json_mode,
                "timeout": int(settings.llm_timeout_seconds),
                "concurrency": settings.light_llm_concurrency
                if tier == "light"
                else settings.heavy_llm_concurrency,
            },
        )
        if result.fetchall():
            created += 1
        else:
            skipped += 1
    report.note("llm_role_bindings", created=created, skipped=skipped)


async def _seed_schedules(connection: Any, settings: Settings, report: SeedReport) -> None:
    """Расписания читает диспетчер: пустая таблица означает, что не запустится ничего.

    Строки берутся из каталога флоу, а не пишутся здесь руками: разъехавшийся
    каталог и разъехавшиеся расписания дают deployment без расписания либо
    расписание без deployment — и то и другое обнаруживается только на проде.
    Флоу без крона (например, дайджест одного подписчика, который запускает
    диспетчер) строки не получает.
    """

    crons = {
        "global-harvest": settings.schedule_global_harvest_cron,
        "digest-dispatch": settings.schedule_digest_dispatch_cron,
        "digest-dispatch-chats": settings.schedule_digest_dispatch_chats_cron,
        "delivery-personal": settings.schedule_delivery_personal_cron,
        "delivery-group": settings.schedule_delivery_group_cron,
        "chat-monitor": settings.schedule_chat_monitor_cron,
        "subscription-maintenance": settings.schedule_subscription_maintenance_cron,
        "metrics-rollup": settings.metrics_rollup_cron,
        "maintenance": settings.schedule_maintenance_cron,
    }
    names = {
        "global-harvest": "Сбор материалов",
        "digest-dispatch": "Диспетчер дайджестов (личные чаты)",
        "digest-dispatch-chats": "Диспетчер дайджестов (группы и каналы)",
        "delivery-personal": "Рассылка в личные чаты",
        "delivery-group": "Рассылка в группы и каналы",
        "chat-monitor": "Проверка чатов с ботом",
        "subscription-maintenance": "Подписки: истечение и напоминания",
        "metrics-rollup": "Пересчёт метрик",
        "maintenance": "Обслуживание очереди рассылки",
    }
    unknown = set(crons) - {spec.key for spec in FLOWS}
    if unknown:
        raise RuntimeError("Расписание без флоу: " + ", ".join(sorted(unknown)))

    created = skipped = 0
    for spec in FLOWS:
        cron = crons.get(spec.key)
        if cron is None:
            continue
        result = await connection.execute(
            text(
                """
                INSERT INTO schedules (
                    id, key, name, kind, cron, timezone, enabled, parameters)
                VALUES (gen_random_uuid(), :key, :name, :kind, :cron, :tz, true,
                        CAST(:parameters AS jsonb))
                ON CONFLICT (key) DO NOTHING
                RETURNING id
                """
            ),
            {
                "key": spec.key,
                "name": names.get(spec.key, spec.description),
                "kind": spec.schedule_kind,
                "cron": cron,
                "tz": settings.timezone,
                "parameters": json.dumps(spec.parameters or {}, ensure_ascii=False),
            },
        )
        if result.fetchall():
            created += 1
        else:
            skipped += 1
    report.note("schedules", created=created, skipped=skipped)


async def _seed_settings(connection: Any, settings: Settings, report: SeedReport) -> None:
    """Снимок значений из .env, чтобы админка показывала, что переопределено."""

    created = skipped = 0
    for key, scope in SETTINGS_IN_DB.items():
        value = getattr(settings, key.lower(), None)
        if value is None:
            continue
        if hasattr(value, "value"):
            value = value.value
        payload = json.dumps(value, ensure_ascii=False, default=str)
        result = await connection.execute(
            text(
                """
                INSERT INTO app_settings (
                    key, value, value_type, env_default, scope, is_env_only, updated_by)
                VALUES (:key, CAST(:value AS jsonb), :value_type, CAST(:value AS jsonb),
                        :scope, false, 'seed')
                ON CONFLICT (key) DO UPDATE SET env_default = EXCLUDED.env_default
                WHERE app_settings.updated_by = 'seed'
                -- xmax = 0 отличает вставку от обновления: без этого повторный
                -- запуск отчитывался бы о создании того, что уже было.
                RETURNING (xmax = 0) AS inserted
                """
            ),
            {
                "key": key,
                "value": payload,
                "value_type": _value_type(value),
                "scope": scope,
            },
        )
        row = result.first()
        if row is not None and row[0]:
            created += 1
        else:
            skipped += 1
    for key in ENV_ONLY:
        await connection.execute(
            text(
                """
                INSERT INTO app_settings (key, value, value_type, scope, is_env_only, updated_by)
                VALUES (:key, 'null'::jsonb, 'secret', 'general', true, 'seed')
                ON CONFLICT (key) DO NOTHING
                """
            ),
            {"key": key},
        )
    report.note("app_settings", created=created, skipped=skipped)


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, (dict, list)):
        return "json"
    return "string"


async def bootstrap(
    engine: AsyncEngine,
    settings: Settings,
    *,
    allow_upgrade: bool = True,
) -> dict[str, Any]:
    """Полный подъём: схема, затем обязательные записи."""

    from geonexa_proxima.bootstrap.schema import ensure_schema, inspect_schema

    before = await ensure_schema(engine, allow_upgrade=allow_upgrade)
    after = await inspect_schema(engine)
    report = await seed_all(engine, settings)
    return {
        "schema_before": before.summary,
        "schema_after": after.summary,
        "migrated": not before.is_current,
        "seed": report.as_dict(),
    }
