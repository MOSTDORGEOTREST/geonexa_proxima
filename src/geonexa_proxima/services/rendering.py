"""Форматы дайджеста: карточки для лички, один пост для канала.

Разные форматы — не украшение. В личке дайджест это диалог: карточка на
материал, под каждой кнопки обратной связи. В канале пост читают подписчики,
которые в него не отвечают, и десять сообщений подряд там выглядят как флуд,
а лента канала — как лог. Поэтому каналу нужен один связный пост.

Лимит Telegram — 4096 символов на сообщение. Разбивать пост можно только по
границе материала: обрыв на середине заголовка читается как поломка.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from typing import Any

from geonexa_proxima.telegram.keyboards import feedback_keyboard

#: Запас к лимиту Telegram: HTML-разметка считается в символах, и упереться в
#: 4096 ровно означает получить отказ на публикации, а не при вёрстке.
MESSAGE_LIMIT = 3900

FORMATS = ("cards", "compact", "single_message", "digest_post")

#: Заголовки разделов. Порядок задаёт структуру поста.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("paper", "Статьи"),
    ("method", "Методы"),
    ("software", "Инструменты"),
    ("dataset", "Данные"),
)


@dataclass(frozen=True, slots=True)
class RenderedItem:
    """Материал в том виде, в каком его достаточно для любого формата."""

    item_id: str
    title: str
    url: str | None
    kind: str = "paper"
    #: Оценка, к которой привязывается обратная связь. Без неё кнопки некуда
    #: адресовать, и материал уезжает без них.
    profile_score_id: str | None = None
    score: float | None = None
    personal_score: float | None = None
    reason: str | None = None
    summary: str | None = None
    venue: str | None = None
    published: date | None = None

    @classmethod
    def from_candidate(cls, candidate: Any) -> RenderedItem:
        item = candidate.item
        score_id = getattr(candidate, "profile_score_id", None)
        return cls(
            item_id=str(item.id),
            title=item.title,
            profile_score_id=str(score_id) if score_id else None,
            url=str(item.canonical_url) if getattr(item, "canonical_url", None) else None,
            kind=str(getattr(item.kind, "value", item.kind)),
            score=item.rank.total_score if getattr(item, "rank", None) else None,
            personal_score=getattr(candidate, "personal_score", None),
            reason=getattr(candidate, "explanation", None),
            summary=item.analysis.summary if getattr(item, "analysis", None) else None,
            venue=getattr(item, "venue", None),
            published=getattr(item, "publication_date", None),
        )


def render_digest(
    items: Sequence[RenderedItem],
    *,
    fmt: str = "cards",
    heading: str = "Проксима",
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    limit: int = MESSAGE_LIMIT,
) -> list[dict[str, Any]]:
    """Собрать дайджест в блоки для очереди доставки.

    Каждый блок — одно сообщение. Для карточек это один материал (к нему
    привязываются кнопки обратной связи, поэтому `item_id` обязателен), для
    поста в канал — часть общего текста, и `item_id` у неё нет.
    """

    if fmt not in FORMATS:
        raise ValueError(f"неизвестный формат доставки: {fmt}")
    if not items:
        return []
    if fmt == "digest_post":
        return _post(
            items, heading=heading, period_start=period_start, period_end=period_end, limit=limit
        )
    if fmt == "single_message":
        return _single(items, heading=heading, limit=limit)
    blocks: list[dict[str, Any]] = []
    for item in items:
        block: dict[str, Any] = {
            "item_id": item.item_id,
            "text": _card(item, compact=fmt == "compact"),
        }
        # Кнопки только у карточек: в канале нажимать их некому, а в длинном
        # посте они относились бы неизвестно к какому материалу.
        if item.profile_score_id:
            block["reply_markup"] = feedback_keyboard(item.profile_score_id)
        blocks.append(block)
    return blocks


def _card(item: RenderedItem, *, compact: bool = False) -> str:
    """Карточка одного материала."""

    title = escape(item.title[:500])
    head = (
        f'<a href="{escape(item.url, quote=True)}">{title}</a>' if item.url else f"<b>{title}</b>"
    )
    lines = [head]
    meta = _meta(item)
    if meta:
        lines.append(f"<i>{escape(meta)}</i>")
    if not compact:
        if item.reason:
            lines.append(escape(item.reason[:700]))
        elif item.summary:
            lines.append(escape(item.summary[:700]))
    return "\n".join(lines)


def _meta(item: RenderedItem) -> str:
    parts: list[str] = []
    if item.venue:
        parts.append(item.venue[:120])
    if item.published:
        parts.append(item.published.strftime("%d.%m.%Y"))
    if item.personal_score is not None:
        parts.append(f"совпадение {item.personal_score * 10:.1f}/10")
    elif item.score is not None:
        parts.append(f"оценка {item.score:.1f}/10")
    return " · ".join(parts)


def _single(items: Sequence[RenderedItem], *, heading: str, limit: int) -> list[dict[str, Any]]:
    """Всё одним сообщением, без разбиения по разделам."""

    blocks = [f"<b>{escape(heading)}</b>"]
    blocks.extend(_card(item, compact=True) for item in items)
    return [{"text": chunk} for chunk in _chunks(blocks, limit)]


def _post(
    items: Sequence[RenderedItem],
    *,
    heading: str,
    period_start: datetime | None,
    period_end: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Пост для канала: заголовок, разделы по видам, нумерованные материалы.

    Нумерация сквозная и не начинается заново в каждой части: читатель второй
    части должен понимать, что это продолжение, а не другой дайджест.
    """

    period = _period(period_start, period_end)
    blocks: list[str] = [
        f"<b>{escape(heading)}</b>" + (f"\n<i>{escape(period)}</i>" if period else "")
    ]

    by_kind: dict[str, list[RenderedItem]] = {}
    for item in items:
        by_kind.setdefault(item.kind, []).append(item)

    number = 0
    for kind, label in SECTIONS:
        group = by_kind.pop(kind, [])
        if not group:
            continue
        blocks.append(f"<b>{label}</b>")
        for item in group:
            number += 1
            blocks.append(_post_item(item, number))
    # Виды, которых нет в SECTIONS, теряться не должны.
    for rest in by_kind.values():
        for item in rest:
            number += 1
            blocks.append(_post_item(item, number))

    chunks = _chunks(blocks, limit)
    if len(chunks) > 1:
        total = len(chunks)
        chunks = [
            chunk + f"\n\n<i>{index}/{total}</i>" for index, chunk in enumerate(chunks, start=1)
        ]
    return [{"text": chunk} for chunk in chunks]


def _post_item(item: RenderedItem, number: int) -> str:
    title = escape(item.title[:400])
    head = (
        f'{number}. <a href="{escape(item.url, quote=True)}">{title}</a>'
        if item.url
        else f"{number}. <b>{title}</b>"
    )
    lines = [head]
    meta = _meta(item)
    if meta:
        lines.append(f"<i>{escape(meta)}</i>")
    body = item.reason or item.summary
    if body:
        lines.append(escape(body[:400]))
    return "\n".join(lines)


def _period(start: datetime | None, end: datetime | None) -> str:
    if start and end:
        if start.date() == end.date():
            return start.strftime("%d.%m.%Y")
        return f"{start.strftime('%d.%m')} — {end.strftime('%d.%m.%Y')}"
    if end:
        return end.strftime("%d.%m.%Y")
    return ""


def _chunks(blocks: Sequence[str], limit: int) -> list[str]:
    """Склеить блоки в сообщения, не разрывая блок посередине.

    Блок длиннее лимита сам по себе — аномалия (обрезка полей это исключает),
    но если он всё же случился, лучше отправить его усечённым, чем не
    отправить дайджест вовсе.
    """

    chunks: list[str] = []
    current = ""
    for block in blocks:
        piece = block if len(block) <= limit else block[: limit - 1] + "…"
        candidate = f"{current}\n\n{piece}" if current else piece
        if current and len(candidate) > limit:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
