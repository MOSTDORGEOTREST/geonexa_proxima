"""Factories wiring collectors to application settings."""

from __future__ import annotations

from geonexa_proxima.collectors.arxiv import ArxivCollector
from geonexa_proxima.collectors.base import TaxonomyInput
from geonexa_proxima.collectors.crossref import CrossrefCollector
from geonexa_proxima.collectors.github import GitHubCollector
from geonexa_proxima.collectors.openalex import OpenAlexCollector
from geonexa_proxima.collectors.semantic_scholar import SemanticScholarEnricher
from geonexa_proxima.config import Settings
from geonexa_proxima.ports import Collector


def create_collectors(
    settings: Settings,
    *,
    query: str | None = None,
    taxonomy: TaxonomyInput = None,
) -> list[Collector]:
    common = {"query": query, "taxonomy": taxonomy}
    return [
        ArxivCollector(**common),
        OpenAlexCollector(**common, email=settings.openalex_email),
        CrossrefCollector(**common, email=settings.crossref_email),
        GitHubCollector(
            **common,
            token=settings.github_token.get_secret_value() if settings.github_token else None,
        ),
    ]


def create_semantic_scholar_enricher(settings: Settings) -> SemanticScholarEnricher:
    return SemanticScholarEnricher(
        api_key=(
            settings.semantic_scholar_api_key.get_secret_value()
            if settings.semantic_scholar_api_key
            else None
        )
    )
