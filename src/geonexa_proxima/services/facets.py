"""Разбор профиля интересов на грани — узкие темы, по которым ищут отдельно.

Зачем это нужно. Профиль вроде «математические модели в геотехнике, механика и
разжижение грунтов. Также ИИ в обработке опытов» — это не одна тема, а две.
Эмбеддинг всего текста даёт центроид между ними, и статья, глубоко попадающая в
одну тему, получает средний косинус: центроид оттянут второй темой. Чем больше
интересов у человека, тем сильнее размывание — и тем увереннее выпадает как раз
то, что ему нужнее всего.

Лечение — искать не только полным профилем, но и каждой гранью по отдельности, а
итоговую близость брать максимумом. Тогда «попал в одну тему целиком» перестаёт
проигрывать «слегка похож на всё сразу».

Модуль намеренно чистый: на входе строка, на выходе список граней. Разбор
профиля — это то место, где ошибка не падает, а тихо портит выдачу, и проверять
её надо таблицей примеров, а не прогоном дайджеста.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from typing import Final

#: Заголовки разделов, которые пишет `ProfileCompiler`. Держим их здесь же,
#: чтобы разбор и сборка расходились с ошибкой теста, а не молча.
DESCRIPTION_SECTION: Final = "Profile description"
INTERESTS_SECTION: Final = "Explicit interests"
TAXONOMY_SECTION: Final = "Base taxonomy"
SIGNALS_SECTION: Final = "Learned interest signals"

_SECTIONS: Final = (TAXONOMY_SECTION, DESCRIPTION_SECTION, INTERESTS_SECTION, SIGNALS_SECTION)

#: Заголовок раздела: с начала строки и до конца строки.
_HEADER = re.compile(
    r"^(?P<name>" + "|".join(re.escape(name) for name in _SECTIONS) + r"):[ \t]*$",
    re.MULTILINE,
)

#: Строка явного интереса: `- positive: разжижение грунтов (weight=5)`.
_INTEREST_LINE = re.compile(
    r"^-\s*(?P<polarity>positive|negative):\s*(?P<text>.+?)\s*\(weight=(?P<weight>[^)]*)\)\s*$",
    re.MULTILINE,
)

#: Кусок текста до разделителя включительно, вместе с пробелами за ним.
#:
#: Разделитель оставляем в куске: без него дробное число «0.5» уже не склеить
#: обратно. Пробелы и переводы строк — тоже: куски склеиваются встык, и без них
#: две темы, разделённые переводом строки, срастались бы в «нагрузках.ИИ», а
#: добавлять пробел при склейке нельзя — он разорвал бы то самое «0.5».
#: Лишние пробелы схлопнет `_clean`.
_CHUNK = re.compile(r"[^.;!?…\n]+[.;!?…]*\s*")

_WHITESPACE = re.compile(r"\s+")

#: Перенос строки посреди предложения. Строчная буква или цифра в начале
#: следующей строки означает, что предложение продолжается: так выглядит
#: описание, набранное с жёсткими переносами. Перенос перед заглавной буквой,
#: наоборот, границу темы обозначает — люди перечисляют интересы построчно.
_WRAPPED = re.compile(r"\n[ \t]*(?=[a-zа-яё0-9])")

#: Что срезается с краёв грани: пробелы, маркеры списка и тире всех видов.
_TRIM = " \t-\u2013\u2014\u2022"

#: Индекс грани «весь профиль». Ноль занят им всегда, даже когда граней нет, —
#: это же и ключ кэша вектора, и он не должен меняться от правки описания.
FULL_PROFILE: Final = 0


@dataclass(frozen=True, slots=True)
class ProfileFacet:
    """Одна тема профиля вместе с тем, откуда она взялась."""

    index: int
    text: str
    source: str

    @property
    def is_full_profile(self) -> bool:
        return self.index == FULL_PROFILE

    @property
    def text_hash(self) -> str:
        """Отпечаток текста — вторая половина ключа кэша векторов.

        Номер грани позиционный, и какой текст под ним окажется, зависит ещё и
        от настроек разбиения. Версия профиля про них не знает, поэтому без
        отпечатка смена `PROFILE_FACET_MIN_CHARS` подсунула бы под старым
        номером чужой вектор — молча и только в поиске.
        """

        return sha256(self.text.encode("utf-8")).hexdigest()[:16]


def sections(compiled_text: str) -> dict[str, str]:
    """Разобрать `compiled_text` на разделы, которые писал компилятор.

    Разбор идёт по заголовкам, а не по пустым строкам: описание профиля пишет
    человек, и абзац в нём — обычное дело. Деление по `\\n\\n` резало бы такое
    описание на куски и приписывало половину следующему разделу.
    """

    found: dict[str, str] = {}
    matches = list(_HEADER.finditer(compiled_text))
    for position, match in enumerate(matches):
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(compiled_text)
        found[match.group("name")] = compiled_text[start:end].strip()
    return found


def split_sentences(text: str, *, min_chars: int) -> list[str]:
    """Разрезать текст по точкам, точкам с запятой и переводам строк.

    Куски копятся в буфере и отдаются, только когда набралось достаточно букв.
    Поэтому «мод. 2.5 кг» не рассыпается на три обрывка, а «Также ИИ.» не
    остаётся висеть гранью из одного слова: искать по ней нечего, а место в
    квоте она бы заняла. Хвост, которому не хватило длины, приклеивается к
    последнему куску, а не теряется.

    Склейка идёт по исходному тексту, до нормализации: иначе «2.5» превратилось
    бы в «2. 5» ровно там, где мы это чинили.

    Перевод строки — граница темы: интересы перечисляют построчно. Но не всякий:
    перенос перед строчной буквой — это жёсткий перенос посреди предложения, и
    резать по нему значит получить две грани из половинок одной мысли.
    """

    chunks: list[str] = []
    buffer = ""
    for raw in _CHUNK.findall(_WRAPPED.sub(" ", text)):
        buffer += raw
        if len(_letters(buffer)) >= min_chars:
            chunks.append(_clean(buffer))
            buffer = ""
    tail = _clean(buffer)
    if tail:
        if chunks:
            chunks[-1] = f"{chunks[-1]} {tail}"
        elif len(_letters(tail)) >= min_chars:
            chunks.append(tail)
    return chunks


@dataclass(frozen=True, slots=True)
class ExplicitInterest:
    """Строка явного интереса, как её записал компилятор профиля."""

    polarity: str
    text: str
    weight: float

    @property
    def is_negative(self) -> bool:
        return self.polarity == "negative"


def parse_interests(block: str) -> list[ExplicitInterest]:
    """Разобрать раздел явных интересов обратно в строки.

    Публичный разбор, а не приватная регулярка: этим пользуется и построение
    граней, и проверка профиля, и лезть в чужие внутренности ради второго —
    надёжный способ разъехаться при первой же правке формата.
    """

    found: list[ExplicitInterest] = []
    for match in _INTEREST_LINE.finditer(block):
        try:
            weight = float(match.group("weight"))
        except ValueError:
            weight = 0.0
        found.append(
            ExplicitInterest(
                polarity=match.group("polarity"),
                text=_clean(match.group("text")),
                weight=weight,
            )
        )
    return found


def interest_variants(term: str) -> list[str]:
    """Написания одного интереса: «liquefaction; разжижение грунтов».

    Явный интерес работает двумя способами сразу. Как грань он уходит в
    векторный поиск — там язык не важен, модель многоязычная. Но он ещё и
    сверяется буквально с текстом материала, а корпус собирается с arXiv,
    OpenAlex и GitHub и почти весь английский: русский термин не совпадёт
    никогда и молча превратится в постоянную нулевую добавку.

    Точка с запятой разделяет варианты: буквальная сверка проверяет каждый,
    вектор грани берёт строку целиком. Так человеку не приходится выбирать
    между «понятно мне» и «совпадёт с текстом».
    """

    return [variant for variant in (part.strip() for part in term.split(";")) if variant]


def build_facets(
    compiled_text: str,
    *,
    limit: int = 8,
    min_chars: int = 16,
) -> list[ProfileFacet]:
    """Грани профиля без грани «весь профиль» — она добавляется отдельно.

    Источников два: предложения текстового описания и явные интересы. Явные
    интересы уже разделены человеком по темам, и склеивать их обратно в один
    вектор было бы ровно той ошибкой, от которой мы уходим.

    Отрицательные интересы гранями не становятся никогда: грань — это поисковый
    запрос, и запрос «распознавание трещин в асфальте» принёс бы именно то, что
    человек просил не показывать.

    ``limit=0`` выключает грани целиком: остаётся прежнее поведение с одним
    вектором на весь профиль.
    """

    if limit <= 0:
        return []
    parts = sections(compiled_text)
    candidates: list[tuple[str, str]] = [
        (chunk, "description")
        for chunk in split_sentences(parts.get(DESCRIPTION_SECTION, ""), min_chars=min_chars)
    ]
    for interest in parse_interests(parts.get(INTERESTS_SECTION, "")):
        if interest.is_negative:
            continue
        if len(_letters(interest.text)) >= min_chars:
            candidates.append((interest.text, "interest"))

    facets: list[ProfileFacet] = []
    seen: set[str] = set()
    for text, source in candidates:
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        facets.append(ProfileFacet(index=len(facets) + 1, text=text, source=source))
        if len(facets) >= limit:
            break
    return facets


def with_full_profile(compiled_text: str, facets: list[ProfileFacet]) -> list[ProfileFacet]:
    """Полный профиль плюс грани — в том порядке, в каком по ним ищут.

    Полный профиль остаётся в списке всегда: он ловит материалы на стыке тем,
    которые ни в одну грань по отдельности не попадают.
    """

    return [ProfileFacet(index=FULL_PROFILE, text=compiled_text, source="profile"), *facets]


def _clean(value: str) -> str:
    # Обрамляющие маркеры списков и тире: строка «— тема» и «тема» — одна грань.
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip(_TRIM)


def _letters(value: str) -> str:
    """Только буквы и цифры: по ним меряется длина.

    Иначе «...» или «— » проходят проверку на длину и становятся гранью,
    по которой ищется белый шум.
    """

    return "".join(character for character in value if character.isalnum())


__all__ = [
    "DESCRIPTION_SECTION",
    "FULL_PROFILE",
    "INTERESTS_SECTION",
    "ExplicitInterest",
    "ProfileFacet",
    "build_facets",
    "interest_variants",
    "parse_interests",
    "sections",
    "split_sentences",
    "with_full_profile",
]
