import pytest
from pydantic import ValidationError

from geonexa_proxima.domain import CollectedItem, RankResult, SourceName


def make_rank_result(**overrides: object) -> RankResult:
    values: dict[str, object] = {
        "relevance": 8,
        "novelty": 6,
        "scientific_quality": 8,
        "practical_value": 7,
        "importance_for_geotechnics": 10,
        "importance_for_ai": 9,
        "reason": "Метод релевантен мониторингу и проверен на натурных данных.",
    }
    values.update(overrides)
    return RankResult(**values)


def test_rank_result_total_score_uses_documented_weights() -> None:
    result = make_rank_result()

    expected = 0.30 * 8 + 0.20 * 6 + 0.15 * 8 + 0.20 * 7 + 0.15 * 9

    assert result.total_score == round(expected, 3)
    assert result.total_score == 7.55


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("relevance", -0.01),
        ("novelty", 10.01),
        ("scientific_quality", 11),
        ("practical_value", -1),
        ("importance_for_geotechnics", 100),
        ("importance_for_ai", -10),
    ],
)
def test_rank_result_rejects_scores_outside_zero_to_ten(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make_rank_result(**{field: value})


def test_collected_item_builds_stable_embedding_text() -> None:
    item = CollectedItem(
        source=SourceName.ARXIV,
        external_id="2501.00001",
        title="Physics-informed monitoring",
        abstract="A field-validated method.",
        keywords=["geotechnics", "InSAR"],
    )

    assert item.embedding_text == (
        "Physics-informed monitoring\n\nA field-validated method.\n\nKeywords: geotechnics, InSAR"
    )
