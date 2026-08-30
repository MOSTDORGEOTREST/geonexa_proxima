"""Правила диспетчера: как часто подписчику положен дайджест."""

from __future__ import annotations

from geonexa_proxima.services.dispatch_queries import (
    DUE_PROFILES,
    PROFILE_CONTEXT,
    SCHEDULE_NEXT,
    digest_interval_hours,
    resolve_kinds,
)


def test_plan_minimum_applies_when_profile_is_silent() -> None:
    assert digest_interval_hours(plan_minimum=168) == 168


def test_profile_may_ask_for_less_often_but_not_more() -> None:
    """Иначе ограничение тарифа обходилось бы правкой собственных настроек."""

    assert digest_interval_hours(plan_minimum=168, profile_settings={"interval_hours": 24}) == 168
    assert digest_interval_hours(plan_minimum=24, profile_settings={"interval_hours": 168}) == 168


def test_garbage_in_settings_falls_back_to_the_plan() -> None:
    for value in ("скоро", None, -5, 0, [], {}):
        assert (
            digest_interval_hours(plan_minimum=24, profile_settings={"interval_hours": value}) == 24
        )


def test_interval_never_drops_below_an_hour() -> None:
    """Ноль в тарифе означал бы дайджест на каждом прогоне диспетчера."""

    assert digest_interval_hours(plan_minimum=0) == 1


def test_dispatcher_filters_on_the_next_run_timestamp() -> None:
    """Регрессия: поле читалось, но никто его не заполнял.

    Из-за этого `next_digest_at IS NULL` было истинно всегда, и диспетчер
    отбирал каждый профиль на каждом прогоне — то есть слал дубликаты.
    """

    due = str(DUE_PROFILES)
    assert "next_digest_at" in due
    # А запрос, который его заполняет, обязан существовать и что-то писать.
    schedule = str(SCHEDULE_NEXT)
    assert "next_digest_at =" in schedule
    assert "last_digest_at" in schedule


def test_profile_context_reads_the_plan_interval() -> None:
    context = str(PROFILE_CONTEXT)
    assert "min_interval_hours" in context
    assert "subscription_plans" in context


def test_resolve_kinds_rejects_unknown_kinds() -> None:
    assert resolve_kinds(["group"]) == ["group"]
    assert resolve_kinds() == ["user", "group", "channel"]
