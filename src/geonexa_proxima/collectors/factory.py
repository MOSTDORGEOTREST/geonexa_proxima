"""Factories wiring collectors to application settings."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from geonexa_proxima.collectors.arxiv import ArxivCollector
from geonexa_proxima.collectors.base import TaxonomyInput
from geonexa_proxima.collectors.crossref import CrossrefCollector
from geonexa_proxima.collectors.github import GitHubCollector
from geonexa_proxima.collectors.oai import OAICollector
from geonexa_proxima.collectors.openalex import OpenAlexCollector
from geonexa_proxima.collectors.semantic_scholar import SemanticScholarEnricher
from geonexa_proxima.config import Settings
from geonexa_proxima.domain import SourceName
from geonexa_proxima.ports import Collector


def create_collectors(
    settings: Settings,
    *,
    query: str | None = None,
    taxonomy: TaxonomyInput = None,
) -> list[Collector]:
    """Все включённые источники профиля сбора.

    Запросы каждого источника берутся из его раздела в `config/harvest.yaml`
    — там они написаны в синтаксисе самого источника и на обоих языках.
    Общая таксономия остаётся запасным вариантом для источника без своего
    списка.
    """

    common = {"query": query, "taxonomy": taxonomy}
    path = settings.harvest_config_path
    config = load_sources(path)
    collectors: list[Collector] = []

    if _enabled(config, "arxiv"):
        collectors.append(ArxivCollector(**common, queries=source_queries(path, "arxiv")))
    if _enabled(config, "openalex"):
        collectors.append(
            OpenAlexCollector(
                **common, email=settings.openalex_email, queries=source_queries(path, "openalex")
            )
        )
    if _enabled(config, "crossref"):
        section = config.get("crossref") or {}
        collectors.append(
            CrossrefCollector(
                **common,
                email=settings.crossref_email,
                queries=source_queries(path, "crossref"),
                issns=[str(value) for value in section.get("issns") or [] if value],
            )
        )
    if _enabled(config, "github"):
        github_queries = source_queries(path, "github")
        collectors.append(
            GitHubCollector(
                # У GitHub свои запросы из профиля сбора: булевы пересечения
                # научных фраз, написанные для arXiv и OpenAlex, в поиске
                # репозиториев почти ничего не находят — там нужны короткие
                # тематические запросы. Без своего списка берём общие.
                **({"query": query, "taxonomy": github_queries} if github_queries else common),
                token=settings.github_token.get_secret_value() if settings.github_token else None,
            )
        )
    # OAI-PMH: КиберЛенинка и любой журнал на OJS. Один класс, разные адреса.
    keep = _gate_prefilter(path)
    for key, source in (("cyberleninka", SourceName.CYBERLENINKA), ("oai", SourceName.OAI)):
        section = config.get(key) or {}
        if not section.get("enabled", False):
            continue
        for endpoint in _endpoints(section):
            collectors.append(
                OAICollector(
                    endpoint["base_url"],
                    source=source,
                    sets=endpoint.get("sets") or [],
                    max_pages=int(endpoint.get("max_pages") or section.get("max_pages") or 20),
                    email=settings.crossref_email or settings.openalex_email,
                    keep=keep,
                )
            )
    return collectors


def _gate_prefilter(config_path: Path) -> Callable[[Any], bool] | None:
    """Гейт профиля сбора как предикат для источников без поиска по словам.

    Тот же матчер, что и в конвейере, только решение сводится к «не
    отклонено»: пограничные материалы остаются — их досмотрит семантика.
    """

    path = Path(config_path)
    if not path.is_file():
        return None
    from geonexa_proxima.harvest import Decision, HarvestMatcher, load_harvest_profile

    matcher = HarvestMatcher(load_harvest_profile(path))

    def keep(item: Any) -> bool:
        result = matcher.match(item.title, item.abstract, item.keywords, venue=item.venue)
        return result.decision is not Decision.REJECTED

    return keep


def load_sources(config_path: Path) -> dict[str, Any]:
    """Раздел `sources` профиля сбора; пусто, если файла нет или он не читается."""

    path = Path(config_path)
    if not path.is_file():
        return {}
    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return dict(raw.get("sources") or {})


def _enabled(config: dict[str, Any], key: str) -> bool:
    """Источник без раздела в конфиге считается включённым: так было всегда."""

    section = config.get(key)
    if section is None:
        return True
    return bool(section.get("enabled", True))


def _endpoints(section: dict[str, Any]) -> list[dict[str, Any]]:
    """Адреса OAI из раздела: либо один `base_url`, либо список `endpoints`."""

    found: list[dict[str, Any]] = []
    if section.get("base_url"):
        found.append(section)
    for entry in section.get("endpoints") or []:
        if isinstance(entry, dict) and entry.get("base_url") and entry.get("enabled", True):
            found.append(entry)
    return found


def create_semantic_scholar_enricher(settings: Settings) -> SemanticScholarEnricher:
    return SemanticScholarEnricher(
        api_key=(
            settings.semantic_scholar_api_key.get_secret_value()
            if settings.semantic_scholar_api_key
            else None
        )
    )


def source_queries(config_path: Path, source: str) -> list[str]:
    """Включённые запросы источника из `config/harvest.yaml` в порядке приоритета.

    Раздел `sources.<источник>.queries` профиля сбора долго лежал без дела:
    ни один коллектор его не читал. Здесь он становится списком строк,
    который коллектор принимает как таксономию.
    """

    section = load_sources(config_path).get(source) or {}
    if not section.get("enabled", True):
        return []
    entries = [
        entry
        for entry in section.get("queries") or []
        if isinstance(entry, dict) and entry.get("enabled", True) and entry.get("query")
    ]
    entries.sort(key=lambda entry: -int(entry.get("priority", 0) or 0))
    return [str(entry["query"]) for entry in entries]
