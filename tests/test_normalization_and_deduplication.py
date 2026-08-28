from geonexa_proxima.domain import CollectedItem, SourceName
from geonexa_proxima.services.deduplication import deduplicate_items, exact_duplicate
from geonexa_proxima.services.normalization import (
    canonicalize_url,
    normalize_arxiv_id,
    normalize_doi,
    normalize_text,
)


def test_identifier_and_text_normalization() -> None:
    assert normalize_text("  Гео\u00a0техника\t\nAI  ") == "Гео техника AI"
    assert normalize_doi("https://doi.org/10.1000/ABC.123") == "10.1000/abc.123"
    assert normalize_arxiv_id("https://arxiv.org/pdf/2501.00001v3.pdf") == "2501.00001"
    assert (
        canonicalize_url("HTTPS://EXAMPLE.COM/paper/?utm_source=digest&lang=ru#results")
        == "https://example.com/paper?lang=ru"
    )


def test_exact_deduplication_uses_normalized_doi_and_preserves_first_item() -> None:
    first = CollectedItem(
        source=SourceName.CROSSREF,
        external_id="crossref-1",
        title="Original provider title",
        doi="10.1000/EXAMPLE",
    )
    duplicate = CollectedItem(
        source=SourceName.OPENALEX,
        external_id="openalex-2",
        title="A different title for the same work",
        doi="https://doi.org/10.1000/example",
    )

    assert exact_duplicate(first, duplicate)
    assert deduplicate_items([first, duplicate]) == [first]
