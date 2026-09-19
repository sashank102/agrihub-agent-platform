"""Reusable runtime for a PostgreSQL-backed research graph."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agent_platform.persistence import open_postgres_persistence
from open_deep_research.deep_researcher import build_graph


@asynccontextmanager
async def open_persistent_graph(
    database_uri: str | None = None,
) -> AsyncIterator[Any]:
    """Yield the research graph with PostgreSQL checkpoint and store support."""
    async with open_postgres_persistence(database_uri) as persistence:
        yield build_graph(
            checkpointer=persistence.checkpointer,
            store=persistence.store,
        )
