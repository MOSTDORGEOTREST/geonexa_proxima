"""SQL выборок диспетчера: живёт вне флоу, чтобы его можно было проверить.

Модуль намеренно не импортирует Prefect: тот же запрос нужен админке, когда она
показывает «кому сейчас пора», и проверочным скриптам, где Prefect не стоит.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import text

from geonexa_proxima.domain import (
    ALL_KINDS,
    CHAT_KINDS,
    PERSONAL_KINDS,
    PRESENT_BOT_STATUSES,
    sql_literals,
)

__all__ = [
    "ALL_KINDS",
    "CHAT_KINDS",
    "DUE_PROFILES",
    "PERSONAL_KINDS",
    "PROFILE_CONTEXT",
    "SCHEDULE_NEXT",
    "digest_interval_hours",
    "resolve_kinds",
]

#: Кому пора. Вид подписчика — параметр, а не константа: диспетчер личек и
#: диспетчер чатов ходят по одной таблице, но по разным строкам, и у них разные
#: лимиты Bot API. Для чатов добавляется проверка, что бота оттуда не выгнали:
#: подписка у группы может быть живой, а доставлять уже некуда.
#:
#: `:grace` — окно допуска, и без него недельное расписание сползает в
#: двухнедельное. Срок следующего дайджеста считается от момента постановки в
#: очередь: прогон в понедельник в 00:00 занял три минуты — и ровно через
#: неделю профиль оказывается «ещё не пора» на три минуты, пропускает свой
#: понедельник и ждёт следующего. Допуск в час съедает эту разницу навсегда.
DUE_PROFILES = text(
    f"""
    SELECT p.id AS profile_id, s.id AS subscriber_id, s.kind, s.telegram_chat_id
      FROM subscriber_profiles p
      JOIN subscribers s ON s.id = p.subscriber_id
     WHERE p.digest_enabled
       AND s.status = 'active'
       AND s.kind = ANY(:kinds)
       AND (p.paused_until IS NULL OR p.paused_until <= now())
       AND (
             p.next_digest_at IS NULL
             OR p.next_digest_at <= now() + make_interval(mins => :grace)
           )
       AND EXISTS (
             SELECT 1 FROM subscriptions sub
              WHERE sub.subscriber_id = s.id
                AND sub.status IN ('active', 'trial')
                AND sub.starts_at <= now()
                AND (sub.ends_at IS NULL OR coalesce(sub.grace_until, sub.ends_at) >= now())
           )
       AND (
             s.kind = 'user'
             OR EXISTS (
                  SELECT 1 FROM chat_memberships m
                   WHERE m.subscriber_id = s.id
                     AND m.bot_status IN ({sql_literals(PRESENT_BOT_STATUSES)})
                     AND (s.kind <> 'channel' OR coalesce(m.can_post_messages, false))
                )
           )
     ORDER BY p.next_digest_at NULLS FIRST
     LIMIT :limit
    """
).bindparams(grace=0)
"""Допуск по умолчанию — ноль: скрипты проверки и админка спрашивают «кому
пора прямо сейчас» и не обязаны знать про настройку. Диспетчер передаёт свой."""


def resolve_kinds(kinds: Iterable[str] | None = None) -> list[str]:
    """Проверить набор видов подписчиков до похода в базу."""

    selected = list(kinds) if kinds else list(ALL_KINDS)
    unknown = set(selected) - set(ALL_KINDS)
    if unknown:
        raise ValueError("неизвестный вид подписчика: " + ", ".join(sorted(unknown)))
    return selected


#: Тариф подписчика вместе с профилем: интервал между дайджестами задаёт план,
#: а не только настройки профиля.
PROFILE_CONTEXT = text(
    """
    SELECT p.id, p.name, p.version, p.max_items, p.min_personal_score,
           p.delivery_format, p.digest_settings, p.timezone, p.last_digest_at,
           s.id AS subscriber_id, s.kind, s.telegram_chat_id, s.timezone AS subscriber_timezone,
           coalesce(plan.min_interval_hours, :default_interval) AS min_interval_hours,
           coalesce(plan.max_items_per_digest, p.max_items) AS plan_max_items
      FROM subscriber_profiles p
      JOIN subscribers s ON s.id = p.subscriber_id
      LEFT JOIN LATERAL (
            SELECT pl.min_interval_hours, pl.max_items_per_digest
              FROM subscriptions sub
              JOIN subscription_plans pl ON pl.id = sub.plan_id
             WHERE sub.subscriber_id = s.id
               AND sub.status IN ('active', 'trial')
               AND sub.starts_at <= now()
               AND (sub.ends_at IS NULL OR coalesce(sub.grace_until, sub.ends_at) >= now())
             -- Порядок обязателен: при двух живых подписках (не погашенный
             -- триал плюс начавшаяся платная) без него Postgres вернёт
             -- произвольную, и интервал между дайджестами будет плавать от
             -- прогона к прогону. Берём самую строгую по частоте.
             ORDER BY pl.min_interval_hours DESC, sub.starts_at DESC
             LIMIT 1
           ) plan ON true
     WHERE p.id = :id
    """
)

#: Отметить, что дайджест построен, и назначить следующий.
#: Без этого запроса диспетчер отбирал профили по `next_digest_at IS NULL`,
#: которое никто никогда не заполнял, — и слал дайджест на каждом прогоне.
SCHEDULE_NEXT = text(
    """
    UPDATE subscriber_profiles
       SET last_digest_at = now(),
           next_digest_at = now() + make_interval(hours => :hours),
           updated_at = now()
     WHERE id = :id
    RETURNING next_digest_at
    """
)


def digest_interval_hours(
    *,
    plan_minimum: int,
    profile_settings: dict[str, Any] | None = None,
    floor_hours: int = 1,
) -> int:
    """Через сколько часов профилю снова положен дайджест.

    Профиль может просить чаще, чем разрешает тариф, — и не получит: интервал
    из настроек профиля работает только в сторону увеличения. Иначе достаточно
    было бы поправить своё расписание, чтобы обойти ограничение плана.
    """

    requested = (profile_settings or {}).get("interval_hours")
    try:
        wanted = int(requested) if requested is not None else 0
    except (TypeError, ValueError):
        wanted = 0
    return max(int(plan_minimum), wanted, floor_hours)
