"""Посуточный сбор: верхняя граница окна прогона и новые расписания.

Сбор перестал быть «всё, что вышло за последние N часов» и стал прогоном за
конкретные сутки. Отсюда две правки.

Первая: у прогона появляется верхняя граница окна. Без неё нельзя ответить на
вопрос «за какие сутки корпус уже собран», а значит нельзя и догнать
пропущенные дни — прогон не знает, с какого места продолжать.

Вторая: расписания. Сбор — каждую ночь за вчерашние сутки, дайджест групп и
каналов — раз в неделю, личные чаты — только руками. Значения правятся, только
если в строке всё ещё стоит прежнее умолчание: расписание правится и через
админку, и затирать сделанное там нельзя.

Revision ID: 0008
Revises: 0007
Created: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: ключ → (прежний крон, новый крон, новое состояние выключателя)
#:
#: Воркера рассылки в группы здесь нет намеренно: он остаётся частым. Время
#: недельной рассылки задаёт `scheduled_at` задания (параметр
#: `deliver_at_hour` у диспетчера чатов), а не крон воркера — с недельным
#: кроном повторная попытка после сбоя ждала бы следующего понедельника и
#: протухала бы по `DELIVERY_JOB_TTL_HOURS`, а очередь длиннее одной пачки
#: разъезжалась бы неделями.
_SCHEDULES: tuple[tuple[str, str, str, bool], ...] = (
    ("global-harvest", "0 3 * * *", "0 1 * * *", True),
    ("digest-dispatch-chats", "30 7 * * 1", "0 0 * * 1", True),
    # Личные чаты остаются со своим кроном, но выключенными: строка видна в
    # админке, и включить её можно одним переключателем, не трогая .env.
    ("digest-dispatch", "0 7 * * 1", "0 7 * * 1", False),
    ("delivery-personal", "*/5 * * * *", "*/5 * * * *", False),
)


#: Индексы, без которых горячие пути читают таблицы целиком.
#:
#: Первые три — под ночную уборку: `DELETE ... WHERE created_at < ...` шёл
#: последовательным чтением по таблицам, которые растут быстрее всех, и под
#: `statement_timeout` отваливался — то есть уборка не работала вовсе, а
#: таблица росла дальше. Остальные — под внешние ключи с `ON DELETE`: без них
#: удаление одного материала или подписчика сканирует по четыре самых больших
#: таблицы, и админка отвечает пятисоткой на удаление одной строки.
_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_harvest_decisions_created_at", "harvest_decisions", ["created_at"]),
    ("ix_delivery_messages_created_at", "delivery_messages", ["created_at"]),
    ("ix_chat_events_occurred_at", "chat_events", ["occurred_at"]),
    ("ix_delivery_jobs_created_at", "delivery_jobs", ["created_at"]),
    ("ix_harvest_decisions_item_id", "harvest_decisions", ["item_id"]),
    ("ix_delivery_messages_item_id", "delivery_messages", ["item_id"]),
    ("ix_llm_call_log_item_id", "llm_call_log", ["item_id"]),
    ("ix_llm_call_log_subscriber_id", "llm_call_log", ["subscriber_id"]),
    ("ix_subscriber_activity_item_id", "subscriber_activity", ["item_id"]),
    ("ix_subscriber_activity_digest_id", "subscriber_activity", ["digest_id"]),
    ("ix_digests_subscriber_id", "digests", ["subscriber_id"]),
    ("ix_items_harvest_profile_id", "items", ["harvest_profile_id"]),
    ("ix_digest_items_profile_score_id", "digest_items", ["profile_score_id"]),
    ("ix_flow_runs_schedule_id", "flow_runs", ["schedule_id"]),
    ("ix_flow_runs_subscriber_id", "flow_runs", ["subscriber_id"]),
)


def upgrade() -> None:
    op.add_column("harvest_runs", sa.Column("until", sa.DateTime(timezone=True)))
    # Уже прошедшие прогоны собирали открытым окном «от since и до свежего»,
    # поэтому честная верхняя граница у них — момент завершения. Без этого
    # первый же плановый прогон решил бы, что не собрано ничего, и полез бы
    # догонять неделю.
    # Момент завершения прогона — не граница собранного: прогон шёл открытым
    # окном и заканчивался уже в следующие сутки. Если записать его как есть,
    # первый плановый прогон решит, что текущие сутки покрыты, и материалы за
    # них не соберутся никогда. Отступаем на сутки назад: лишний раз собрать
    # уже собранное дешевле, чем не собрать вовсе — дедупликация на месте.
    op.execute(
        "UPDATE harvest_runs "
        "   SET until = date_trunc('day', coalesce(finished_at, started_at)) "
        " WHERE until IS NULL AND status = 'succeeded'"
    )
    op.create_index(
        "ix_harvest_runs_succeeded_until",
        "harvest_runs",
        [sa.text("until DESC")],
        postgresql_where=sa.text("status = 'succeeded'"),
    )
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns)
    for key, previous, current, enabled in _SCHEDULES:
        op.execute(
            sa.text(
                "UPDATE schedules SET cron = :cron, enabled = :enabled, "
                "sync_pending = true, updated_at = now() "
                "WHERE key = :key AND cron = :previous"
            ).bindparams(key=key, previous=previous, cron=current, enabled=enabled)
        )
    # Параметры строки расписания сильнее параметров из каталога флоу, поэтому
    # час отправки надо дописать в саму строку: иначе на существующей базе
    # дайджест уедет в чаты в полночь, сразу после сборки.
    op.execute(
        "UPDATE schedules "
        "   SET parameters = coalesce(parameters, '{}'::jsonb) "
        "                    || '{\"deliver_at_hour\": 16}'::jsonb, "
        "       sync_pending = true, updated_at = now() "
        " WHERE key = 'digest-dispatch-chats' "
        # Только если час ещё не задан: администратор мог уже выставить свой,
        # и молча вернуть 16 — значит отправить дайджест не в то время.
        "   AND jsonb_typeof(coalesce(parameters, '{}'::jsonb)) = 'object' "
        "   AND NOT (coalesce(parameters, '{}'::jsonb) ? 'deliver_at_hour')"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE schedules SET parameters = parameters - 'deliver_at_hour', "
        "sync_pending = true, updated_at = now() WHERE key = 'digest-dispatch-chats'"
    )
    for key, previous, current, enabled in _SCHEDULES:
        if previous == current and not enabled:
            # Строка, у которой менялся только выключатель. Возвращать ей
            # `enabled = true` нельзя: личные чаты выключены намеренно, и
            # откат схемы не должен начинать рассылать дайджесты людям.
            continue
        op.execute(
            sa.text(
                "UPDATE schedules SET cron = :previous, enabled = true, "
                "sync_pending = true, updated_at = now() "
                "WHERE key = :key AND cron = :cron"
            ).bindparams(key=key, previous=previous, cron=current)
        )
    for name, table, _ in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
    op.drop_index("ix_harvest_runs_succeeded_until", table_name="harvest_runs")
    op.drop_column("harvest_runs", "until")
