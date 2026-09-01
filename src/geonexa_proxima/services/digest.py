"""Отбор материалов в дайджест и безопасная для Telegram вёрстка.

Весь текст здесь читает человек, поэтому он на русском: этот форматтер
используют и бот по команде, и воркер плановой рассылки.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from html import escape
from typing import TYPE_CHECKING

from geonexa_proxima.domain import ItemKind, StoredItem
from geonexa_proxima.ports import ItemRepository

if TYPE_CHECKING:
    from geonexa_proxima.services.personalization import PersonalizationService, PersonalizedItem


class DigestFormatter:
    def __init__(self, *, max_message_length: int = 3900) -> None:
        self.max_message_length = max_message_length

    def format(self, items: Sequence[StoredItem], *, heading: str = "Дайджест") -> list[str]:
        if not items:
            return [f"<b>{escape(heading)}</b>\n\nПодходящих материалов нет."]
        groups = {
            ItemKind.PAPER: "Статьи",
            ItemKind.METHOD: "Методы",
            ItemKind.SOFTWARE: "Инструменты",
            ItemKind.DATASET: "Данные",
        }
        blocks: list[str] = [f"<b>{escape(heading)}</b>"]
        for kind, label in groups.items():
            matching = [item for item in items if item.kind == kind]
            if not matching:
                continue
            blocks.append(f"<b>{label}</b>")
            blocks.extend(self.format_item(item) for item in matching)
        return self._chunk(blocks)

    def format_item(self, item: StoredItem) -> str:
        score = f"{item.rank.total_score:.1f}/10" if item.rank else "без оценки"
        title = escape(item.title[:500])
        title_line = (
            f'<a href="{escape(item.canonical_url, quote=True)}">{title}</a>'
            if item.canonical_url
            else f"<b>{title}</b>"
        )
        details = [f"Оценка: {score}"]
        if item.rank:
            details.append(escape(item.rank.reason[:500]))
        if item.analysis:
            details.append(escape(item.analysis.summary[:700]))
        return title_line + "\n" + "\n".join(details)

    def format_personalized_item(self, candidate: PersonalizedItem) -> str:
        base = self.format_item(candidate.item)
        details = [f"<b>Персональная оценка: {candidate.personal_score * 10:.1f}/10</b>"]
        if candidate.explanation:
            details.append(escape(candidate.explanation[:700]))
        return base + "\n" + "\n".join(details)

    def _chunk(self, blocks: Sequence[str]) -> list[str]:
        chunks: list[str] = []
        current = ""
        for block in blocks:
            candidate = f"{current}\n\n{block}" if current else block
            if current and len(candidate) > self.max_message_length:
                chunks.append(current)
                current = block
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks


class DigestBuilder:
    def __init__(
        self,
        repository: ItemRepository,
        formatter: DigestFormatter | None = None,
        personalization: PersonalizationService | None = None,
    ) -> None:
        self.repository = repository
        self.formatter = formatter or DigestFormatter()
        self.personalization = personalization

    async def build(
        self,
        *,
        minimum_score: float,
        limit: int = 20,
        heading: str = "Дайджест",
        kinds: set[ItemKind] | None = None,
        since: datetime | None = None,
    ) -> list[str]:
        items = await self.list_items(
            minimum_score=minimum_score,
            limit=limit,
            kinds=kinds,
            since=since,
        )
        return self.formatter.format(items, heading=heading)

    async def list_items(
        self,
        *,
        minimum_score: float,
        limit: int = 20,
        kinds: set[ItemKind] | None = None,
        since: datetime | None = None,
    ) -> list[StoredItem]:
        items = await self.repository.list_digest_candidates(minimum_score, limit, since)
        return [item for item in items if kinds is None or item.kind in kinds]

    async def list_personalized(
        self,
        profile: object,
        *,
        limit: int = 20,
        kinds: set[ItemKind] | None = None,
        since: datetime | None = None,
        minimum_global_score: float = 0,
        minimum_personal_score: float = 0,
    ) -> list[PersonalizedItem]:
        if self.personalization is None:
            raise RuntimeError("Сервис персонализации не настроен")
        return await self.personalization.rank(
            profile,
            limit=limit,
            kinds=kinds,
            since=since,
            minimum_global_score=minimum_global_score,
            minimum_personal_score=minimum_personal_score,
        )
