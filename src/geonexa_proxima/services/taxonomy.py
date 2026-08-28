"""Taxonomy loading and collector/search query generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_strings(item))
        return result
    return []


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


@dataclass(frozen=True, slots=True)
class Taxonomy:
    """A provider-neutral representation of the project's research profile."""

    terms: tuple[str, ...]
    synonyms: tuple[str, ...] = ()
    excluded_terms: tuple[str, ...] = ()
    categories: dict[str, tuple[str, ...]] = field(default_factory=dict)
    discovery_queries: tuple[str, ...] = ()

    @property
    def positive_terms(self) -> tuple[str, ...]:
        return _unique([*self.terms, *self.synonyms])

    @property
    def profile_text(self) -> str:
        categories = "; ".join(
            f"{name}: {', '.join(values)}" for name, values in self.categories.items()
        )
        return ". ".join(part for part in [", ".join(self.positive_terms), categories] if part)

    def queries(self, *, max_terms: int = 12, include_exclusions: bool = True) -> list[str]:
        """Generate bounded Boolean queries suitable for scholarly APIs."""

        if max_terms < 1:
            raise ValueError("max_terms must be positive")
        if self.discovery_queries:
            return list(self.discovery_queries)
        terms = list(self.positive_terms)
        if not terms:
            return []
        suffix = ""
        if include_exclusions and self.excluded_terms:
            suffix = " AND NOT (" + " OR ".join(_quote(term) for term in self.excluded_terms) + ")"
        return [
            "("
            + " OR ".join(_quote(term) for term in terms[index : index + max_terms])
            + ")"
            + suffix
            for index in range(0, len(terms), max_terms)
        ]


def _quote(term: str) -> str:
    escaped = term.replace('"', '\\"')
    return f'"{escaped}"' if any(character.isspace() for character in escaped) else escaped


def load_taxonomy(path: str | Path) -> Taxonomy:
    """Load both compact and nested YAML taxonomies.

    Recognized semantic keys are ``terms``/``keywords``, ``synonyms``/``aliases``,
    and ``excluded``/``negative``. Unknown nested sections become categories.
    """

    taxonomy_path = Path(path)
    if not taxonomy_path.is_file():
        raise FileNotFoundError(f"Taxonomy file does not exist: {taxonomy_path}")
    raw = yaml.safe_load(taxonomy_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, (dict, list)):
        raise ValueError("Taxonomy root must be a mapping or list")

    terms: list[str] = []
    synonyms: list[str] = []
    excluded: list[str] = []
    discovery_queries: list[str] = []
    categories: dict[str, tuple[str, ...]] = {}
    semantic_keys = {
        "terms": terms,
        "keywords": terms,
        "topics": terms,
        "subtopics": terms,
        "positive_signals": terms,
        "synonyms": synonyms,
        "aliases": synonyms,
        "excluded": excluded,
        "exclude": excluded,
        "negative": excluded,
        "negative_terms": excluded,
        "negative_signals": excluded,
    }
    ignored_keys = {
        "version",
        "language",
        "name",
        "mission",
        "content_languages",
        "preferred_item_kinds",
        "id",
        "weight",
        "description",
        "ranking",
        "weights",
    }

    def visit(value: Any, name: str | None = None) -> list[str]:
        if isinstance(value, str):
            return _strings(value)
        if isinstance(value, list):
            found: list[str] = []
            for item in value:
                found.extend(visit(item, name))
            return found
        if not isinstance(value, dict):
            return []
        found = []
        for key, nested in value.items():
            normalized_key = str(key).lower()
            if normalized_key in ignored_keys:
                continue
            direct = _strings(nested)
            if normalized_key == "discovery_queries":
                discovery_queries.extend(direct)
                continue
            if normalized_key in semantic_keys and direct:
                destination = semantic_keys[normalized_key]
                destination.extend(direct)
                if destination is not excluded:
                    found.extend(direct)
                continue
            nested_terms = visit(nested, str(key))
            if nested_terms:
                categories[str(key)] = _unique(nested_terms)
                terms.extend(nested_terms)
                found.extend(nested_terms)
        return found

    root_terms = visit(raw)
    if isinstance(raw, list):
        terms.extend(root_terms)
    return Taxonomy(
        terms=_unique(terms),
        synonyms=_unique(synonyms),
        excluded_terms=_unique(excluded),
        categories=categories,
        discovery_queries=_unique(discovery_queries),
    )
