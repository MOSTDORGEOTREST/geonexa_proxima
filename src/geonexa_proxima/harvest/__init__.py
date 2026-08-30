"""Глобальная область поиска: что платформа вообще ищет во внешних источниках."""

from geonexa_proxima.harvest.matcher import (
    Decision,
    GroupMode,
    HarvestMatcher,
    HarvestProfile,
    MatchResult,
    MatchType,
    TermGroup,
    load_harvest_profile,
    normalize,
)

__all__ = [
    "Decision",
    "GroupMode",
    "HarvestMatcher",
    "HarvestProfile",
    "MatchResult",
    "MatchType",
    "TermGroup",
    "load_harvest_profile",
    "normalize",
]
