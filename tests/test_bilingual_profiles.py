"""Двуязычный профиль: русский пишет человек, английский делает переводчик.

Проверяется склейка, а не качество перевода: что перевод считается только
при смене описания, что английская сторона становится гранями поиска, что
явная тема получает формат «en; ru», и что сбой переводчика не блокирует
сохранение профиля.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from geonexa_proxima.domain import InterestPolarity, UserProfile
from geonexa_proxima.services.facets import DESCRIPTION_EN_SECTION, build_facets, sections
from geonexa_proxima.services.profiles import UserProfileService, compile_profile
from geonexa_proxima.services.translation import (
    bilingual_term,
    is_russian,
    source_fingerprint,
)


class _Translator:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def translate_description(self, text: str) -> str:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("LLM недоступна")
        return "\n".join(f"[en] {line.strip()}" for line in text.splitlines() if line.strip())

    async def translate_term(self, term: str) -> str:
        self.calls.append(term)
        if self.fail:
            raise RuntimeError("LLM недоступна")
        return "soil liquefaction; cyclic liquefaction"


class _Repository:
    """Память вместо PostgreSQL: хранит ровно то, что ему передали."""

    def __init__(self) -> None:
        self.profiles: dict[UUID, dict[str, Any]] = {}
        self.interests: list[dict[str, Any]] = []

    def _profile(self, values: dict[str, Any]) -> UserProfile:
        return UserProfile(
            id=values["id"],
            user_id=values["user_id"],
            name=values["name"],
            normalized_name=values["name"].lower(),
            description=values.get("description"),
            description_en=values.get("description_en"),
            translation_source_hash=values.get("translation_source_hash"),
            compiled_text=values.get("compiled_text", ""),
            version=values.get("version", 1),
            is_active=True,
            digest_enabled=False,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    async def create_profile(self, user_id: UUID, name: str, **values: Any) -> UserProfile:
        row = {"id": uuid4(), "user_id": user_id, "name": name, "version": 1, **values}
        self.profiles[row["id"]] = row
        return self._profile(row)

    async def update_profile(self, user_id: UUID, profile_id: UUID, **values: Any) -> UserProfile:
        row = self.profiles[profile_id]
        for key, value in values.items():
            if value is None:
                continue
            if key in {"description_en", "translation_source_hash"} and value == "":
                row[key] = None
                continue
            row[key] = value
        row["version"] += 1
        return self._profile(row)

    async def get_profile(self, user_id: UUID, profile_id: UUID) -> UserProfile:
        return self._profile(self.profiles[profile_id])

    async def list_profiles(self, user_id: UUID) -> list[UserProfile]:
        return [self._profile(row) for row in self.profiles.values()]

    async def list_interests(self, *_: object) -> list[Any]:
        return [
            SimpleNamespace(
                id=row["id"],
                polarity=row["polarity"],
                target_text=row["query"],
                weight=row["weight"],
            )
            for row in self.interests
        ]

    async def list_profile_signals(self, *_: object) -> list[Any]:
        return []

    async def add_interest(self, user_id: UUID, profile_id: UUID, **values: Any) -> Any:
        row = {"id": uuid4(), **values}
        self.interests.append(row)
        return SimpleNamespace(**row)


def _service(translator: _Translator | None) -> tuple[UserProfileService, _Repository]:
    repository = _Repository()
    return UserProfileService(repository, "", translator=translator), repository  # type: ignore[arg-type]


async def test_description_is_translated_on_create_and_reused_until_it_changes() -> None:
    translator = _Translator()
    service, _ = _service(translator)
    user = uuid4()

    profile = await service.create_profile(
        user, "Основной", description="Разжижение грунтов.\nОсадки свайных фундаментов."
    )

    assert profile.description_en == ("[en] Разжижение грунтов.\n[en] Осадки свайных фундаментов.")
    assert profile.translation_source_hash == source_fingerprint(profile.description)
    assert DESCRIPTION_EN_SECTION in profile.compiled_text

    # Правка без смены описания перевод не вызывает: он дорогой и уже есть.
    await service.update_profile(user, profile.id, name="Переименован")
    assert len(translator.calls) == 1

    updated = await service.update_profile(user, profile.id, description="Карстовые провалы.")
    assert len(translator.calls) == 2
    assert updated.description_en == "[en] Карстовые провалы."


async def test_english_side_becomes_search_facets() -> None:
    compiled = compile_profile(
        "",
        description="Разжижение грунтов при циклических нагрузках.",
        description_en="Soil liquefaction under cyclic loading (cyclic mobility, CPT-based).",
    )

    parts = sections(compiled)
    assert parts[DESCRIPTION_EN_SECTION].startswith("Soil liquefaction")
    facets = build_facets(compiled, limit=16, min_chars=16)
    assert {facet.source for facet in facets} == {"description", "description_en"}


async def test_translator_failure_keeps_the_profile_monolingual() -> None:
    service, _ = _service(_Translator(fail=True))
    user = uuid4()

    profile = await service.create_profile(user, "Основной", description="Разжижение грунтов.")

    assert profile.description == "Разжижение грунтов."
    assert profile.description_en is None
    # Отпечатка нет — значит, при следующей сборке перевод попробуют снова.
    assert profile.translation_source_hash is None
    assert DESCRIPTION_EN_SECTION not in profile.compiled_text


async def test_english_description_is_its_own_english_side() -> None:
    translator = _Translator()
    service, _ = _service(translator)

    profile = await service.create_profile(
        uuid4(), "EN", description="Soil liquefaction under cyclic loading."
    )

    assert translator.calls == []
    assert profile.description_en == profile.description


async def test_interest_gets_english_variants_but_explicit_ones_are_kept() -> None:
    translator = _Translator()
    service, repository = _service(translator)
    user = uuid4()
    profile = await service.create_profile(user, "Основной", description="Грунты.")

    await service.add_interest(
        user, profile.id, query="разжижение грунтов", polarity=InterestPolarity.POSITIVE
    )
    await service.add_interest(
        user,
        profile.id,
        query="InSAR; радарная интерферометрия",
        polarity=InterestPolarity.POSITIVE,
    )

    stored = [row["query"] for row in repository.interests]
    assert stored[0] == "soil liquefaction; cyclic liquefaction; разжижение грунтов"
    # Тема с явным английским написанием переводчику не показывается.
    assert stored[1] == "InSAR; радарная интерферометрия"
    assert translator.calls.count("разжижение грунтов") == 1


def test_language_detection_and_bilingual_term() -> None:
    assert is_russian("Разжижение грунтов")
    assert is_russian("Разжижение грунтов по CPT")
    assert not is_russian("Soil liquefaction")
    assert not is_russian("")
    assert bilingual_term("грунт", "soil; ground; soil") == "soil; ground; грунт"
