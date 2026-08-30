"""Правила уборки: имена таблиц и колонок должны существовать в схеме."""

from __future__ import annotations

from geonexa_proxima.config import Settings
from geonexa_proxima.db.base import Base
from geonexa_proxima.metrics.purge import RULES


def test_purge_rules_match_the_orm_schema() -> None:
    """Опечатка в имени колонки иначе всплывёт только на проде, ночью.

    Правила собираются строкой в SQL, поэтому единственная защита — сверка с
    метаданными ORM, которые проверяются `alembic check` против живой базы.
    """

    tables = Base.metadata.tables
    for rule in RULES:
        assert rule.table in tables, f"нет таблицы {rule.table}"
        assert rule.column in tables[rule.table].columns, f"нет колонки {rule.table}.{rule.column}"


def test_every_purge_rule_names_a_real_setting() -> None:
    """Настройка с опечаткой означала бы, что таблица не чистится молча."""

    for rule in RULES:
        assert rule.setting in Settings.model_fields, f"нет настройки {rule.setting}"


def test_purge_statement_is_bounded_by_days() -> None:
    """Запрос обязан фильтровать по времени: DELETE без WHERE — это авария."""

    for rule in RULES:
        sql = str(rule.statement)
        assert "WHERE" in sql
        assert ":days" in sql
