"""Удаление старых сырых событий.

Модуль называется `purge`, а не `retention`: `RETENTION_ROLLUP` рядом — это
удержание когорт, продуктовая метрика. Два разных смысла под одним словом
в соседних модулях гарантированно кого-нибудь запутают.

Агрегаты живут долго и весят мало; сырые строки — наоборот. `harvest_decisions`
растёт быстрее всего: при включённом `HARVEST_STORE_REJECTED` туда попадает
каждый отклонённый материал, а отклоняется большинство. Без уборки таблица
переживёт полезность своих строк на годы.

Сроки хранения — настройки, а не константы: месяц калибровки порогов и год
продуктовой аналитики хранятся по-разному, и решает это администратор.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True, slots=True)
class RetentionRule:
    """Что чистим, по какой колонке и на основании какой настройки."""

    table: str
    column: str
    setting: str

    @property
    def statement(self) -> Any:
        # Имена таблицы и колонки — из этого модуля, не извне: подстановка
        # безопасна, а параметром PostgreSQL их принять не может.
        return text(
            f"DELETE FROM {self.table} WHERE {self.column} < now() - make_interval(days => :days)"
        )


RULES: tuple[RetentionRule, ...] = (
    RetentionRule("harvest_decisions", "created_at", "harvest_decision_retention_days"),
    RetentionRule("subscriber_activity", "occurred_at", "metrics_retention_days"),
    RetentionRule("llm_call_log", "created_at", "metrics_retention_days"),
    RetentionRule("chat_events", "occurred_at", "metrics_retention_days"),
    RetentionRule("delivery_messages", "created_at", "metrics_retention_days"),
)


async def purge(engine: AsyncEngine, settings: Any) -> dict[str, int]:
    """Удалить всё, что пережило свой срок. Возвращает число строк по таблицам."""

    removed: dict[str, int] = {}
    for rule in RULES:
        days = getattr(settings, rule.setting, None)
        if not days:
            continue
        async with engine.begin() as connection:
            result = await connection.execute(rule.statement, {"days": int(days)})
        if result.rowcount:
            removed[rule.table] = int(result.rowcount)
    return removed
