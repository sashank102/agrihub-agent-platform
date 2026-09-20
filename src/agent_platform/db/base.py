"""Shared SQLAlchemy metadata for platform-owned tables."""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

PLATFORM_SCHEMA = "platform"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base restricted to the platform PostgreSQL schema."""

    metadata = MetaData(
        schema=PLATFORM_SCHEMA,
        naming_convention=NAMING_CONVENTION,
    )
