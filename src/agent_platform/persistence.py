"""PostgreSQL lifecycle for LangGraph checkpoints and store data."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore

from agent_platform.core.settings import get_settings


@dataclass(frozen=True, slots=True)
class PostgresPersistence:
    """Open LangGraph PostgreSQL persistence resources."""

    checkpointer: AsyncPostgresSaver
    store: AsyncPostgresStore


@asynccontextmanager
async def open_postgres_persistence(
    database_uri: str | None = None,
) -> AsyncIterator[PostgresPersistence]:
    """Set up, yield, and cleanly close PostgreSQL-backed LangGraph resources.

    LangGraph's setup methods use their own migration tracking and are safe to
    call every time this lifecycle starts, including against an empty database.
    """
    uri = database_uri or get_settings().DATABASE_URI
    if uri is None:  # Defensive: Settings validation always supplies a value.
        raise RuntimeError("DATABASE_URI is not configured")

    async with AsyncExitStack() as stack:
        checkpointer = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(uri)
        )
        store = await stack.enter_async_context(
            AsyncPostgresStore.from_conn_string(uri)
        )
        await checkpointer.setup()
        await store.setup()
        yield PostgresPersistence(checkpointer=checkpointer, store=store)
