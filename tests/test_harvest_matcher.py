"""Проверки keyword-gate на реальных формулировках заголовков."""

from __future__ import annotations

from pathlib import Path

import pytest

from geonexa_proxima.harvest import (
    Decision,
    HarvestMatcher,
    HarvestProfile,
    load_harvest_profile,
    normalize,
)

PROFILE_PATH = Path("config/harvest.yaml")

RELEVANT = [
    (
        "Physics-informed neural networks for undrained shear strength prediction from CPT data",
        "A PINN fuses cone penetration test soundings with a critical state soil model.",
    ),
    (
        "Deep learning landslide susceptibility mapping using InSAR time series",
        "A convolutional network is trained on interferometric deformation series.",
    ),
    (
        "A graph neural network surrogate for discrete element method simulations",
        "We learn a simulator for granular material flow; code is available.",
    ),
    (
        "Bayesian calibration of a hypoplastic constitutive model for sand",
        "Uncertainty quantification on triaxial test data under cyclic loading.",
    ),
    (
        "Distributed fiber optic sensing for deformation monitoring of a tailings dam",
        "",
    ),
    (
        "Нейросетевая оценка разжижения грунтов по данным статического зондирования",
        "Машинное обучение применено к инженерно-геологическим изысканиям.",
    ),
    (
        "Neural operator learning for coupled hydro-mechanical modelling of porous media",
        "DeepONet applied to poromechanics and seepage in an earth dam.",
    ),
    (
        "Digital twin of a deep excavation with real-time model updating",
        "Inclinometer data drives the update loop.",
    ),
    # Мягкий гейт: геотехнический якорь без ML тоже проходит — источники и так
    # опрашиваются профильными запросами, а отсеивать по строгому правилу
    # значило терять большинство собранного (332 из 385 за месяц).
    ("Slope stability analysis using limit equilibrium methods", "No learning component."),
]

IRRELEVANT = [
    ("Deep learning for protein folding prediction", "AlphaFold-style model."),
    ("Soil microbiome diversity under different fertilizer regimes", "Crop yield study."),
    ("Transformer architectures for sentiment analysis of product reviews", ""),
    (
        "A bibliometric review of machine learning in geotechnical engineering",
        "Publication trends.",
    ),
]

#: Чистая геология: по решению владельца в базу течёт всё «гео», отсев делает
#: пайплайн дальше. Группа pure_geology осталась в файле выключенной.
PURE_GEOLOGY = [
    ("Biostratigraphy of the Jurassic sediments of the Volga basin", ""),
    ("Zircon U-Pb geochronology of granites in the Urals", "Petrogenesis."),
    ("Стратиграфия юрских отложений Западной Сибири", ""),
    ("Mineral exploration using hyperspectral imaging", "Ore deposit targeting."),
]

#: Широкий охват: строительство, инженерная геология без ML, геология с
#: инженерным якорем — всё это в корпусе нужно.
BROAD_ACCEPTED = [
    ("Расчёт осадки фундаментов высотных зданий на слабых грунтах", ""),
    ("Seismic response of a reinforced concrete bridge pier on liquefiable ground", ""),
    ("Влияние минералогического состава глин на набухание грунтов оснований", ""),
    ("Machine learning prediction of tunnel boring machine performance", ""),
    ("Groundwater flow modelling in a fractured aquifer", ""),
    ("Lunar regolith excavation with autonomous robots", "Planetary soil handling."),
]


@pytest.fixture(scope="module")
def profile() -> HarvestProfile:
    return load_harvest_profile(PROFILE_PATH)


@pytest.fixture(scope="module")
def matcher(profile: HarvestProfile) -> HarvestMatcher:
    return HarvestMatcher(profile)


def test_profile_loads_expected_groups(profile: HarvestProfile) -> None:
    keys = {group.key for group in profile.groups}
    assert {
        "geo_domain",
        "ai_method",
        "geo_sensing",
        "construction",
        "geo_broad",
        "pure_geology",
        "hard_exclude",
    } <= keys
    assert profile.satisfy_expr
    # Нулевой порог — осознанно: всё, что прошло satisfy, принимается сразу.
    assert 0 <= profile.keyword_score_threshold < 1


@pytest.mark.parametrize(("title", "abstract"), RELEVANT)
def test_relevant_items_pass(matcher: HarvestMatcher, title: str, abstract: str) -> None:
    result = matcher.match(title, abstract)
    assert result.decision is not Decision.REJECTED, matcher.explain(result)
    assert result.satisfied


@pytest.mark.parametrize(("title", "abstract"), IRRELEVANT)
def test_irrelevant_items_are_rejected(matcher: HarvestMatcher, title: str, abstract: str) -> None:
    result = matcher.match(title, abstract)
    assert result.decision is Decision.REJECTED, matcher.explain(result)


@pytest.mark.parametrize(("title", "abstract"), PURE_GEOLOGY)
def test_pure_geology_flows_into_the_corpus(
    matcher: HarvestMatcher, title: str, abstract: str
) -> None:
    result = matcher.match(title, abstract)
    assert result.decision is not Decision.REJECTED, matcher.explain(result)


def test_pure_geology_group_is_kept_but_disabled(profile: HarvestProfile) -> None:
    """Выключатель на будущее: включить и вернуть в satisfy — без правки кода."""

    assert profile.group("pure_geology").enabled is False


@pytest.mark.parametrize(
    "title",
    [
        "A new optimizer for large language models",
        "Спутниковые снимки в сельском хозяйстве",
        "Quantum error correction with surface codes",
    ],
)
def test_non_geo_titles_do_not_pass(matcher: HarvestMatcher, title: str) -> None:
    result = matcher.match(title, None)
    assert result.decision is Decision.REJECTED, matcher.explain(result)


@pytest.mark.parametrize(("title", "abstract"), BROAD_ACCEPTED)
def test_broad_engineering_scope_passes(matcher: HarvestMatcher, title: str, abstract: str) -> None:
    result = matcher.match(title, abstract)
    assert result.decision is not Decision.REJECTED, matcher.explain(result)


def test_hard_exclude_blocks_regardless_of_domain_hits(matcher: HarvestMatcher) -> None:
    result = matcher.match(
        "Soil fertility and machine learning for geotechnical engineering",
        "Combines soil mechanics vocabulary with agronomy.",
    )
    assert result.decision is Decision.REJECTED
    assert result.blocked_by == "hard_exclude"


def test_matched_terms_are_reported(matcher: HarvestMatcher) -> None:
    result = matcher.match("Machine learning for liquefaction triggering assessment")
    assert "geo_domain" in result.matched_terms
    assert "ai_method" in result.matched_terms


def test_score_grows_with_evidence(matcher: HarvestMatcher) -> None:
    weak = matcher.match("Machine learning for slope stability")
    strong = matcher.match(
        "Physics-informed graph neural network for slope stability and landslide runout",
        "Validated on field data from instrumented slopes; open source code and dataset.",
    )
    assert strong.keyword_score > weak.keyword_score


def test_normalize_handles_hyphens_and_case() -> None:
    assert normalize("Physics‑Informed  Neural-Networks!") == "physics informed neural networks"


def test_satisfy_rejects_unknown_group(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        "profile:\n"
        "  key: t\n"
        "  name: t\n"
        "  satisfy: 'geo_domain and nonexistent'\n"
        "groups:\n"
        "  - id: geo_domain\n"
        "    mode: any_of\n"
        "    terms: [{term: soil, match: token}]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown group"):
        load_harvest_profile(broken)


def test_satisfy_rejects_arbitrary_code(tmp_path: Path) -> None:
    broken = tmp_path / "unsafe.yaml"
    broken.write_text(
        "profile:\n"
        "  key: t\n"
        "  name: t\n"
        '  satisfy: \'__import__("os").system("id")\'\n'
        "groups:\n"
        "  - id: geo_domain\n"
        "    mode: any_of\n"
        "    terms: [{term: soil, match: token}]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_harvest_profile(broken)
