"""Перевод профиля интересов на английский — второй язык поиска.

Корпус собирается с arXiv, OpenAlex, Crossref и GitHub и почти весь
английский. Человек описывает интересы по-русски, и до этого модуля его
описание работало только через многоязычный эмбеддинг, а явные темы без
английского написания не давали буквальной прибавки вообще. Теперь у профиля
две стороны: русская — то, что написал человек, и английская — перевод,
который делает LLM при каждом сохранении. Ищут обе.

Перевод не дословный. Модели объясняют, что это поисковый профиль по
геотехнике: она обязана использовать устоявшуюся англоязычную терминологию
(«разжижение грунтов» — ``soil liquefaction``, а не ``ground dilution``),
раскрывать русские аббревиатуры (ИГЭ, ОФМГ, СП) и дописывать к теме два-три
термина, которыми та же тема называется в статьях. Так одна русская фраза
превращается в несколько английских ключей.

Перевод хранится рядом с оригиналом вместе с отпечатком исходного текста:
по нему видно, что описание правили, а перевод отстал.
"""

from __future__ import annotations

import logging
import re
from hashlib import sha256
from typing import Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger("geonexa.translation")

CYRILLIC = re.compile("[А-Яа-яЁё]")
LATIN = re.compile("[A-Za-z]")


class Translator(Protocol):
    """Порт: перевод текста профиля и отдельной темы."""

    async def translate_description(self, text: str) -> str: ...

    async def translate_term(self, term: str) -> str: ...


class DescriptionTranslation(BaseModel):
    """Ответ модели на перевод описания: по строке на тему исходника."""

    english: str = Field(description="English version of the profile, same topic structure")
    notes: str | None = Field(default=None, description="Anything ambiguous, or null")


class TermTranslation(BaseModel):
    """Ответ модели на перевод одной темы: главный термин и синонимы."""

    english: str = Field(description="Standard English term as used in paper titles")
    synonyms: list[str] = Field(default_factory=list, description="Up to 3 alternative terms")


_SYSTEM_DESCRIPTION = (
    "You translate a researcher's interest profile from Russian into English. The profile "
    "is used as a SEARCH QUERY against English-language scientific literature in geotechnical "
    "engineering, engineering geology, soil mechanics, foundations, tunnelling, dams, roads, "
    "construction, geophysics, remote sensing and applied machine learning.\n"
    "Rules:\n"
    "1. Keep the structure: one topic per line, same order, same number of topics. Do not "
    "merge or drop topics; do not add topics the author did not mention.\n"
    "2. Use the established English terminology of the field, the way it appears in paper "
    "titles and abstracts (e.g. «разжижение грунтов» → soil liquefaction; «ОФМГ» → soil "
    "mechanics and foundation engineering; «ИГЭ» → engineering-geological element / soil "
    "unit; «СП», «СНиП» → Russian building codes (SP, SNiP); «свайный фундамент» → pile "
    "foundation; «статическое зондирование» → cone penetration test (CPT); «изыскания» → "
    "site investigation; «определяющие соотношения» → constitutive models; «МКЭ» → finite "
    "element method (FEM); «МДЭ» → discrete element method (DEM); «просадочные грунты» → "
    "collapsible soils; «набухающие грунты» → expansive soils; «ММГ», «многолетнемёрзлые "
    "грунты» → permafrost; «циклические нагрузки» → cyclic loading; «восстановление "
    "параметров» → parameter identification / inverse analysis).\n"
    "3. Expand Russian abbreviations and add, in parentheses after the topic, up to three "
    "alternative English terms or standard abbreviations by which the same topic is named in "
    "the literature. Keep each line self-contained: a reader must understand it without the "
    "other lines.\n"
    "4. Translate faithfully: do not broaden a narrow topic and do not narrow a broad one. If "
    "a topic is already in English, keep it and only add the alternatives.\n"
    "5. Plain text lines, no markdown, no numbering unless the source has it."
)

_SYSTEM_TERM = (
    "You translate one research topic from Russian into English for keyword matching against "
    "English scientific paper titles and abstracts in geotechnical engineering, engineering "
    "geology, construction, geophysics and applied machine learning. Return the single most "
    "standard English term (lowercase unless it is a proper abbreviation such as CPT, InSAR, "
    "PINN) and up to three alternative terms or abbreviations used in the literature for the "
    "same concept. Expand Russian abbreviations. If the input is already English, return it "
    "unchanged as the term and add alternatives."
)


def is_russian(text: str) -> bool:
    """Есть ли что переводить.

    Русское описание часто несёт английские термины в скобках («… (CPT, PINN)»),
    и латиницы в нём может оказаться немало. Порог поэтому мягкий: кириллица
    есть и составляет хотя бы треть от латиницы. Чисто английский текст с
    одним русским словом переводить не станем — переводчик вернул бы его же.
    """

    cyrillic = len(CYRILLIC.findall(text))
    latin = len(LATIN.findall(text))
    return cyrillic > 0 and cyrillic * 3 >= latin


def source_fingerprint(text: str | None) -> str | None:
    """Отпечаток исходника, к которому относится перевод."""

    cleaned = (text or "").strip()
    if not cleaned:
        return None
    return sha256(cleaned.encode("utf-8")).hexdigest()[:24]


class LLMTranslator:
    """Переводчик на light-модели: перевод — дешёвая операция, ризонинг не нужен."""

    def __init__(self, client: object) -> None:
        # `OpenAICompatibleJSONClient`; тип не импортируется, чтобы сервисы
        # профилей не тянули модуль LLM ради аннотации.
        self._client = client

    async def translate_description(self, text: str) -> str:
        source = text.strip()
        if not source:
            return ""
        if not is_russian(source):
            return source
        result = await self._client.generate(  # type: ignore[attr-defined]
            DescriptionTranslation,
            system=_SYSTEM_DESCRIPTION,
            user=f"Profile (Russian):\n{source}",
            grounding=False,
        )
        english = result.english.strip()
        if not english:
            raise ValueError("Перевод описания пришёл пустым")
        return english

    async def translate_term(self, term: str) -> str:
        source = term.strip()
        if not source or not is_russian(source):
            return source
        result = await self._client.generate(  # type: ignore[attr-defined]
            TermTranslation,
            system=_SYSTEM_TERM,
            user=f"Topic (Russian): {source}",
            grounding=False,
        )
        variants = [result.english.strip(), *(item.strip() for item in result.synonyms)]
        variants = [item for item in dict.fromkeys(variants) if item and ";" not in item]
        if not variants:
            raise ValueError("Перевод темы пришёл пустым")
        return "; ".join(variants[:4])


def bilingual_term(term: str, english: str) -> str:
    """Тема в формате «en; ru», который понимают грани и буквальная сверка.

    Русский вариант остаётся в строке: по нему человек узнаёт свою тему в
    списке, а буквальная сверка по русским статьям (КиберЛенинка) работает
    именно через него.
    """

    parts = [part.strip() for part in english.split(";")] + [term.strip()]
    return "; ".join(part for part in dict.fromkeys(parts) if part)
