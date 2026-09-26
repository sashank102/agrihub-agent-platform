"""Prepare PostgreSQL, then return so one API worker can start.

Order: wait for PostgreSQL, take the migration advisory lock, upgrade the
platform schema, initialize LangGraph checkpoint and store tables, release the
migration lock. The API process lock is acquired later by the server lifespan.
Failures raise immediately and the process exits non-zero.
"""

import asyncio
import time

import psycopg

from agent_platform.api.routes.health import _project_root
from agent_platform.core.settings import get_settings
from agent_platform.logging_config import configure_logging
from agent_platform.persistence import open_postgres_persistence
from agent_platform.process_lock import (
    MIGRATION_ADVISORY_LOCK_CLASSID,
    MIGRATION_ADVISORY_LOCK_OBJID,
)
from alembic import command
from alembic.config import Config


def main() -> None:
    """Run startup preparation. Do not print the database URL or secrets."""
    configure_logging()
    settings = get_settings()
    if settings.DATABASE_URI is None:
        raise RuntimeError("DATABASE_URI is required")
    if settings.ENVIRONMENT == "production" and settings.AUTH_MODE != "api_key":
        raise RuntimeError("production requires AUTH_MODE=api_key")
    wait_for_postgres(settings.DATABASE_URI)
    with psycopg.connect(settings.DATABASE_URI, autocommit=True) as connection:
        connection.execute(
            "SELECT pg_advisory_lock(%s, %s)",
            (MIGRATION_ADVISORY_LOCK_CLASSID, MIGRATION_ADVISORY_LOCK_OBJID),
        )
        try:
            upgrade_platform_schema()
            asyncio.run(_initialize_langgraph(settings.DATABASE_URI))
        finally:
            connection.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (MIGRATION_ADVISORY_LOCK_CLASSID, MIGRATION_ADVISORY_LOCK_OBJID),
            )


def wait_for_postgres(database_uri: str, attempts: int = 30) -> None:
    """Retry a connectivity probe without including the URL in the error."""
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            with psycopg.connect(database_uri, connect_timeout=3) as connection:
                connection.execute("SELECT 1")
            return
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    raise RuntimeError("PostgreSQL is not ready") from last_error


def upgrade_platform_schema() -> None:
    """Apply Alembic migrations through head."""
    config = Config(str(_project_root() / "alembic.ini"))
    command.upgrade(config, "head")


async def _initialize_langgraph(database_uri: str) -> None:
    async with open_postgres_persistence(database_uri):
        return


if __name__ == "__main__":
    main()
