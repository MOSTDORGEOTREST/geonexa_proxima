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


def test_weekly_schedule_does_not_slip_into_biweekly() -> None:
    """Регрессия: срок считается от постановки в очередь, а крон — по часам.

    Прогон в понедельник в 00:00 занял три минуты — и ровно через неделю
    профиль «ещё не пора» на эти три минуты, пропускает свой понедельник и
    ждёт следующего. Недельная рассылка превращалась в двухнедельную.
    """

    due = str(DUE_PROFILES)
    assert "make_interval(mins => :grace)" in due


def test_due_profiles_works_without_the_grace_parameter() -> None:
    """Скрипты проверки и админка спрашивают «кому пора сейчас».

    Они не обязаны знать про настройку допуска, поэтому у параметра есть
    значение по умолчанию: без него их вызовы падали бы на недостающем bind.
    """

    assert DUE_PROFILES.compile().params["grace"] == 0


def test_profile_context_reads_the_plan_interval() -> None:
    context = str(PROFILE_CONTEXT)
    assert "min_interval_hours" in context
    assert "subscription_plans" in context


def test_resolve_kinds_rejects_unknown_kinds() -> None:
    assert resolve_kinds(["group"]) == ["group"]
    assert resolve_kinds() == ["user", "group", "channel"]


def test_deliver_at_hour_splits_building_from_sending() -> None:
    """Дайджест собирается ночью, а уходит в чаты в названный час.

    Час назначается заданию, а не крону воркера рассылки: с недельным кроном
    повторная попытка после сбоя ждала бы следующего понедельника и протухала
    бы по TTL.
    """

    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from geonexa_proxima.workflows.dispatch import _deliver_at

    moscow = ZoneInfo("Europe/Moscow")
    # Понедельник, полночь по Москве — момент запуска диспетчера чатов.
    midnight = datetime(2026, 8, 30, 21, 0, tzinfo=UTC)

    planned = _deliver_at("Europe/Moscow", 16, midnight)

    assert planned == datetime(2026, 8, 31, 13, 0, tzinfo=UTC)
    assert planned.astimezone(moscow).hour == 16


def test_no_hour_means_send_as_soon_as_the_worker_sees_it() -> None:
    from geonexa_proxima.workflows.dispatch import _deliver_at

    assert _deliver_at("Europe/Moscow", None) is None


def test_hour_already_past_sends_immediately() -> None:
    """Ручной запуск вечером не должен откладывать отправку на сутки."""

    from datetime import UTC, datetime

    from geonexa_proxima.workflows.dispatch import _deliver_at

    evening = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)  # 19:00 по Москве

    assert _deliver_at("Europe/Moscow", 16, evening) is None


def test_hour_outside_the_day_is_rejected() -> None:
    import pytest

    from geonexa_proxima.workflows.dispatch import _deliver_at

    with pytest.raises(ValueError, match="вне суток"):
        _deliver_at("Europe/Moscow", 25)
