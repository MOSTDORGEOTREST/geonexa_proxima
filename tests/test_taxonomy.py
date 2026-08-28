from pathlib import Path

import pytest
import yaml

from geonexa_proxima.services.taxonomy import load_taxonomy

TAXONOMY_PATH = Path(__file__).parents[1] / "config" / "taxonomy.yaml"


@pytest.fixture(scope="module")
def taxonomy() -> dict[str, object]:
    with TAXONOMY_PATH.open(encoding="utf-8") as stream:
        loaded = yaml.safe_load(stream)

    assert isinstance(loaded, dict)
    return loaded


def test_taxonomy_contains_complete_interest_profile(taxonomy: dict[str, object]) -> None:
    profile = taxonomy["profile"]
    assert isinstance(profile, dict)
    assert profile["mission"]
    assert {"ru", "en"} <= set(profile["content_languages"])
    assert profile["positive_signals"]
    assert profile["negative_signals"]

    domains = taxonomy["domains"]
    assert isinstance(domains, list)
    domain_ids = {domain["id"] for domain in domains}

    assert {
        "geotechnical_engineering",
        "engineering_geology",
        "geotechnical_monitoring",
        "geophysics",
        "remote_sensing",
        "ai_for_geoscience",
        "physics_informed_ai",
        "uncertainty_and_risk",
        "digital_twins",
    } <= domain_ids


def test_taxonomy_ids_are_unique_and_domains_are_searchable(
    taxonomy: dict[str, object],
) -> None:
    domains = taxonomy["domains"]
    priorities = taxonomy["cross_cutting_priorities"]
    assert isinstance(domains, list)
    assert isinstance(priorities, list)

    identifiers = [entry["id"] for entry in [*domains, *priorities]]

    assert len(identifiers) == len(set(identifiers))
    for domain in domains:
        assert 0 < domain["weight"] <= 1
        assert domain["terms"]
        assert domain["subtopics"]


def test_taxonomy_ranking_weights_form_normalized_distribution(
    taxonomy: dict[str, object],
) -> None:
    ranking = taxonomy["ranking"]
    assert isinstance(ranking, dict)
    weights = ranking["weights"]
    assert isinstance(weights, dict)

    assert sum(weights.values()) == pytest.approx(1.0)
    assert all(0 < weight <= 1 for weight in weights.values())


def test_taxonomy_loader_builds_search_profile() -> None:
    loaded = load_taxonomy(TAXONOMY_PATH)

    assert "geotechnical engineering" in loaded.positive_terms
    assert "отсутствие описания данных или метрик" in loaded.excluded_terms
    assert len(loaded.queries(max_terms=5)) >= 10
    assert all(" AND " in query for query in loaded.queries())
    assert "geotechnical engineering" in loaded.profile_text
