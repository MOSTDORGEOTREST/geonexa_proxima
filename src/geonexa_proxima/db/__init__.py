"""PostgreSQL persistence API."""

from geonexa_proxima.db.base import Base
from geonexa_proxima.db.repository import (
    ItemNotFoundError,
    SQLAlchemyItemRepository,
    normalize_arxiv_id,
    normalize_doi,
    normalize_title,
)
from geonexa_proxima.db.session import (
    SessionFactory,
    create_engine,
    create_session_factory,
    init_database,
    session_scope,
)
from geonexa_proxima.db.user_repository import (
    FinalProfileDeletionError,
    InterestNotFoundError,
    ProfileNotFoundError,
    SQLAlchemyUserProfileRepository,
    UserNotFoundError,
    normalize_profile_name,
)

__all__ = [
    "Base",
    "FinalProfileDeletionError",
    "InterestNotFoundError",
    "ItemNotFoundError",
    "ProfileNotFoundError",
    "SQLAlchemyItemRepository",
    "SQLAlchemyUserProfileRepository",
    "SessionFactory",
    "UserNotFoundError",
    "create_engine",
    "create_session_factory",
    "init_database",
    "normalize_arxiv_id",
    "normalize_doi",
    "normalize_profile_name",
    "normalize_title",
    "session_scope",
]
