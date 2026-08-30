"""Детерминированный keyword-gate первой стадии воронки.

Решает, тянуть материал дальше или нет, до эмбеддингов и LLM. Работает по
профилю выборки: группы терминов, булево выражение допуска и взвешенная
оценка совпадений. Никакой сети и никаких моделей, только текст материала.
"""

from __future__ import annotations

import ast
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

# Разные дефисы и тире приводим к обычному минусу до нормализации.
_DASHES = str.maketrans(dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2212", "-"))
_SEPARATORS = re.compile(r"[-_/]")
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

DEFAULT_FIELDS: tuple[str, ...] = ("title", "abstract", "keywords")


class GroupMode(StrEnum):
    ANY_OF = "any_of"
    ALL_OF = "all_of"
    NONE_OF = "none_of"


class MatchType(StrEnum):
    PHRASE = "phrase"
    TOKEN = "token"
    PREFIX = "prefix"
    REGEX = "regex"


class Decision(StrEnum):
    ACCEPTED = "accepted"
    BORDERLINE = "borderline"
    REJECTED = "rejected"


def normalize(text: str | None) -> str:
    """Привести текст к форме, по которой сравниваются термины."""

    lowered = unicodedata.normalize("NFKC", text or "").lower().translate(_DASHES)
    without_separators = _SEPARATORS.sub(" ", lowered)
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", without_separators)).strip()


@dataclass(frozen=True, slots=True)
class Term:
    term: str
    match_type: MatchType = MatchType.PHRASE
    weight: float = 1.0
    lang: str | None = None
    pattern: re.Pattern[str] = field(compare=False, repr=False, default=None)  # type: ignore[assignment]

    @classmethod
    def build(cls, raw: Mapping[str, Any]) -> Term:
        term = str(raw["term"])
        match_type = MatchType(str(raw.get("match", MatchType.PHRASE)))
        return cls(
            term=term,
            match_type=match_type,
            weight=float(raw.get("weight", 1.0)),
            lang=raw.get("lang"),
            pattern=_compile(term, match_type),
        )


def _compile(term: str, match_type: MatchType) -> re.Pattern[str]:
    if match_type is MatchType.REGEX:
        return re.compile(term, re.IGNORECASE | re.UNICODE)
    normalized = normalize(term)
    if not normalized:
        raise ValueError(f"Term normalizes to an empty string: {term!r}")
    escaped = re.escape(normalized)
    if match_type is MatchType.PREFIX:
        return re.compile(rf"(?<!\w){escaped}\w*", re.UNICODE)
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.UNICODE)


@dataclass(frozen=True, slots=True)
class TermGroup:
    key: str
    mode: GroupMode
    terms: tuple[Term, ...]
    name: str = ""
    min_matches: int = 1
    fields: tuple[str, ...] = DEFAULT_FIELDS
    weight: float = 0.0
    is_hard: bool = False
    penalty: float = 0.0
    affects_satisfy: bool = True
    enabled: bool = True

    @classmethod
    def build(cls, raw: Mapping[str, Any]) -> TermGroup:
        return cls(
            key=str(raw["id"]),
            name=str(raw.get("name", "")),
            mode=GroupMode(str(raw["mode"])),
            min_matches=int(raw.get("min_matches", 1)),
            fields=tuple(raw.get("fields", DEFAULT_FIELDS)),
            weight=float(raw.get("weight", 0.0)),
            is_hard=bool(raw.get("hard", False)),
            penalty=float(raw.get("penalty", 0.0)),
            affects_satisfy=bool(raw.get("affects_satisfy", True)),
            enabled=bool(raw.get("enabled", True)),
            terms=tuple(Term.build(item) for item in (raw.get("terms") or [])),
        )

    def evaluate(self, fields: Mapping[str, str]) -> tuple[bool, tuple[Term, ...]]:
        """Вернуть (выполнена ли группа, сработавшие термины)."""

        haystack = " ".join(fields.get(name, "") for name in self.fields)
        hits = tuple(term for term in self.terms if term.pattern.search(haystack))
        if self.mode is GroupMode.ANY_OF:
            return len(hits) >= max(1, self.min_matches), hits
        if self.mode is GroupMode.ALL_OF:
            if not self.enabled or not self.terms:
                return True, hits
            return len(hits) == len(self.terms), hits
        return not hits, hits


@dataclass(frozen=True, slots=True)
class MatchResult:
    decision: Decision
    keyword_score: float
    satisfied: bool
    matched_terms: dict[str, list[str]]
    blocked_by: str | None = None
    reason: str = ""

    @property
    def needs_semantic_gate(self) -> bool:
        return self.decision is Decision.BORDERLINE


@dataclass(frozen=True, slots=True)
class HarvestProfile:
    key: str
    name: str
    satisfy_expr: str
    groups: tuple[TermGroup, ...]
    keyword_score_threshold: float = 0.35
    borderline_semantic_threshold: float = 0.52
    description: str = ""
    languages: tuple[str, ...] = ("en", "ru")
    item_kinds: tuple[str, ...] = ("paper", "method", "software", "dataset")

    def group(self, key: str) -> TermGroup:
        for candidate in self.groups:
            if candidate.key == key:
                return candidate
        raise KeyError(key)


def load_harvest_profile(path: str | Path) -> HarvestProfile:
    """Загрузить harvest-профиль из YAML-сида (в runtime источник истины — БД)."""

    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Harvest profile does not exist: {source}")
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    profile = raw.get("profile") or {}
    groups = tuple(TermGroup.build(item) for item in raw.get("groups") or [])
    if not groups:
        raise ValueError("Harvest profile defines no term groups")
    satisfy_expr = str(profile.get("satisfy", "")).strip()
    if not satisfy_expr:
        raise ValueError("Harvest profile has no satisfy expression")
    parsed = HarvestProfile(
        key=str(profile.get("key", source.stem)),
        name=str(profile.get("name", source.stem)),
        description=str(profile.get("description", "")),
        satisfy_expr=satisfy_expr,
        groups=groups,
        keyword_score_threshold=float(profile.get("keyword_score_threshold", 0.35)),
        borderline_semantic_threshold=float(profile.get("borderline_semantic_threshold", 0.52)),
        languages=tuple(
            profile.get("content_languages") or profile.get("languages") or ("en", "ru")
        ),
        item_kinds=tuple(profile.get("item_kinds") or ("paper", "method", "software", "dataset")),
    )
    _validate_satisfy(parsed)
    return parsed


def _validate_satisfy(profile: HarvestProfile) -> None:
    known = {group.key for group in profile.groups}
    tree = ast.parse(profile.satisfy_expr, mode="eval")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in known:
            raise ValueError(f"satisfy references unknown group: {node.id}")
        if not isinstance(
            node,
            (ast.Expression, ast.BoolOp, ast.UnaryOp, ast.Not, ast.And, ast.Or, ast.Name, ast.Load),
        ):
            raise ValueError(
                f"satisfy supports only and/or/not over group ids: {type(node).__name__}"
            )


class _SatisfyEvaluator(ast.NodeVisitor):
    def __init__(self, values: Mapping[str, bool]) -> None:
        self._values = values

    def visit_Expression(self, node: ast.Expression) -> bool:
        return bool(self.visit(node.body))

    def visit_BoolOp(self, node: ast.BoolOp) -> bool:
        results = [bool(self.visit(value)) for value in node.values]
        return all(results) if isinstance(node.op, ast.And) else any(results)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> bool:
        if isinstance(node.op, ast.Not):
            return not self.visit(node.operand)
        raise ValueError("satisfy supports only the 'not' unary operator")

    def visit_Name(self, node: ast.Name) -> bool:
        return bool(self._values[node.id])

    def generic_visit(self, node: ast.AST) -> bool:
        raise ValueError(f"Unsupported node in satisfy: {type(node).__name__}")


class HarvestMatcher:
    """Применяет harvest-профиль к материалу и объясняет решение."""

    def __init__(self, profile: HarvestProfile) -> None:
        self.profile = profile
        self._satisfy = ast.parse(profile.satisfy_expr, mode="eval")
        self._scoring_weight = sum(
            group.weight for group in profile.groups if group.mode is not GroupMode.NONE_OF
        )

    def match(
        self,
        title: str,
        abstract: str | None = None,
        keywords: Iterable[str] | None = None,
        *,
        venue: str | None = None,
        threshold: float | None = None,
    ) -> MatchResult:
        fields = {
            "title": normalize(title),
            "abstract": normalize(abstract),
            "keywords": normalize(" ".join(keywords or ())),
            "venue": normalize(venue),
        }
        values: dict[str, bool] = {}
        matched: dict[str, list[str]] = {}
        score = 0.0
        penalty = 0.0
        blocked_by: str | None = None

        for group in self.profile.groups:
            satisfied, hits = group.evaluate(fields)
            if hits:
                matched[group.key] = [term.term for term in hits]
            if group.mode is GroupMode.NONE_OF:
                # Для стоп-листов в satisfy участвует факт срабатывания.
                values[group.key] = bool(hits)
                if hits and group.is_hard and blocked_by is None:
                    blocked_by = group.key
                elif hits and not group.is_hard:
                    penalty += group.penalty
                continue
            values[group.key] = satisfied if group.affects_satisfy else True
            if hits and group.weight:
                score += group.weight * _group_score(hits)

        satisfied = bool(_SatisfyEvaluator(values).visit(self._satisfy))
        normalized_score = score / self._scoring_weight if self._scoring_weight else 0.0
        keyword_score = round(max(0.0, min(1.0, normalized_score) - penalty), 4)
        cutoff = self.profile.keyword_score_threshold if threshold is None else threshold

        if blocked_by is not None:
            return MatchResult(
                Decision.REJECTED,
                keyword_score,
                False,
                matched,
                blocked_by,
                f"жёсткий стоп-лист {blocked_by}",
            )
        if not satisfied:
            positive = {
                group.key for group in self.profile.groups if group.mode is not GroupMode.NONE_OF
            }
            failed = [key for key, value in values.items() if not value and key in positive]
            return MatchResult(
                Decision.REJECTED,
                keyword_score,
                False,
                matched,
                None,
                "не выполнено satisfy: " + ", ".join(failed),
            )
        if keyword_score >= cutoff:
            return MatchResult(Decision.ACCEPTED, keyword_score, True, matched, None, "")
        return MatchResult(
            Decision.BORDERLINE,
            keyword_score,
            True,
            matched,
            None,
            f"keyword_score {keyword_score} ниже порога {cutoff}",
        )

    def explain(self, result: MatchResult) -> str:
        parts = [f"{result.decision.value} ({result.keyword_score})"]
        parts += [f"{key}: {', '.join(terms[:4])}" for key, terms in result.matched_terms.items()]
        if result.reason:
            parts.append(result.reason)
        return " · ".join(parts)


def _group_score(hits: Sequence[Term]) -> float:
    """Сильнейший термин плюс покрытие: одно точное попадание не равно трём."""

    best = max(term.weight for term in hits)
    coverage = min(1.0, len(hits) / 3)
    return 0.6 * best + 0.4 * coverage
