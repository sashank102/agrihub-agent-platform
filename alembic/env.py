"""Alembic environment for platform-owned PostgreSQL objects."""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from agent_platform.core.settings import get_settings
from agent_platform.db import models  # noqa: F401
from agent_platform.db.base import PLATFORM_SCHEMA, Base
from agent_platform.db.session import sqlalchemy_database_uri
from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_name(
    name: str | None,
    type_: str,
    parent_names: dict[str, str | None],
) -> bool:
    """Limit autogeneration to the platform schema."""
    if type_ == "schema":
        return name == PLATFORM_SCHEMA
    if type_ == "table":
        return parent_names.get("schema_name") == PLATFORM_SCHEMA
    return True


def configure_url() -> str:
    """Load the same validated database URI used by the application."""
    uri = get_settings().DATABASE_URI
    if uri is None:
        raise RuntimeError("DATABASE_URI is not configured")
    return sqlalchemy_database_uri(uri)


def run_migrations_offline() -> None:
    """Run migrations without creating an Engine."""
    context.configure(
        url=configure_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=include_name,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure and run migrations on a synchronous proxy connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async Psycopg engine and execute migration operations."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = configure_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in online mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
