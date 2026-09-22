"""PostgreSQL integration tests for the platform-owned schema."""

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from agent_platform.db.models import (
    Agent,
    ApiKey,
    Artifact,
    AuditLog,
    Run,
    RunEvent,
    Thread,
    User,
)
from agent_platform.db.repositories import (
    AgentRepository,
    ArtifactRepository,
    RunEventRepository,
    RunRepository,
    ThreadRepository,
    UserRepository,
)
from agent_platform.db.session import (
    create_platform_engine,
    create_session_factory,
    session_scope,
)
from agent_platform.persistence import open_postgres_persistence
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.postgres

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_TABLES = {
    "agents",
    "api_keys",
    "artifacts",
    "audit_log",
    "run_events",
    "runs",
    "threads",
    "users",
}
LANGGRAPH_TABLES = {
    "checkpoint_blobs",
    "checkpoint_migrations",
    "checkpoint_writes",
    "checkpoints",
    "store",
    "store_migrations",
}
PLATFORM_INDEXES = {
    "ix_agents_graph_id",
    "ix_agents_owner_user_id",
    "ix_api_keys_expires_at",
    "ix_api_keys_revoked_at",
    "ix_api_keys_user_id",
    "ix_artifacts_owner_created_at",
    "ix_artifacts_run_id",
    "ix_artifacts_thread_created_at",
    "ix_audit_log_actor_created_at",
    "ix_audit_log_created_at",
    "ix_audit_log_request_id",
    "ix_audit_log_resource",
    "ix_run_events_created_at",
    "ix_runs_active_thread",
    "ix_runs_agent_id",
    "ix_runs_finished_at",
    "ix_runs_status_created_at",
    "ix_runs_thread_created_at",
    "ix_threads_agent_id",
    "ix_threads_owner_last_activity",
    "ix_threads_status_last_activity",
    "ix_users_deleted_at",
    "pk_agents",
    "pk_api_keys",
    "pk_artifacts",
    "pk_audit_log",
    "pk_run_events",
    "pk_runs",
    "pk_threads",
    "pk_users",
    "uq_agents_global_graph_version",
    "uq_agents_owner_graph_version",
    "uq_api_keys_key_prefix",
    "uq_runs_thread_idempotency_key",
    "uq_users_email",
}
PLATFORM_CONSTRAINTS = {
    "ck_agents_agent_version_positive",
    "ck_run_events_run_event_sequence_positive",
    "ck_runs_run_status",
    "ck_threads_thread_status",
    "ck_users_user_status",
    "fk_agents_owner_user_id_users",
    "fk_api_keys_user_id_users",
    "fk_artifacts_owner_user_id_users",
    "fk_artifacts_run_id_runs",
    "fk_artifacts_thread_id_threads",
    "fk_audit_log_actor_user_id_users",
    "fk_run_events_run_id_runs",
    "fk_runs_agent_id_agents",
    "fk_runs_thread_id_threads",
    "fk_threads_agent_id_agents",
    "fk_threads_owner_user_id_users",
    "pk_agents",
    "pk_api_keys",
    "pk_artifacts",
    "pk_audit_log",
    "pk_run_events",
    "pk_runs",
    "pk_threads",
    "pk_users",
    "uq_api_keys_key_prefix",
    "uq_users_email",
}
LEGACY_CHECK_CONSTRAINTS = {
    "agent_version_positive",
    "run_event_sequence_positive",
    "run_status",
    "thread_status",
    "user_status",
}
NAMED_CHECK_CONSTRAINTS = {
    constraint
    for constraint in PLATFORM_CONSTRAINTS
    if constraint.startswith("ck_")
}


def _database_uri_with_name(uri: str, database_name: str) -> str:
    parsed = urlsplit(uri)
    return urlunsplit(parsed._replace(path=f"/{database_name}"))


@pytest.fixture
def postgres_database_uri() -> Iterator[str]:
    """Create a new empty database for each integration test."""
    admin_uri = os.getenv("TEST_DATABASE_URI")
    if not admin_uri:
        pytest.skip("set TEST_DATABASE_URI to run PostgreSQL integration tests")

    database_name = f"agent_platform_schema_test_{uuid.uuid4().hex}"
    with psycopg.connect(admin_uri, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )

    database_uri = _database_uri_with_name(admin_uri, database_name)
    try:
        yield database_uri
    finally:
        with psycopg.connect(admin_uri, autocommit=True) as connection:
            connection.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )


def _alembic_config() -> Config:
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _migrate(database_uri: str, revision: str) -> None:
    with patch.dict(os.environ, {"DATABASE_URI": database_uri}):
        if revision == "base":
            command.downgrade(_alembic_config(), revision)
        else:
            command.upgrade(_alembic_config(), revision)


@pytest.fixture
def migrated_database_uri(postgres_database_uri: str) -> str:
    """Apply the platform migration to a fresh database."""
    _migrate(postgres_database_uri, "head")
    return postgres_database_uri


def _table_names(database_uri: str, schema: str) -> set[str]:
    with psycopg.connect(database_uri) as connection:
        rows = connection.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = %s
            """,
            (schema,),
        ).fetchall()
    return {row[0] for row in rows}


def _platform_catalog(database_uri: str) -> tuple[set[str], set[str]]:
    with psycopg.connect(database_uri) as connection:
        indexes = {
            row[0]
            for row in connection.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'platform'
                """
            ).fetchall()
        }
        constraints = {
            row[0]
            for row in connection.execute(
                """
                SELECT c.conname
                FROM pg_constraint AS c
                JOIN pg_namespace AS n ON n.oid = c.connamespace
                WHERE n.nspname = 'platform'
                  AND c.contype IN ('p', 'u', 'f', 'c')
                """
            ).fetchall()
        }
    return indexes, constraints


def _public_table_oids(database_uri: str) -> dict[str, int]:
    with psycopg.connect(database_uri) as connection:
        rows = connection.execute(
            """
            SELECT c.relname, c.oid
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            """
        ).fetchall()
    return dict(rows)


def test_migration_upgrade_downgrade_upgrade_and_schema_boundaries(
    postgres_database_uri: str,
):
    assert _table_names(postgres_database_uri, "public") == set()
    assert _table_names(postgres_database_uri, "platform") == set()

    _migrate(postgres_database_uri, "head")

    assert _table_names(postgres_database_uri, "platform") == PLATFORM_TABLES
    indexes, constraints = _platform_catalog(postgres_database_uri)
    assert indexes == PLATFORM_INDEXES
    assert constraints == PLATFORM_CONSTRAINTS

    with patch.dict(os.environ, {"DATABASE_URI": postgres_database_uri}):
        command.downgrade(_alembic_config(), "20260920_0001")
    _, downgraded_constraints = _platform_catalog(postgres_database_uri)
    assert LEGACY_CHECK_CONSTRAINTS <= downgraded_constraints
    assert NAMED_CHECK_CONSTRAINTS.isdisjoint(downgraded_constraints)
    _migrate(postgres_database_uri, "head")
    assert _platform_catalog(postgres_database_uri) == (
        PLATFORM_INDEXES,
        PLATFORM_CONSTRAINTS,
    )

    async def initialize_langgraph() -> None:
        async with open_postgres_persistence(postgres_database_uri):
            pass

    asyncio.run(initialize_langgraph())
    public_before = _public_table_oids(postgres_database_uri)
    assert set(public_before) == LANGGRAPH_TABLES | {"alembic_version"}

    _migrate(postgres_database_uri, "base")

    assert _table_names(postgres_database_uri, "platform") == set()
    assert _public_table_oids(postgres_database_uri) == public_before

    _migrate(postgres_database_uri, "head")

    assert _table_names(postgres_database_uri, "platform") == PLATFORM_TABLES
    assert _public_table_oids(postgres_database_uri) == public_before
    assert _platform_catalog(postgres_database_uri) == (
        PLATFORM_INDEXES,
        PLATFORM_CONSTRAINTS,
    )


def test_repository_flows_ownership_idempotency_and_replay(
    migrated_database_uri: str,
):
    async def scenario() -> None:
        engine = create_platform_engine(migrated_database_uri)
        factory = create_session_factory(engine)
        try:
            async with session_scope(factory) as session:
                users = UserRepository(session)
                agents = AgentRepository(session)
                threads = ThreadRepository(session)
                runs = RunRepository(session)
                events = RunEventRepository(session)
                artifacts = ArtifactRepository(session)

                owner = await users.create(
                    display_name="Owner",
                    email="owner@example.test",
                )
                stranger = await users.create(display_name="Stranger")
                agent = await agents.create(
                    owner_user_id=owner.id,
                    graph_id="research",
                    name="Research",
                    configuration={"depth": 2},
                )
                thread = await threads.create(
                    owner_user_id=owner.id,
                    agent_id=agent.id,
                    title="Initial title",
                )
                run, created = await runs.create_idempotent(
                    owner_user_id=owner.id,
                    thread_id=thread.id,
                    agent_id=agent.id,
                    input={"question": "How?"},
                    idempotency_key="request-1",
                )
                same_run, created_again = await runs.create_idempotent(
                    owner_user_id=owner.id,
                    thread_id=thread.id,
                    agent_id=agent.id,
                    input={"question": "Ignored retry body"},
                    idempotency_key="request-1",
                )
                first_event = await events.append(
                    run_id=run.id,
                    owner_user_id=owner.id,
                    event_type="started",
                )
                second_event = await events.append(
                    run_id=run.id,
                    owner_user_id=owner.id,
                    event_type="progress",
                    payload={"percent": 50},
                )
                artifact = await artifacts.create(
                    owner_user_id=owner.id,
                    thread_id=thread.id,
                    run_id=run.id,
                    kind="report",
                    media_type="text/markdown",
                    text_content="# Result",
                )

                assert created is True
                assert created_again is False
                assert same_run.id == run.id
                assert (first_event.sequence, second_event.sequence) == (1, 2)
                assert await threads.get_for_owner(thread.id, stranger.id) is None
                assert await runs.get_for_owner(run.id, stranger.id) is None
                assert await artifacts.get_for_owner(artifact.id, stranger.id) is None
                assert await agents.get_for_owner(agent.id, stranger.id) is None

                await users.update_profile(owner, display_name="Updated Owner")
                assert (
                    await agents.update_for_owner(
                        agent.id,
                        owner.id,
                        active=False,
                    )
                ) is not None
                assert (
                    await threads.update_for_owner(
                        thread.id,
                        owner.id,
                        title="Updated title",
                        touch=True,
                    )
                ) is not None
                assert (
                    await threads.update_for_owner(
                        thread.id,
                        stranger.id,
                        title="Foreign update",
                    )
                ) is None
                assert (
                    await artifacts.update_for_owner(
                        artifact.id,
                        owner.id,
                        metadata={"retention": "short"},
                    )
                ) is not None
                assert (
                    await artifacts.update_for_owner(
                        artifact.id,
                        stranger.id,
                        text_content="foreign",
                    )
                ) is None
                assert (
                    await runs.set_status_for_owner(
                        run.id,
                        stranger.id,
                        "completed",
                    )
                ) is None
                assert (
                    await runs.request_cancellation_for_owner(
                        run.id,
                        stranger.id,
                    )
                ) is None
                assert (
                    await runs.set_status_internal(
                        run.id,
                        "running",
                    )
                ) is not None

                owner_id = owner.id
                stranger_id = stranger.id
                agent_id = agent.id
                thread_id = thread.id
                run_id = run.id
                artifact_id = artifact.id

            async with session_scope(factory) as session:
                users = UserRepository(session)
                threads = ThreadRepository(session)
                runs = RunRepository(session)
                events = RunEventRepository(session)
                artifacts = ArtifactRepository(session)

                assert (await users.get(owner_id)).display_name == "Updated Owner"
                assert (await threads.get_for_owner(thread_id, owner_id)).title == (
                    "Updated title"
                )
                assert len(await runs.list_for_thread(thread_id, owner_id)) == 1
                replay = await events.replay(
                    run_id=run_id,
                    owner_user_id=owner_id,
                )
                assert [event.sequence for event in replay] == [1, 2]
                assert [event.event_type for event in replay] == [
                    "started",
                    "progress",
                ]
                assert (
                    await artifacts.get_for_owner(artifact_id, owner_id)
                ).metadata_ == {"retention": "short"}
                assert await artifacts.get_for_owner(artifact_id, stranger_id) is None
                assert (await session.get(Agent, agent_id)).active is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_repository_ownership_and_cross_resource_consistency(
    migrated_database_uri: str,
):
    async def scenario() -> None:
        engine = create_platform_engine(migrated_database_uri)
        factory = create_session_factory(engine)
        try:
            async with session_scope(factory) as session:
                users = UserRepository(session)
                agents = AgentRepository(session)
                threads = ThreadRepository(session)
                runs = RunRepository(session)
                artifacts = ArtifactRepository(session)

                owner = await users.create(display_name="Owner")
                stranger = await users.create(display_name="Stranger")
                owned_agent = await agents.create(
                    owner_user_id=owner.id,
                    graph_id="owned",
                    name="Owned",
                )
                other_agent = await agents.create(
                    owner_user_id=owner.id,
                    graph_id="other",
                    name="Other",
                )
                global_agent = await agents.create(
                    graph_id="global",
                    name="Global",
                )
                owner_thread = await threads.create(
                    owner_user_id=owner.id,
                    agent_id=owned_agent.id,
                )
                stranger_thread = await threads.create(
                    owner_user_id=stranger.id,
                    agent_id=global_agent.id,
                )
                owner_run, _ = await runs.create_idempotent(
                    owner_user_id=owner.id,
                    thread_id=owner_thread.id,
                    agent_id=owned_agent.id,
                )
                stranger_run, _ = await runs.create_idempotent(
                    owner_user_id=stranger.id,
                    thread_id=stranger_thread.id,
                    agent_id=global_agent.id,
                )

                visible_global = await agents.get_for_owner(
                    global_agent.id,
                    owner.id,
                )
                assert visible_global is not None
                assert (
                    await agents.update_for_owner(
                        visible_global.id,
                        owner.id,
                        name="Owner must not mutate this",
                    )
                    is None
                )
                assert (
                    await agents.update_global(
                        global_agent.id,
                        name="System-updated global",
                    )
                ) is not None

                with pytest.raises(
                    LookupError,
                    match="thread is not owned by the user and agent",
                ):
                    await runs.create_idempotent(
                        owner_user_id=owner.id,
                        thread_id=owner_thread.id,
                        agent_id=other_agent.id,
                    )
                with pytest.raises(
                    LookupError,
                    match="thread is not owned",
                ):
                    await artifacts.create(
                        owner_user_id=stranger.id,
                        thread_id=owner_thread.id,
                        kind="invalid-owner",
                        media_type="text/plain",
                    )
                with pytest.raises(
                    LookupError,
                    match="run does not belong",
                ):
                    await artifacts.create(
                        owner_user_id=owner.id,
                        thread_id=owner_thread.id,
                        run_id=stranger_run.id,
                        kind="invalid-run",
                        media_type="text/plain",
                    )

                mismatched = Artifact(
                    owner_user_id=owner.id,
                    thread_id=stranger_thread.id,
                    run_id=None,
                    kind="legacy-mismatch",
                    media_type="text/plain",
                )
                session.add(mismatched)
                await session.flush()
                mismatched_id = mismatched.id
                owner_id = owner.id
                stranger_id = stranger.id
                stranger_thread_id = stranger_thread.id
                global_agent_id = global_agent.id

            async with session_scope(factory) as session:
                artifacts = ArtifactRepository(session)
                assert (
                    await artifacts.get_for_owner(mismatched_id, owner_id)
                    is None
                )
                assert (
                    await artifacts.get_for_owner(mismatched_id, stranger_id)
                    is None
                )
                assert (
                    await artifacts.list_for_thread(
                        stranger_thread_id,
                        owner_id,
                    )
                    == []
                )
                assert (
                    await artifacts.list_for_thread(
                        stranger_thread_id,
                        stranger_id,
                    )
                    == []
                )
                assert (
                    await session.get(Agent, global_agent_id)
                ).name == "System-updated global"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_atomic_event_sequence_under_concurrent_writers(
    migrated_database_uri: str,
):
    async def scenario() -> None:
        engine = create_platform_engine(migrated_database_uri)
        factory = create_session_factory(engine)
        try:
            async with session_scope(factory) as session:
                owner = await UserRepository(session).create(
                    display_name="Concurrent owner"
                )
                agent = await AgentRepository(session).create(
                    owner_user_id=owner.id,
                    graph_id="concurrent",
                    name="Concurrent",
                )
                thread = await ThreadRepository(session).create(
                    owner_user_id=owner.id,
                    agent_id=agent.id,
                )
                run, _ = await RunRepository(session).create_idempotent(
                    owner_user_id=owner.id,
                    thread_id=thread.id,
                    agent_id=agent.id,
                )
                owner_id = owner.id
                run_id = run.id

            async def append_event(number: int) -> None:
                async with session_scope(factory) as session:
                    await RunEventRepository(session).append(
                        run_id=run_id,
                        owner_user_id=owner_id,
                        event_type="chunk",
                        payload={"number": number},
                    )

            await asyncio.gather(*(append_event(i) for i in range(8)))

            async with session_scope(factory) as session:
                replay = await RunEventRepository(session).replay(
                    run_id=run_id,
                    owner_user_id=owner_id,
                )
                assert [event.sequence for event in replay] == list(range(1, 9))
                assert {event.payload["number"] for event in replay} == set(range(8))
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_foreign_keys_deletion_and_immutable_audit_log(
    migrated_database_uri: str,
):
    async def scenario() -> None:
        engine = create_platform_engine(migrated_database_uri)
        factory = create_session_factory(engine)
        try:
            async with session_scope(factory) as session:
                user = await UserRepository(session).create(display_name="Delete owner")
                agent = await AgentRepository(session).create(
                    owner_user_id=user.id,
                    graph_id="delete-test",
                    name="Delete test",
                )
                thread = await ThreadRepository(session).create(
                    owner_user_id=user.id,
                    agent_id=agent.id,
                )
                run, _ = await RunRepository(session).create_idempotent(
                    owner_user_id=user.id,
                    thread_id=thread.id,
                    agent_id=agent.id,
                )
                event = await RunEventRepository(session).append(
                    run_id=run.id,
                    owner_user_id=user.id,
                    event_type="created",
                )
                artifact = await ArtifactRepository(session).create(
                    owner_user_id=user.id,
                    thread_id=thread.id,
                    run_id=run.id,
                    kind="report",
                    media_type="text/plain",
                )
                session.add(
                    ApiKey(
                        user_id=user.id,
                        key_prefix="visible-prefix",
                        secret_hash="not-plaintext",
                    )
                )
                user_id = user.id
                agent_id = agent.id
                run_id = run.id
                event_sequence = event.sequence
                artifact_id = artifact.id

            async with session_scope(factory) as session:
                await session.execute(delete(Run).where(Run.id == run_id))

            async with session_scope(factory) as session:
                assert await session.get(RunEvent, (run_id, event_sequence)) is None
                assert (await session.get(Artifact, artifact_id)).run_id is None

            async with session_scope(factory) as session:
                thread = await session.scalar(
                    select(Thread).where(Thread.owner_user_id == user_id)
                )
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await session.execute(delete(Agent).where(Agent.id == agent_id))

            async with session_scope(factory) as session:
                await session.execute(delete(User).where(User.id == user_id))

            async with session_scope(factory) as session:
                assert await session.get(User, user_id) is None
                assert await session.get(Thread, thread.id) is None
                assert await session.get(Artifact, artifact_id) is None
                assert (
                    await session.scalar(
                        select(ApiKey).where(ApiKey.user_id == user_id)
                    )
                    is None
                )
                assert (await session.get(Agent, agent_id)).owner_user_id is None

            async with session_scope(factory) as session:
                audit_actor = await UserRepository(session).create(
                    display_name="Audit actor"
                )
                audit = AuditLog(
                    actor_user_id=audit_actor.id,
                    action="thread.read",
                    resource_type="thread",
                    resource_id=str(thread.id),
                )
                session.add(audit)
                await session.flush()
                audit_id = audit.id
                audit_actor_id = audit_actor.id

            async with session_scope(factory) as session:
                with pytest.raises(DBAPIError):
                    async with session.begin_nested():
                        await session.execute(
                            text(
                                "UPDATE platform.audit_log "
                                "SET action = 'changed' WHERE id = :id"
                            ),
                            {"id": audit_id},
                        )

            async with session_scope(factory) as session:
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await session.execute(
                            delete(User).where(User.id == audit_actor_id)
                        )
                with pytest.raises(DBAPIError, match="immutable"):
                    async with session.begin_nested():
                        await session.execute(
                            delete(AuditLog).where(AuditLog.id == audit_id)
                        )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_failed_transaction_rolls_back_repository_flushes(
    migrated_database_uri: str,
):
    async def scenario() -> None:
        engine = create_platform_engine(migrated_database_uri)
        factory = create_session_factory(engine)
        try:
            with pytest.raises(RuntimeError, match="force rollback"):
                async with session_scope(factory) as session:
                    await UserRepository(session).create(
                        display_name="Rolled back",
                        email="rollback@example.test",
                    )
                    raise RuntimeError("force rollback")

            async with session_scope(factory) as session:
                assert (
                    await UserRepository(session).get_by_email("rollback@example.test")
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())
