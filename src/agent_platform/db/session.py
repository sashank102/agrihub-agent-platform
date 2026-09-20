"""Async SQLAlchemy engine, session, and transaction helpers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent_platform.core.settings import get_settings

AsyncSessionFactory = async_sessionmaker[AsyncSession]


def sqlalchemy_database_uri(database_uri: str) -> str:
    """Select SQLAlchemy's async Psycopg dialect for a validated URI."""
    if database_uri.startswith("postgresql+psycopg://"):
        return database_uri
    if database_uri.startswith("postgresql://"):
        return database_uri.replace(
            "postgresql://",
            "postgresql+psycopg://",
            1,
        )
    if database_uri.startswith("postgres://"):
        return database_uri.replace(
            "postgres://",
            "postgresql+psycopg://",
            1,
        )
    raise ValueError("DATABASE_URI must use postgres:// or postgresql://")


def create_platform_engine(
    database_uri: str | None = None,
    **engine_options: object,
) -> AsyncEngine:
    """Create an async engine using the configured PostgreSQL database."""
    uri = database_uri or get_settings().DATABASE_URI
    if uri is None:  # Settings always supplies a validated value.
        raise RuntimeError("DATABASE_URI is not configured")
    return create_async_engine(
        sqlalchemy_database_uri(uri),
        pool_pre_ping=True,
        **engine_options,
    )


def create_session_factory(engine: AsyncEngine) -> AsyncSessionFactory:
    """Create sessions whose state remains usable after transaction commits."""
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session_factory: AsyncSessionFactory | None = None,
    *,
    database_uri: str | None = None,
) -> AsyncIterator[AsyncSession]:
    """Yield one transactional session and own its commit or rollback.

    Repositories only flush. This context manager is the dependency-free
    transaction boundary for scripts and other non-HTTP callers.
    """
    owns_engine = session_factory is None
    engine = None
    if session_factory is None:
        engine = create_platform_engine(database_uri)
        session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            async with session.begin():
                yield session
    finally:
        if owns_engine and engine is not None:
            await engine.dispose()
