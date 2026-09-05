"""Как писать профиль интересов — и проверка того, что он написан рабочим.

Профиль выглядит как свободный текст, но обрабатывается механически: описание
режется на темы по знакам препинания, каждая тема становится отдельным
поисковым запросом, а явные интересы вдобавок сверяются с текстом материала
буквально. Ни одно из этих правил из поля ввода не видно, и ошибка в профиле не
падает — она просто портит выдачу месяцами.

Поэтому здесь два блока и оба общие:

* `GUIDE` — текст инструкции. Его показывает и бот, и админка: два процесса, и
  две копии одного текста однажды разойдутся.
* `review` — проверка конкретного профиля. Отвечает не «правильно/неправильно»,
  а «вот это работать не будет и вот почему»: список замечаний рядом с полем
  ввода стоит дороже любой инструкции, потому что относится к тому, что человек
  только что написал.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from html import escape

from geonexa_proxima.services.facets import (
    DESCRIPTION_SECTION,
    INTERESTS_SECTION,
    ProfileFacet,
    build_facets,
    interest_variants,
    parse_interests,
    sections,
    split_sentences,
)

CYRILLIC = re.compile("[А-Яа-яЁё]")
LATIN = re.compile("[A-Za-z]")

#: Уровни замечаний. `error` — написанное точно не сработает; `warning` —
#: сработает не так, как человек ожидает; `hint` — можно сделать лучше.
LEVELS = ("error", "warning", "hint")


@dataclass(frozen=True, slots=True)
class Note:
    """Замечание к конкретному профилю."""

    level: str
    text: str
    subject: str = ""


@dataclass(frozen=True, slots=True)
class GuideSection:
    """Раздел инструкции: правило, примеры «как надо» и разбор ошибок."""

    title: str
    body: tuple[str, ...] = ()
    good: tuple[str, ...] = ()
    bad: tuple[tuple[str, str], ...] = ()


GUIDE: tuple[GuideSection, ...] = (
    GuideSection(
        title="Одна мысль — одно предложение",
        body=(
            "Описание режется на темы по точкам, точкам с запятой и переводам "
            "строк. Каждая тема ищется по корпусу отдельно, и материал, попавший "
            "в одну тему целиком, больше не проигрывает материалу, слегка "
            "похожему на весь профиль сразу.",
            "Отсюда единственное правило вёрстки: одна область интересов — одно "
            "предложение или одна строка. Абзац из пяти областей через запятую "
            "останется одной темой, и всё, что в нём есть, снова размоется.",
        ),
        good=(
            "Математическое моделирование в геотехнике: МКЭ, МДЭ, определяющие "
            "соотношения грунтов.\n"
            "Разжижение грунтов при циклических и сейсмических нагрузках.\n"
            "ИИ для обработки данных полевых и лабораторных опытов.",
        ),
        bad=(
            (
                "Интересует геотехника, механика грунтов, разжижение, ИИ, "
                "обработка опытов, мониторинг и всё что рядом",
                "одна тема на шесть областей: вектор станет усреднением между "
                "ними, и точное попадание ни в одну не сработает",
            ),
        ),
    ),
    GuideSection(
        title="Пишите тему, а не должность",
        body=(
            "Тема сравнивается по смыслу с названиями и аннотациями работ. "
            "Работает то, что могло бы стоять в заголовке статьи; не работает "
            "то, что описывает вас, а не предмет.",
            "Полезно называть метод, объект и задачу: «нейросетевые суррогатные "
            "модели для расчёта осадок оснований» точнее, чем «нейросети».",
        ),
        good=(
            "Суррогатные модели на основе нейросетей для расчёта осадок свайных оснований.",
            "Мониторинг оползневых склонов методами InSAR и машинного обучения.",
        ),
        bad=(
            ("Я главный инженер проекта", "должность не встречается в статьях"),
            ("Хочу быть в курсе новинок", "нет предмета — под это подходит всё"),
            ("ИИ", "слишком общо: похоже на любую работу в корпусе"),
        ),
    ),
    GuideSection(
        title="Слишком короткий кусок приклеится к соседнему",
        body=(
            "Фрагмент короче примерно пятнадцати символов отдельной темой не "
            "становится: искать по «Также ИИ» нечего, а место в выдаче такая "
            "тема заняла бы. Он присоединяется к предыдущему предложению.",
            "Перенос строки посреди предложения границей темы не считается — "
            "если следующая строка начинается со строчной буквы, текст "
            "продолжается. Перечисляя темы построчно, начинайте каждую с "
            "заглавной.",
        ),
    ),
    GuideSection(
        title="Пишите по-русски: английскую сторону сделает система",
        body=(
            "Корпус собирается с arXiv, OpenAlex, Crossref, GitHub и КиберЛенинки, "
            "и большая его часть — английская. Описание и темы пишите по-русски: "
            "при каждом сохранении профиль переводится на английский с "
            "терминологией отрасли, и поиск идёт по обеим сторонам — русской и "
            "английской.",
            "У явной темы перевод дописывается через точку с запятой: "
            "«разжижение грунтов» превращается в «soil liquefaction; cyclic "
            "liquefaction; разжижение грунтов». Буквальная сверка проверяет каждое "
            "написание, по смыслу тема берётся целиком. Хотите свой английский "
            "вариант — напишите его сами через «;», система его не тронет.",
        ),
        good=(
            "разжижение грунтов",
            "InSAR; радарная интерферометрия",
            "constitutive model; определяющие соотношения",
        ),
        bad=(
            (
                "liquefaction cyclic soil sand triaxial",
                "набор английских слов вместо темы: перевод не нужен, но смысла "
                "в такой строке для поиска нет — назовите тему одной фразой",
            ),
        ),
    ),
    GuideSection(
        title="Вес: 0-10, и это приоритет, а не важность",
        body=(
            "Вес сравнивает темы между собой, а не задаёт абсолютную планку. "
            "Десятка у всех тем означает ровно то же, что пятёрка у всех.",
            "Рабочий диапазон — 3-7, и 8-10 стоит оставить одной-двум главным "
            "темам. Минус убирает материал из выдачи; минус-тема тоже "
            "сверяется буквально, поэтому ей нужны оба написания — русское "
            "система переведёт сама, а если английское уже есть, оставит его.",
        ),
        good=("liquefaction; разжижение грунтов — вес 8", "asphalt crack detection — минус, вес 5"),
    ),
    GuideSection(
        title="Что делает правка",
        body=(
            "Описание заменяется целиком, а не дополняется. Сохранение поднимает "
            "версию профиля: темы и их векторы пересобираются заново, а "
            "накопленная история оценок к прошлой версии больше не относится.",
            "Поэтому правьте профиль осмысленными заходами, а не по одному слову: "
            "каждое сохранение — это новая версия и новый пересчёт.",
        ),
    ),
)


def review(
    compiled_text: str,
    *,
    facet_limit: int = 8,
    facet_min_chars: int = 16,
) -> list[Note]:
    """Что в этом профиле не сработает — по тому, что реально записано.

    Проверка идёт по `compiled_text`, то есть по тому же тексту, который видит
    поиск. Сверять что-то другое значило бы обещать человеку поведение, которого
    нет.
    """

    notes: list[Note] = []
    parts = sections(compiled_text)
    description = parts.get(DESCRIPTION_SECTION, "").strip()
    facets = build_facets(compiled_text, limit=facet_limit, min_chars=facet_min_chars)

    if not description:
        notes.append(
            Note(
                "warning",
                "Описание пустое: поиск идёт только по базовой таксономии "
                "и одинаков для всех. Опишите интересы обычными словами.",
            )
        )
    else:
        topics = split_sentences(description, min_chars=facet_min_chars)
        # Одна тема на длинное описание или на перечисление через запятую —
        # ровно тот случай, ради которого профиль вообще режется на темы.
        crammed = len(description) > 120 or description.count(",") >= 3
        if len(topics) == 1 and crammed:
            notes.append(
                Note(
                    "warning",
                    "Всё описание — одна тема. Разделите области интересов точками "
                    "или строками, иначе они усреднятся в один вектор.",
                )
            )
        if not topics:
            notes.append(
                Note(
                    "warning",
                    "Из описания не вышло ни одной темы: оно слишком короткое. "
                    "Поиск пойдёт только по профилю целиком.",
                )
            )

    notes.extend(_interest_notes(parts.get(INTERESTS_SECTION, "")))

    if len(facets) >= facet_limit:
        notes.append(
            Note(
                "warning",
                f"Тем набралось {facet_limit} — это предел, и всё, что ниже по "
                "списку, в поиске не участвует. Уберите лишние или объедините.",
            )
        )
    return notes


def _interest_notes(interests_block: str) -> list[Note]:
    """Замечания к явным темам: без английской стороны тема сработает вполсилы.

    Перевод добавляется автоматически при сохранении, так что тема без
    английского написания в `compiled_text` — это либо сбой перевода, либо
    профиль, собранный до его появления. В обоих случаях лечится
    пересохранением, и об этом и говорим.
    """

    notes: list[Note] = []
    for interest in parse_interests(interests_block):
        term = interest.text
        negative = interest.is_negative
        variants = interest_variants(term)
        if any(LATIN.search(variant) for variant in variants):
            continue
        if not CYRILLIC.search(term):
            continue
        notes.append(
            Note(
                "warning" if negative else "hint",
                (
                    f"«{term}»: у минус-темы нет английского написания, и по "
                    "английским статьям она не сработает. Перевод добавляется при "
                    "сохранении — пересохраните тему или допишите вариант через «;»."
                )
                if negative
                else (
                    f"«{term}»: английского написания пока нет — буквальная прибавка "
                    "работает только по русским статьям. Перевод добавляется при "
                    "сохранении; если его нет, пересохраните тему."
                ),
                subject=term,
            )
        )
    return notes


@dataclass(frozen=True, slots=True)
class Preview:
    """Как профиль выглядит с точки зрения поиска."""

    facets: tuple[ProfileFacet, ...] = ()
    notes: tuple[Note, ...] = ()
    dropped: tuple[str, ...] = field(default=())


def preview(
    compiled_text: str,
    *,
    facet_limit: int = 8,
    facet_min_chars: int = 16,
) -> Preview:
    """Темы, которые получатся из текста, вместе с замечаниями.

    То, ради чего это существует: разбиение механическое и из поля ввода не
    видно. Показать результат дешевле, чем объяснить правило, — и человек
    исправляет профиль до того, как месяц получает не тот дайджест.
    """

    facets = build_facets(compiled_text, limit=facet_limit, min_chars=facet_min_chars)
    parts = sections(compiled_text)
    topics = split_sentences(parts.get(DESCRIPTION_SECTION, ""), min_chars=facet_min_chars)
    kept = {facet.text for facet in facets}
    return Preview(
        facets=tuple(facets),
        notes=tuple(
            review(compiled_text, facet_limit=facet_limit, facet_min_chars=facet_min_chars)
        ),
        dropped=tuple(topic for topic in topics if topic not in kept),
    )


#: Лимит одного сообщения Bot API. Берём с запасом: HTML-разметка внутри
#: считается в тот же лимит, а обрезанное посередине сообщение Telegram просто
#: отвергает — команда выглядит неработающей.
TELEGRAM_LIMIT = 3800


def _trim(block: str, limit: int) -> str:
    """Обрезать блок, не оставив половины тега или мнемоники.

    Резать по символу нельзя: обрыв посреди `&amp;` или внутри `<a href="…">`
    даёт «can't parse entities», и Telegram отвергает сообщение целиком —
    то есть обрезка ради спасения ответа его же и убивает.
    """

    cut = block[: limit - 1]
    for marker in ("<", "&"):
        opened = cut.rfind(marker)
        if opened != -1 and marker == "<" and ">" not in cut[opened:]:
            cut = cut[:opened]
        if opened != -1 and marker == "&" and ";" not in cut[opened:]:
            cut = cut[:opened]
    return cut + "…"


def chunk_messages(blocks: Sequence[str], limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Собрать блоки в как можно меньше сообщений, не разрывая блок.

    Два ограничения сразу. Сообщение длиннее лимита Bot API просто отвергает —
    команда выглядит неработающей, а причина видна только в логе. И наоборот,
    Bot API держит около одного сообщения в секунду на чат, поэтому «по блоку
    на сообщение» превращается в `429` с потерей хвоста.

    Блок длиннее лимита обрезается: лучше усечённая тема, чем неотправленный
    ответ.
    """

    messages: list[str] = []
    current = ""
    for block in blocks:
        piece = block if len(block) <= limit else _trim(block, limit)
        candidate = f"{current}\n\n{piece}" if current else piece
        if current and len(candidate) > limit:
            messages.append(current)
            current = piece
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def render_telegram(guide: tuple[GuideSection, ...] = GUIDE) -> list[str]:
    """Инструкция в HTML Telegram, упакованная в как можно меньше сообщений."""

    return chunk_messages(
        [_section_html(index, section) for index, section in enumerate(guide, start=1)]
    )


def _section_html(index: int, section: GuideSection) -> str:
    lines = [f"<b>{index}. {escape(section.title)}</b>"]
    lines.extend(escape(paragraph) for paragraph in section.body)
    if section.good:
        lines.append("<b>Так работает</b>")
        lines.extend(f"<code>{escape(example)}</code>" for example in section.good)
    if section.bad:
        lines.append("<b>Так не работает</b>")
        lines.extend(
            f"<code>{escape(example)}</code>\n— {escape(reason)}" for example, reason in section.bad
        )
    return "\n\n".join(lines)


def as_payload(guide: tuple[GuideSection, ...] = GUIDE) -> list[dict[str, object]]:
    """Инструкция для админки. Тот же текст, что и в боте, без вёрстки."""

    return [
        {
            "title": section.title,
            "body": list(section.body),
            "good": list(section.good),
            "bad": [{"example": example, "reason": reason} for example, reason in section.bad],
        }
        for section in guide
    ]


__all__ = [
    "GUIDE",
    "TELEGRAM_LIMIT",
    "GuideSection",
    "Note",
    "Preview",
    "as_payload",
    "chunk_messages",
    "preview",
    "render_telegram",
    "review",
]
