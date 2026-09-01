"""Форматы дайджеста: карточки для лички, один пост для канала."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from geonexa_proxima.services.rendering import (
    MESSAGE_LIMIT,
    RenderedItem,
    render_digest,
)


def _item(index: int, *, kind: str = "paper", title: str | None = None) -> RenderedItem:
    return RenderedItem(
        item_id=f"item-{index}",
        title=title or f"Физически информированные сети для КПТ №{index}",
        url=f"https://example.org/{index}",
        kind=kind,
        score=8.0,
        personal_score=0.82,
        reason="Совпадает с интересом «разжижение грунтов».",
        venue="Géotechnique",
        published=date(2026, 3, 14),
    )


def test_cards_keep_one_item_per_message() -> None:
    """Кнопки обратной связи привязываются к материалу — значит, и сообщение к нему."""

    blocks = render_digest([_item(i) for i in range(3)], fmt="cards")

    assert len(blocks) == 3
    assert [b["item_id"] for b in blocks] == ["item-0", "item-1", "item-2"]


def test_post_is_a_single_message_for_a_channel() -> None:
    """Десять сообщений подряд в канале выглядят как флуд, а не как дайджест."""

    blocks = render_digest([_item(i) for i in range(6)], fmt="digest_post")

    assert len(blocks) == 1
    text = blocks[0]["text"]
    assert "item_id" not in blocks[0]
    # Все материалы на месте и пронумерованы сквозным счётом.
    for number in range(1, 7):
        assert f"{number}. " in text


def test_post_groups_by_kind_in_fixed_order() -> None:
    items = [
        _item(1, kind="dataset"),
        _item(2, kind="paper"),
        _item(3, kind="software"),
    ]
    text = render_digest(items, fmt="digest_post")[0]["text"]

    assert text.index("Статьи") < text.index("Инструменты") < text.index("Данные")


def test_post_splits_on_item_boundaries_and_numbers_parts() -> None:
    """Разрыв посреди заголовка читается как поломка, поэтому режем по границам."""

    long_title = "Оценка устойчивости склона методом конечных элементов " * 6
    items = [_item(i, title=f"{long_title}{i}") for i in range(20)]

    blocks = render_digest(items, fmt="digest_post")

    assert len(blocks) > 1
    for block in blocks:
        assert len(block["text"]) <= MESSAGE_LIMIT + 40  # запас на пометку части
        # Ни одна часть не начинается с обрывка предыдущего материала.
        assert not block["text"].startswith("…")
    assert "1/" in blocks[0]["text"] and f"{len(blocks)}" in blocks[-1]["text"]


def test_numbering_continues_across_parts() -> None:
    """Читатель второй части должен видеть продолжение, а не новый дайджест."""

    long_title = "Мониторинг деформаций по данным InSAR временных рядов " * 6
    items = [_item(i, title=f"{long_title}{i}") for i in range(20)]

    blocks = render_digest(items, fmt="digest_post")
    joined = "\n".join(block["text"] for block in blocks)

    assert "20. " in joined
    assert joined.count("1. ") >= 1


def test_period_appears_in_the_heading() -> None:
    blocks = render_digest(
        [_item(1)],
        fmt="digest_post",
        heading="Проксима",
        period_start=datetime(2026, 3, 1, tzinfo=UTC),
        period_end=datetime(2026, 3, 8, tzinfo=UTC),
    )

    assert "01.03 — 08.03.2026" in blocks[0]["text"]


def test_html_in_titles_is_escaped() -> None:
    """Заголовок статьи — чужой текст: незакрытый тег ломает всё сообщение."""

    item = RenderedItem(
        item_id="x",
        title="<script>alert(1)</script> & прочее",
        url="https://example.org/?a=1&b=2",
        kind="paper",
    )
    for fmt in ("cards", "compact", "single_message", "digest_post"):
        text = render_digest([item], fmt=fmt)[0]["text"]
        assert "<script>" not in text
        assert "&lt;script&gt;" in text


def test_empty_digest_produces_no_blocks() -> None:
    """Пустой дайджест не отправляется вовсе — сообщать не о чем."""

    assert render_digest([], fmt="digest_post") == []
    assert render_digest([], fmt="cards") == []


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(ValueError, match="неизвестный формат"):
        render_digest([_item(1)], fmt="carrier-pigeon")


def test_compact_drops_explanations_but_keeps_identity() -> None:
    full = render_digest([_item(1)], fmt="cards")[0]["text"]
    compact = render_digest([_item(1)], fmt="compact")[0]["text"]

    assert len(compact) < len(full)
    assert "example.org/1" in compact
    assert "Géotechnique" in compact


def test_cards_carry_feedback_buttons() -> None:
    """Без кнопок обучаемый профиль недостижим: обратной связи неоткуда взяться.

    Плановый дайджест отправляет воркер доставки, а не бот, — и раньше он слал
    голый текст, так что кнопки доезжали только до сообщений, запрошенных
    командой вручную.
    """

    item = RenderedItem(
        item_id="i1",
        title="Проба",
        url=None,
        profile_score_id="11111111-1111-1111-1111-111111111111",
    )

    block = render_digest([item], fmt="cards")[0]

    assert "reply_markup" in block
    buttons = [b for row in block["reply_markup"]["inline_keyboard"] for b in row]
    codes = {
        b["callback_data"].split(":")[1] for b in buttons if b["callback_data"].startswith("fb:")
    }
    assert codes == {"vi", "u", "ni", "s", "d"}
    # callback_data ограничена 64 байтами, а UUID занимает 36 — коды короткие не зря.
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)


def test_post_has_no_buttons() -> None:
    """В канале нажимать их некому, а в длинном посте непонятно, к чему они."""

    item = RenderedItem(
        item_id="i1",
        title="Проба",
        url=None,
        profile_score_id="11111111-1111-1111-1111-111111111111",
    )

    assert "reply_markup" not in render_digest([item], fmt="digest_post")[0]


def test_item_without_score_gets_no_buttons() -> None:
    """Кнопке некуда адресоваться — лучше без неё, чем в никуда."""

    item = RenderedItem(item_id="i1", title="Проба", url=None)

    assert "reply_markup" not in render_digest([item], fmt="cards")[0]
