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

    #: Дополнительное условие: не всякую строку можно удалять по возрасту.
    predicate: str = ""

    @property
    def statement(self) -> Any:
        # Имена таблицы и колонки — из этого модуля, не извне: подстановка
        # безопасна, а параметром PostgreSQL их принять не может.
        #
        # Удаление идёт пачками: один `DELETE` по таблице в миллионы строк не
        # успевает уложиться в `statement_timeout` и отваливается целиком —
        # таблица не чистится, растёт, и следующая попытка отваливается
        # быстрее. Пачками уборка идёт медленнее, но доходит до конца.
        extra = f" AND {self.predicate}" if self.predicate else ""
        return text(
            f"WITH doomed AS ("
            f"  SELECT ctid FROM {self.table}"
            f"   WHERE {self.column} < now() - make_interval(days => :days){extra}"
            f"   LIMIT :batch"
            f") DELETE FROM {self.table} AS victim USING doomed"
            f" WHERE victim.ctid = doomed.ctid"
        )


RULES: tuple[RetentionRule, ...] = (
    RetentionRule("harvest_decisions", "created_at", "harvest_decision_retention_days"),
    RetentionRule("subscriber_activity", "occurred_at", "metrics_retention_days"),
    RetentionRule("llm_call_log", "created_at", "metrics_retention_days"),
    RetentionRule("chat_events", "occurred_at", "metrics_retention_days"),
    RetentionRule("delivery_messages", "created_at", "metrics_retention_days"),
    # Очередь рассылки росла без границы: её читает каждая загрузка дашборда
    # (GROUP BY по всей таблице) и ночное обслуживание. Живые задания не
    # трогаем — только те, что уже отработали.
    RetentionRule(
        "delivery_jobs",
        "created_at",
        "metrics_retention_days",
        predicate="status IN ('sent', 'failed', 'skipped', 'cancelled')",
    ),
)

#: Сколько строк удаляем за один заход. Достаточно, чтобы уборка догоняла
#: накопление, и достаточно мало, чтобы уложиться в `statement_timeout`.
BATCH = 20_000


async def purge(engine: AsyncEngine, settings: Any) -> dict[str, int]:
    """Удалить всё, что пережило свой срок. Возвращает число строк по таблицам."""

    removed: dict[str, int] = {}
    for rule in RULES:
        days = getattr(settings, rule.setting, None)
        if not days:
            continue
        total = 0
        # Пачками, пока есть что удалять, но не бесконечно: ограничение по
        # числу заходов не даёт ночному обслуживанию превратиться в вечное.
        for _ in range(50):
            async with engine.begin() as connection:
                result = await connection.execute(
                    rule.statement, {"days": int(days), "batch": BATCH}
                )
            count = int(result.rowcount or 0)
            total += count
            if count < BATCH:
                break
        if total:
            removed[rule.table] = total
    return removed
