"""External async collection providers."""

from geonexa_proxima.collectors.arxiv import ArxivCollector
from geonexa_proxima.collectors.base import TaxonomyInput
from geonexa_proxima.collectors.crossref import CrossrefCollector
from geonexa_proxima.collectors.factory import (
    create_collectors,
    create_semantic_scholar_enricher,
)
from geonexa_proxima.collectors.github import GitHubCollector
from geonexa_proxima.collectors.openalex import OpenAlexCollector
from geonexa_proxima.collectors.semantic_scholar import SemanticScholarEnricher

__all__ = [
    "ArxivCollector",
    "CrossrefCollector",
    "GitHubCollector",
    "OpenAlexCollector",
    "SemanticScholarEnricher",
    "TaxonomyInput",
    "create_collectors",
    "create_semantic_scholar_enricher",
]
