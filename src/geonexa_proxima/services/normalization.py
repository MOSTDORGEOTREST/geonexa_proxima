"""Canonicalization helpers used before persistence and deduplication."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from geonexa_proxima.domain import CollectedItem

_SPACE_RE = re.compile(r"\s+")
_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_ARXIV_PREFIX_RE = re.compile(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", re.IGNORECASE)
_TRACKING_KEYS = {"fbclid", "gclid", "ref", "source"}


def normalize_text(value: str) -> str:
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", value)).strip()


def normalize_title(value: str) -> str:
    return normalize_text(value).rstrip(" .")


def title_key(value: str) -> str:
    normalized = normalize_title(value).casefold()
    return "".join(character for character in normalized if character.isalnum() or character == " ")


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = _DOI_PREFIX_RE.sub("", normalize_text(value)).strip().lower()
    return normalized or None


def normalize_arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    normalized = _ARXIV_PREFIX_RE.sub("", normalize_text(value)).removesuffix(".pdf")
    normalized = re.sub(r"v\d+$", "", normalized, flags=re.IGNORECASE)
    return normalized or None


def canonicalize_url(value: object | None) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/") or "/",
            urlencode(query),
            "",
        )
    )


def normalize_item(item: CollectedItem) -> CollectedItem:
    """Return a normalized copy while retaining the provider's raw payload."""

    values = item.model_dump()
    values.update(
        {
            "external_id": normalize_text(item.external_id),
            "title": normalize_title(item.title),
            "abstract": normalize_text(item.abstract) if item.abstract else None,
            "keywords": list(dict.fromkeys(normalize_text(word) for word in item.keywords if word)),
            "doi": normalize_doi(item.doi),
            "arxiv_id": normalize_arxiv_id(item.arxiv_id),
            "venue": normalize_text(item.venue) if item.venue else None,
            "url": canonicalize_url(item.url),
            "code_url": canonicalize_url(item.code_url),
            "dataset_url": canonicalize_url(item.dataset_url),
        }
    )
    return CollectedItem.model_validate(values)


def identity_keys(item: CollectedItem) -> tuple[str, ...]:
    keys = [f"source:{item.source}:{item.external_id}", f"title:{title_key(item.title)}"]
    if item.doi:
        keys.append(f"doi:{normalize_doi(item.doi)}")
    if item.arxiv_id:
        keys.append(f"arxiv:{normalize_arxiv_id(item.arxiv_id)}")
    return tuple(keys)
