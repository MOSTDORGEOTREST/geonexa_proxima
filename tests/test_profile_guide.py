"""Инструкция по профилю и проверка того, что профиль написан рабочим.

Профиль — свободный текст, но обрабатывается механически: описание режется на
темы, каждая тема ищется отдельно, а явные темы вдобавок сверяются с текстом
статьи буквально. Ошибка здесь не падает — она молча портит выдачу месяцами,
поэтому проверка и предпросмотр закрыты тестами наравне с самим поиском.
"""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient

from geonexa_proxima.api import create_app
from geonexa_proxima.config import Settings
from geonexa_proxima.services.facets import interest_variants, parse_interests
from geonexa_proxima.services.profile_guide import (
    GUIDE,
    TELEGRAM_LIMIT,
    as_payload,
    preview,
    render_telegram,
    review,
)

CYRILLIC = re.compile("[А-Яа-яЁё]")


def _profile(description: str = "", interests: str = "") -> str:
    blocks = ["Base taxonomy:\nинженерная геология, геотехника"]
    if description:
        blocks.append(f"Profile description:\n{description}")
    if interests:
        blocks.append(f"Explicit interests:\n{interests}")
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Два написания одной темы                                                     #
# --------------------------------------------------------------------------- #


def test_interest_carries_both_spellings() -> None:
    """Тема сверяется с текстом статьи буквально, а корпус английский.

    Без второго написания русский термин не совпадёт никогда и молча
    превратится в постоянную нулевую добавку — самый дорогой вид ошибки,
    потому что снаружи он неотличим от «просто ничего не нашлось».
    """

    assert interest_variants("liquefaction; разжижение грунтов") == [
        "liquefaction",
        "разжижение грунтов",
    ]
    assert interest_variants("  liquefaction ;; ") == ["liquefaction"]
    assert interest_variants("разжижение грунтов") == ["разжижение грунтов"]


def test_literal_match_accepts_any_spelling() -> None:
    """Английская статья должна совпасть с русско-английской темой."""

    from types import SimpleNamespace

    from geonexa_proxima.domain import ItemKind, StoredItem
    from geonexa_proxima.services.personalization import _interest_score

    item = StoredItem(
        kind=ItemKind.PAPER,
        title="Deep learning for soil liquefaction assessment",
        abstract="We predict liquefaction triggering from CPT data.",
    )
    bilingual = SimpleNamespace(
        query="liquefaction; разжижение грунтов", weight=5.0, polarity="positive"
    )
    russian_only = SimpleNamespace(query="разжижение грунтов", weight=5.0, polarity="positive")

    assert _interest_score(item, [bilingual], []) > 0.5
    # Русский вариант в одиночку не совпадает — ровно то, о чём предупреждает
    # инструкция и что ловит проверка профиля.
    assert _interest_score(item, [russian_only], []) == pytest.approx(0.5)


def test_explicit_interests_parse_back_from_the_compiled_text() -> None:
    interests = parse_interests(
        "- positive: liquefaction; разжижение грунтов (weight=8)\n"
        "- negative: asphalt crack detection (weight=3)"
    )

    assert [item.text for item in interests] == [
        "liquefaction; разжижение грунтов",
        "asphalt crack detection",
    ]
    assert [item.weight for item in interests] == [8.0, 3.0]
    assert interests[1].is_negative


# --------------------------------------------------------------------------- #
# Проверка профиля                                                             #
# --------------------------------------------------------------------------- #


def test_russian_only_interest_is_flagged() -> None:
    notes = review(_profile(interests="- positive: разжижение грунтов (weight=5)"))

    assert [note.level for note in notes] == ["warning", "warning"]  # плюс пустое описание
    assert any("разжижение грунтов" in note.subject for note in notes)


def test_russian_only_minus_interest_is_an_error_not_a_warning() -> None:
    """Минус-тема без английского написания не работает вовсе.

    Плюс-тема хотя бы участвует в поиске по смыслу; минус существует только
    ради буквальной сверки, и без совпадения он не делает ничего.
    """

    notes = review(
        _profile(
            description="Разжижение грунтов при циклических нагрузках.",
            interests="- negative: трещины в асфальте (weight=3)",
        )
    )

    assert [note.level for note in notes] == ["error"]


def test_bilingual_interest_passes_without_notes() -> None:
    notes = review(
        _profile(
            description="Разжижение грунтов при циклических нагрузках.",
            interests="- positive: liquefaction; разжижение грунтов (weight=8)",
        )
    )

    assert notes == []


def test_everything_crammed_into_one_sentence_is_flagged() -> None:
    """Тот случай, ради которого профиль вообще режется на темы."""

    notes = review(
        _profile(description="Интересует геотехника, механика грунтов, разжижение, ИИ и мониторинг")
    )

    assert any("одна тема" in note.text for note in notes)


def test_empty_description_is_flagged() -> None:
    assert any("Описание пустое" in note.text for note in review(_profile()))


def test_facet_limit_is_reported() -> None:
    """Тема сверх лимита в поиске не участвует — и об этом надо сказать."""

    description = " ".join(f"Тема номер {index} про геотехнику и модели." for index in range(6))
    notes = review(_profile(description=description), facet_limit=3)

    assert any("предел" in note.text for note in notes)


# --------------------------------------------------------------------------- #
# Предпросмотр                                                                 #
# --------------------------------------------------------------------------- #


def test_preview_shows_the_split_and_what_did_not_survive() -> None:
    result = preview(
        _profile(
            description=(
                "Математическое моделирование в геотехнике и механике грунтов.\n"
                "ИИ для обработки данных полевых опытов."
            ),
            interests="- positive: liquefaction; разжижение грунтов (weight=8)",
        )
    )

    assert [facet.source for facet in result.facets] == ["description", "description", "interest"]
    assert result.notes == ()
    assert result.dropped == ()


def test_preview_reports_a_fragment_that_did_not_become_a_topic() -> None:
    """Молча пропавший кусок описания — самая обидная из ошибок.

    Человек его написал и уверен, что он работает; на деле он приклеился к
    соседней теме, потому что слишком короткий.
    """

    result = preview(_profile(description="Разжижение грунтов при нагрузках. Также ИИ."))

    assert len(result.facets) == 1
    assert result.facets[0].text.endswith("Также ИИ.")


# --------------------------------------------------------------------------- #
# Один текст на бота и админку                                                 #
# --------------------------------------------------------------------------- #


def test_guide_is_russian_and_fits_telegram() -> None:
    """Инструкция должна дойти целиком и не упереться в лимиты Bot API.

    Два ограничения сразу: сообщение длиннее лимита Telegram отвергает, а
    «по разделу на сообщение» упирается в лимит частоты (около одного
    сообщения в секунду на чат) и теряет хвост инструкции.
    """

    messages = render_telegram()

    assert messages, "инструкция не должна быть пустой"
    assert len(messages) <= 3, "слишком много сообщений подряд — это 429 от Bot API"
    assert all(CYRILLIC.search(message) for message in messages)
    assert all(len(message) <= TELEGRAM_LIMIT for message in messages)
    # Ни один раздел не потерялся при упаковке.
    joined = "\n\n".join(messages)
    assert all(section.title in joined for section in GUIDE)


def test_chunking_never_emits_an_oversized_message() -> None:
    """Блок длиннее лимита обрезается, а не роняет отправку целиком."""

    from geonexa_proxima.services.profile_guide import chunk_messages

    messages = chunk_messages(["a" * 10, "b" * 500, "c" * 10], limit=100)

    assert all(len(message) <= 100 for message in messages)
    assert messages[0].startswith("a" * 10)
    assert "…" in messages[1]


def test_guide_payload_carries_the_same_sections() -> None:
    """Админка и бот показывают одну инструкцию, а не две похожие."""

    payload = as_payload()

    assert [section["title"] for section in payload] == [section.title for section in GUIDE]
    assert all(section["body"] for section in payload)
    assert any(section["bad"] for section in payload), "разбор ошибок — половина пользы"


def test_every_section_explains_a_rule_that_actually_exists() -> None:
    """Инструкция обещает поведение, которое должно быть в коде.

    Правило, которого нет, хуже отсутствующего: человек по нему пишет профиль.
    """

    text = "\n".join("\n".join([section.title, *section.body, *section.good]) for section in GUIDE)
    for promise in ("точк", "строк", ";", "0-10", "заменя"):
        assert promise in text, promise


# --------------------------------------------------------------------------- #
# Адреса API                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_guide_and_preview_are_not_shadowed_by_the_profile_id_route() -> None:
    """`/profiles/guide` не должен разбираться как UUID профиля.

    Порядок объявления маршрутов в FastAPI решает: соседний
    `/profiles/{profile_id}` при неудачном порядке съел бы оба адреса и отвечал
    бы 422 вместо инструкции.
    """

    application = create_app(settings=Settings(_env_file=None, admin_password="test-password"))
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        guide = await client.get("/api/admin/profiles/guide")
        facets = await client.post("/api/admin/profiles/preview", json={"description": "x"})

    # Без токена — 401/403, но именно от гейта авторизации, а не 422 от разбора
    # пути: значит, адрес нашёлся.
    assert guide.status_code in {401, 403}
    assert facets.status_code in {401, 403}
