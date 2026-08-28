"""Application services and dependency composition."""

from geonexa_proxima.services.container import Container, load_container
from geonexa_proxima.services.digest import DigestBuilder, DigestFormatter
from geonexa_proxima.services.ingestion import IngestionService, IngestionStats
from geonexa_proxima.services.profiles import (
    ProfileCompiler,
    UserProfileService,
    compile_profile,
)

__all__ = [
    "Container",
    "DigestBuilder",
    "DigestFormatter",
    "IngestionService",
    "IngestionStats",
    "ProfileCompiler",
    "UserProfileService",
    "compile_profile",
    "load_container",
]
