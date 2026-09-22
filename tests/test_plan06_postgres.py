"""PostgreSQL tests for Plan 05 remediation and Plan 06 tenancy."""

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph
from psycopg import sql
from sqlalchemy import select

from agent_platform.core.settings import Settings
from agent_platform.db.models import AuditLog, Run, Thread
from agent_platform.db.repositories.runs import RunRepository as RunRepositoryClass
from agent_platform.db.session import session_scope
from agent_platform.main import create_app
from agent_platform.process_lock import (
    API_PROCESS_ADVISORY_LOCK_CLASSID,
    API_PROCESS_ADVISORY_LOCK_OBJID,
)
from agent_platform.services.accounts import AccountService
from agent_platform.services.run_manager import RunManager
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.postgres
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PEPPER = "plan06-test-pepper"


def _database_uri_with_name(uri: str, database_name: str) -> str:
    parsed = urlsplit(uri)
    return urlunsplit(parsed._replace(path=f"/{database_name}"))


@pytest.fixture
def postgres_database_uri() -> Iterator[str]:
    """Create and migrate a fresh database for each Plan 06 test."""
    admin_uri = os.getenv("TEST_DATABASE_URI")
    if not admin_uri:
        pytest.skip("set TEST_DATABASE_URI to run PostgreSQL integration tests")
    database_name = f"agent_platform_plan06_{uuid.uuid4().hex}"
    with psycopg.connect(admin_uri, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    database_uri = _database_uri_with_name(admin_uri, database_name)
    try:
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        with patch.dict(os.environ, {"DATABASE_URI": database_uri}):
            command.upgrade(config, "head")
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


def _settings(database_uri: str, **overrides: Any) -> Settings:
    payload: dict[str, Any] = {
        "ENVIRONMENT": "test",
        "DATABASE_URI": database_uri,
        "API_MAX_CONCURRENT_RUNS": 2,
    }
    payload.update(overrides)
    return Settings(_env_file=None, **payload)


def _auth_settings(database_uri: str, **overrides: Any) -> Settings:
    payload = {
        "AUTH_MODE": "api_key",
        "API_KEY_PEPPER": PEPPER,
        "API_KEY_LAST_USED_MIN_INTERVAL_SECONDS": 3600,
    }
    payload.update(overrides)
    return _settings(database_uri, **payload)


def _echo_graph():
    def build(*, checkpointer: Any, store: Any):
        async def respond(state: MessagesState, config: RunnableConfig) -> dict[str, Any]:
            content = str(state["messages"][-1].content)
            configurable = config.get("configurable") or {}
            metadata = config.get("metadata") or {}
            return {
                "messages": [
                    AIMessage(
                        content=(
                            f"Echo: {content}:"
                            f"{configurable.get('user_id')}:"
                            f"{metadata.get('run_id')}"
                        )
                    )
                ]
            }

        builder = StateGraph(MessagesState)
        builder.add_node("respond", respond)
        builder.add_edge(START, "respond")
        builder.add_edge("respond", END)
        return builder.compile(checkpointer=checkpointer, store=store)

    return build


def _payload(content: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "assistant_id": "agrihub",
        "input": {"messages": [{"type": "human", "content": content}]},
        "stream_mode": ["values"],
        "multitask_strategy": "reject",
    }
    body.update(extra)
    return body


def _parse_sse(raw: str) -> list[dict[str, Any]]:
    events = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        item: dict[str, Any] = {"id": None, "event": None, "data": None}
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("id:"):
                item["id"] = line.split(":", 1)[1].strip()
            elif line.startswith("event:"):
                item["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if data_lines:
            item["data"] = json.loads("\n".join(data_lines))
        events.append(item)
    return events


async def _create_thread(client: AsyncClient, headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = await client.post(
        "/threads",
        json={"metadata": {"graph_id": "agrihub"}},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_both_completion_writes_fail_then_reconcile_without_graph_failure(
    postgres_database_uri: str,
    caplog: pytest.LogCaptureFixture,
):
    async def scenario() -> None:
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_echo_graph(),
        )
        original = RunRepositoryClass.set_status_for_owner
        allow = asyncio.Event()

        async def wrapped(self, run_id, owner_user_id, status, **kwargs):
            if status == "completed" and not allow.is_set():
                raise RuntimeError("metadata store unavailable")
            return await original(self, run_id, owner_user_id, status, **kwargs)

        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = await _create_thread(client)
                messages: list[str] = []

                class _Capture(logging.Handler):
                    def emit(self, record: logging.LogRecord) -> None:
                        messages.append(record.getMessage())

                capture = _Capture()
                target_logger = logging.getLogger("agent_platform.services.run_manager")
                previous_level = target_logger.level
                previous_disable = logging.root.manager.disable
                logging.disable(logging.NOTSET)
                target_logger.disabled = False
                target_logger.setLevel(logging.INFO)
                target_logger.addHandler(capture)
                try:
                    with patch.object(RunRepositoryClass, "set_status_for_owner", wrapped):
                        response = await client.post(
                            f"/threads/{thread['thread_id']}/runs/stream",
                            json=_payload("hello"),
                        )
                    rendered = "\n".join(messages)
                finally:
                    target_logger.removeHandler(capture)
                    target_logger.setLevel(previous_level)
                    logging.disable(previous_disable)
                assert response.status_code == 200
                assert "Echo: hello" in response.text
                assert "run_failed" not in response.text
                manager = app.state.run_manager
                assert "terminal_persistence_unconfirmed" in rendered
                assert "graph_succeeded=True" in rendered
                assert manager._pending_terminals
                async with session_scope(app.state.session_factory) as session:
                    run = await session.scalar(select(Run))
                    assert run is not None
                    assert run.status in {"pending", "running"}
                    assert run.error_message != "Graph execution failed"
                allow.set()
                applied = await manager.reconcile_pending()
                assert applied == 1
                assert manager._pending_terminals == {}
                async with session_scope(app.state.session_factory) as session:
                    run = await session.scalar(select(Run))
                    assert run is not None
                    assert run.status == "completed"
                    assert run.error_message != "Graph execution failed"
                    assert run.output_summary["persistence_reconciled"] is True
                follow = await client.post(
                    f"/threads/{thread['thread_id']}/runs/stream",
                    json=_payload("again"),
                )
                assert follow.status_code == 200, follow.text
                assert "Echo: again" in follow.text

    asyncio.run(scenario())


def test_terminal_writes_remain_unconfirmed_through_shutdown(
    postgres_database_uri: str,
    caplog: pytest.LogCaptureFixture,
):
    async def scenario() -> None:
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_echo_graph(),
        )
        original = RunRepositoryClass.set_status_for_owner

        async def wrapped(self, run_id, owner_user_id, status, **kwargs):
            if status == "completed":
                raise RuntimeError("metadata store unavailable")
            return await original(self, run_id, owner_user_id, status, **kwargs)

        messages: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                messages.append(record.getMessage())

        capture = _Capture()
        target_logger = logging.getLogger("agent_platform.services.run_manager")
        previous_disable = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        target_logger.disabled = False
        target_logger.addHandler(capture)
        try:
            with patch.object(RunRepositoryClass, "set_status_for_owner", wrapped):
                async with app.router.lifespan_context(app):
                    async with AsyncClient(
                        transport=ASGITransport(app=app),
                        base_url="http://test",
                    ) as client:
                        thread = await _create_thread(client)
                        response = await client.post(
                            f"/threads/{thread['thread_id']}/runs/stream",
                            json=_payload("kept"),
                        )
                        assert response.status_code == 200
                        assert "Echo: kept" in response.text
                        assert "run_failed" not in response.text
        finally:
            target_logger.removeHandler(capture)
            logging.disable(previous_disable)
        assert "terminal_persistence_unconfirmed" in "\n".join(messages)
        assert "graph_succeeded=True" in "\n".join(messages)
        async with session_scope(app.state.session_factory) as session:
            run = await session.scalar(select(Run))
            assert run is not None
            assert run.status in {"pending", "running"}
            assert run.status != "failed"
            assert run.error_message != "Graph execution failed"

    asyncio.run(scenario())


def test_queue_overflow_catches_up_without_cancelling(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        gates = {"started": asyncio.Event(), "release": asyncio.Event()}

        def build(*, checkpointer: Any, store: Any):
            async def respond(state: MessagesState) -> dict[str, Any]:
                gates["started"].set()
                await gates["release"].wait()
                content = str(state["messages"][-1].content)
                return {"messages": [AIMessage(content=f"Echo: {content}")]}

            builder = StateGraph(MessagesState)
            builder.add_node("respond", respond)
            builder.add_edge(START, "respond")
            builder.add_edge("respond", END)
            return builder.compile(checkpointer=checkpointer, store=store)

        app = create_app(
            settings=_settings(
                postgres_database_uri,
                API_STREAM_SUBSCRIBER_QUEUE_SIZE=1,
            ),
            graph_builder=build,
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = await _create_thread(client)
            manager = app.state.run_manager
            owner = app.state.settings.DEVELOPMENT_USER_ID
            from agent_platform.api.schemas import RunStreamRequest

            starter = asyncio.create_task(
                manager.start_run(
                    uuid.UUID(thread["thread_id"]),
                    RunStreamRequest.model_validate(
                        _payload("burst", on_disconnect="cancel")
                    ),
                    owner,
                )
            )
            await gates["started"].wait()
            view = await starter
            slow = await manager._subscribe(view.id, cancel_on_disconnect=True)
            assert slow is not None
            fast_events: list[int] = []

            async def read_fast() -> None:
                async for raw in manager.stream_events(
                    thread_id=view.thread_id,
                    run_id=view.id,
                    owner_user_id=owner,
                    after_sequence=0,
                    cancel_on_disconnect=False,
                ):
                    parsed = _parse_sse(raw.decode())
                    if parsed and parsed[0]["id"] is not None:
                        fast_events.append(int(parsed[0]["id"]))

            fast = asyncio.create_task(read_fast())
            await asyncio.sleep(0.05)
            for number in range(6):
                await manager.persist_and_publish(
                    run_id=view.id,
                    owner_user_id=owner,
                    event_type="values",
                    payload={"number": number, "owner": str(owner)},
                )
            assert slow.lagging is True
            async with manager._lock:
                entry = manager._runs[view.id]
                assert entry.cancel_requested is False
            gates["release"].set()
            await fast
            await entry.task
            assert entry.task.cancelled() is False
            slow_events: list[int] = []
            async for raw in manager.stream_events(
                thread_id=view.thread_id,
                run_id=view.id,
                owner_user_id=owner,
                after_sequence=0,
                cancel_on_disconnect=True,
            ):
                parsed = _parse_sse(raw.decode())
                if parsed and parsed[0]["id"] is not None:
                    slow_events.append(int(parsed[0]["id"]))
            async with session_scope(app.state.session_factory) as session:
                run = await session.get(Run, view.id)
                assert run is not None
                assert run.status == "completed"
            assert fast_events == list(range(1, len(fast_events) + 1))
            assert slow_events == fast_events
            await manager._unsubscribe(view.id, slow, disconnect_cancel=False)

    asyncio.run(scenario())


def test_task_registration_is_atomic_under_the_manager_lock(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_echo_graph(),
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = await _create_thread(client)
            manager = app.state.run_manager
            owner = app.state.settings.DEVELOPMENT_USER_ID
            from agent_platform.api.schemas import RunStreamRequest

            request = RunStreamRequest.model_validate(_payload("register"))
            seen: dict[str, Any] = {}
            original = RunManager._execute

            async def watching(self, **kwargs):
                seen["present"] = kwargs["run_id"] in self._runs
                seen["attached"] = (
                    self._runs[kwargs["run_id"]].task is asyncio.current_task()
                )
                await original(self, **kwargs)

            with patch.object(RunManager, "_execute", watching):
                view = await manager.start_run(
                    uuid.UUID(thread["thread_id"]),
                    request,
                    owner,
                )
                entry = manager._runs.get(view.id)
                if entry is not None and entry.task is not None:
                    await entry.task
            assert seen["present"] is True
            assert seen["attached"] is True
            assert view.id not in manager._runs
            async with session_scope(app.state.session_factory) as session:
                run = await session.get(Run, view.id)
            assert run is not None and run.status == "completed"

            started = asyncio.Event()
            release = asyncio.Event()

            async def paused(self, **kwargs):
                seen["paused_id"] = kwargs["run_id"]
                started.set()
                await release.wait()
                await original(self, **kwargs)

            with patch.object(RunManager, "_execute", paused):
                starter = asyncio.create_task(
                    manager.start_run(
                        uuid.UUID(thread["thread_id"]),
                        RunStreamRequest.model_validate(_payload("subscriber")),
                        owner,
                    )
                )
                await started.wait()
                subscriber = await manager._subscribe(
                    seen["paused_id"],
                    cancel_on_disconnect=False,
                )
                assert subscriber is not None
                paused_entry = manager._runs[seen["paused_id"]]
                release.set()
                await starter
                assert paused_entry.task is not None
                await paused_entry.task

            real_create = asyncio.create_task

            def fail_execute(coro, **kwargs):
                name = getattr(getattr(coro, "cr_code", None), "co_name", "")
                if name == "_execute":
                    coro.close()
                    raise RuntimeError("cannot schedule")
                return real_create(coro, **kwargs)

            with patch("asyncio.create_task", fail_execute):
                with pytest.raises(RuntimeError, match="cannot schedule"):
                    await manager.start_run(
                        uuid.UUID(thread["thread_id"]),
                        RunStreamRequest.model_validate(_payload("schedule")),
                        owner,
                    )
            async with session_scope(app.state.session_factory) as session:
                runs = list((await session.scalars(select(Run))).all())
            failed = [run for run in runs if run.error_code == "task_scheduling_failed"]
            assert len(failed) == 1
            assert failed[0].status == "cancelled"
            assert failed[0].id not in manager._runs

            def cancel_while_scheduling(coro, **kwargs):
                name = getattr(getattr(coro, "cr_code", None), "co_name", "")
                if name == "_execute":
                    for entry in manager._runs.values():
                        if entry.task is None:
                            entry.cancel_requested = True
                return real_create(coro, **kwargs)

            with patch("asyncio.create_task", cancel_while_scheduling):
                cancelled = await manager.start_run(
                    uuid.UUID(thread["thread_id"]),
                    RunStreamRequest.model_validate(_payload("early-cancel")),
                    owner,
                )
            for _ in range(50):
                async with session_scope(app.state.session_factory) as session:
                    row = await session.get(Run, cancelled.id)
                if row is not None and row.status == "cancelled":
                    break
                await asyncio.sleep(0.02)
            assert row is not None
            assert row.status == "cancelled"
            assert row.error_code == "cancelled"

    asyncio.run(scenario())


def test_second_lifespan_cannot_take_the_advisory_lock(postgres_database_uri: str):
    async def scenario() -> None:
        first = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_echo_graph(),
        )
        second = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_echo_graph(),
        )
        async with first.router.lifespan_context(first):
            with pytest.raises(RuntimeError, match="advisory lock"):
                async with second.router.lifespan_context(second):
                    pass
            assert first.state.ready is True
        async with second.router.lifespan_context(second):
            assert second.state.ready is True

    asyncio.run(scenario())


def test_uvicorn_workers_cannot_start_two_run_managers(postgres_database_uri: str):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URI": postgres_database_uri,
            "ENVIRONMENT": "test",
            "AUTH_MODE": "disabled",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "agent_platform.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "2",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        seen = ""
        for _ in range(80):
            assert process.stderr is not None
            chunk = process.stderr.readline()
            seen += chunk
            if "advisory lock" in seen:
                break
        assert "advisory lock" in seen
        with psycopg.connect(postgres_database_uri) as connection:
            count = connection.execute(
                """
                SELECT count(*)
                FROM pg_locks
                WHERE locktype = 'advisory'
                  AND classid = %s
                  AND objid = %s
                """,
                (
                    API_PROCESS_ADVISORY_LOCK_CLASSID,
                    API_PROCESS_ADVISORY_LOCK_OBJID,
                ),
            ).fetchone()
        assert count is not None
        assert count[0] <= 1
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=15)


def test_protocol_status_pages_past_one_thousand_threads(postgres_database_uri: str):
    async def scenario() -> None:
        calls = {"state": 0}

        def build(*, checkpointer: Any, store: Any):
            graph_builder = _echo_graph()
            graph = graph_builder(checkpointer=checkpointer, store=store)

            class Counting:
                def __getattr__(self, name: str) -> Any:
                    return getattr(graph, name)

                async def aget_state(self, config: dict[str, Any]) -> Any:
                    calls["state"] += 1
                    return await graph.aget_state(config)

            return Counting()

        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=build,
        )
        async with app.router.lifespan_context(app):
            owner = app.state.settings.DEVELOPMENT_USER_ID
            agent_id = app.state.settings.DEVELOPMENT_AGENT_ID
            origin = datetime(2024, 1, 1, tzinfo=UTC)
            async with session_scope(app.state.session_factory) as session:
                threads: list[Thread] = []
                for index in range(1020):
                    threads.append(
                        Thread(
                            owner_user_id=owner,
                            agent_id=agent_id,
                            metadata_={"batch": "status-page"},
                            last_activity_at=origin + timedelta(seconds=index),
                        )
                    )
                session.add_all(threads)
                await session.flush()
                for index, status in (
                    *[(index, "failed") for index in range(1015, 1020)],
                    *[(index, "interrupted") for index in range(1010, 1015)],
                    *[(index, "running") for index in range(1005, 1010)],
                ):
                    session.add(
                        Run(
                            thread_id=threads[index].id,
                            agent_id=agent_id,
                            status=status,
                            input={},
                            configuration={},
                        )
                    )
                await session.flush()
                idle_ids = {thread.id for thread in threads[:1005]}
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                errors = await client.post(
                    "/threads/search",
                    json={"status": "error", "metadata": {"batch": "status-page"}, "limit": 10},
                )
                assert errors.status_code == 200
                assert len(errors.json()) == 5
                assert {item["status"] for item in errors.json()} == {"error"}
                assert calls["state"] == 5
                idle = await client.post(
                    "/threads/search",
                    json={
                        "status": "idle",
                        "metadata": {"batch": "status-page"},
                        "limit": 5,
                        "offset": 1000,
                    },
                )
                assert idle.status_code == 200, idle.text
                body = idle.json()
                assert len(body) == 5
                assert {item["status"] for item in body} == {"idle"}
                assert {uuid.UUID(item["thread_id"]) for item in body} <= idle_ids
                assert calls["state"] == 10

    asyncio.run(scenario())


def test_api_key_auth_headers_revocation_and_redaction(
    postgres_database_uri: str,
    caplog: pytest.LogCaptureFixture,
):
    async def scenario() -> None:
        app = create_app(
            settings=_auth_settings(postgres_database_uri),
            graph_builder=_echo_graph(),
        )
        async with app.router.lifespan_context(app):
            accounts: AccountService = app.state.accounts
            user = await accounts.create_user(display_name="Key Owner")
            issued = await accounts.issue_api_key(user_id=user.id, label="primary")
            async with session_scope(app.state.session_factory) as session:
                from agent_platform.db.models import ApiKey

                row = await session.get(ApiKey, issued.id)
                assert row is not None
                assert issued.plaintext not in row.secret_hash
                assert issued.plaintext not in (row.key_prefix or "")
                dumped = json.dumps(
                    {
                        "prefix": row.key_prefix,
                        "hash": row.secret_hash,
                        "label": row.label,
                    }
                )
                assert issued.plaintext not in dumped
            headers = {"X-Api-Key": issued.plaintext}
            bearer = {"Authorization": f"Bearer {issued.plaintext}"}
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                with caplog.at_level("INFO"):
                    created = await client.post(
                        "/threads",
                        json={"metadata": {"graph_id": "agrihub"}},
                        headers=headers,
                    )
                    assert created.status_code == 200, created.text
                    assert created.headers["x-request-id"]
                    second = await client.post(
                        "/threads",
                        json={"metadata": {"graph_id": "agrihub"}},
                        headers=bearer,
                    )
                    assert second.status_code == 200
                    missing = await client.post(
                        "/threads",
                        json={"metadata": {"graph_id": "agrihub"}},
                    )
                    malformed = await client.post(
                        "/threads",
                        json={"metadata": {"graph_id": "agrihub"}},
                        headers={"Authorization": "Basic abc"},
                    )
                    conflict = await client.post(
                        "/threads",
                        json={"metadata": {"graph_id": "agrihub"}},
                        headers={
                            "X-Api-Key": issued.plaintext,
                            "Authorization": "Bearer aghub_differentsecretvalue",
                        },
                    )
                    unknown = await client.post(
                        "/threads",
                        json={"metadata": {"graph_id": "agrihub"}},
                        headers={"X-Api-Key": "aghub_unknown1unknownsecretvalue"},
                    )
                for response in (missing, malformed, conflict, unknown):
                    assert response.status_code == 401
                    assert response.json()["detail"] == "invalid authentication credentials"
                    assert issued.plaintext not in response.text
                await accounts.disable_user(user.id)
                disabled = await client.post(
                    "/threads",
                    json={"metadata": {"graph_id": "agrihub"}},
                    headers=headers,
                )
                assert disabled.status_code == 401
                await accounts.enable_user(user.id)
                restored = await client.post(
                    "/threads",
                    json={"metadata": {"graph_id": "agrihub"}},
                    headers=headers,
                )
                assert restored.status_code == 200
                rotated = await accounts.rotate_api_key(issued.id)
                old = await client.get(
                    f"/threads/{created.json()['thread_id']}",
                    headers=headers,
                )
                assert old.status_code == 401
                new = await client.get(
                    f"/threads/{created.json()['thread_id']}",
                    headers={"X-Api-Key": rotated.plaintext},
                )
                assert new.status_code == 200
                expired = await accounts.issue_api_key(
                    user_id=user.id,
                    expires_at=datetime.now(UTC) - timedelta(minutes=1),
                )
                expired_response = await client.get(
                    f"/threads/{created.json()['thread_id']}",
                    headers={"X-Api-Key": expired.plaintext},
                )
                assert expired_response.status_code == 401
            async with session_scope(app.state.session_factory) as session:
                audits = list((await session.scalars(select(AuditLog))).all())
            rendered = json.dumps(
                [
                    {
                        "action": row.action,
                        "metadata": row.metadata_,
                        "resource_id": row.resource_id,
                    }
                    for row in audits
                ]
            )
            assert issued.plaintext not in rendered
            assert rotated.plaintext not in rendered
            assert issued.plaintext not in caplog.text
            assert rotated.plaintext not in caplog.text
            async with session_scope(app.state.session_factory) as session:
                from agent_platform.db.models import ApiKey

                current = await session.get(ApiKey, rotated.id)
                assert current is not None
                first_used = current.last_used_at
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                again = await client.get("/info")
                assert again.status_code == 200
                touched = await client.get(
                    f"/threads/{created.json()['thread_id']}",
                    headers={"X-Api-Key": rotated.plaintext},
                )
                assert touched.status_code == 200
            async with session_scope(app.state.session_factory) as session:
                current = await session.get(ApiKey, rotated.id)
                assert current is not None
                assert current.last_used_at == first_used

    asyncio.run(scenario())


def test_two_tenants_cannot_cross_resources(postgres_database_uri: str):
    async def scenario() -> None:
        app = create_app(
            settings=_auth_settings(postgres_database_uri),
            graph_builder=_echo_graph(),
        )
        async with app.router.lifespan_context(app):
            accounts: AccountService = app.state.accounts
            user_a = await accounts.create_user(display_name="Tenant A")
            user_b = await accounts.create_user(display_name="Tenant B")
            key_a = await accounts.issue_api_key(user_id=user_a.id, label="a")
            key_b = await accounts.issue_api_key(user_id=user_b.id, label="b")
            headers_a = {"X-Api-Key": key_a.plaintext}
            headers_b = {"X-Api-Key": key_b.plaintext}
            shared_thread = str(uuid.uuid4())
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                created = await client.post(
                    "/threads",
                    json={
                        "thread_id": shared_thread,
                        "metadata": {
                            "graph_id": "agrihub",
                            "owner_user_id": str(user_b.id),
                        },
                    },
                    headers=headers_a,
                )
                assert created.status_code == 200, created.text
                assert "owner_user_id" not in created.json()["metadata"]
                hidden = [
                    await client.post(
                        "/threads/search",
                        json={"ids": [shared_thread], "limit": 10},
                        headers=headers_b,
                    ),
                    await client.get(f"/threads/{shared_thread}", headers=headers_b),
                    await client.get(
                        f"/threads/{shared_thread}/state",
                        headers=headers_b,
                    ),
                    await client.post(
                        f"/threads/{shared_thread}/history",
                        json={"limit": 5},
                        headers=headers_b,
                    ),
                ]
                assert hidden[0].json() == []
                assert [item.status_code for item in hidden[1:]] == [404, 404, 404]
                run = await client.post(
                    f"/threads/{shared_thread}/runs/stream",
                    json=_payload(
                        "tenant",
                        config={
                            "configurable": {
                                "user_id": str(user_b.id),
                                "api_key": "sk-client-secret",
                            }
                        },
                        context={"user_id": str(user_b.id)},
                    ),
                    headers=headers_a,
                )
                assert run.status_code == 200, run.text
                assert str(user_a.id) in run.text
                assert str(user_b.id) not in run.text
                assert "sk-client-secret" not in run.text
                events = _parse_sse(run.text)
                metadata = next(item for item in events if item["event"] == "metadata")
                run_id = metadata["data"]["run_id"]
                denied_run = await client.get(
                    f"/threads/{shared_thread}/runs/{run_id}/stream",
                    headers=headers_b,
                )
                denied_cancel = await client.post(
                    f"/threads/{shared_thread}/runs/{run_id}/cancel",
                    headers=headers_b,
                )
                denied_resume = await client.post(
                    f"/threads/{shared_thread}/runs/stream",
                    json={"assistant_id": "agrihub", "command": {"resume": {"ok": True}}},
                    headers=headers_b,
                )
                assert denied_run.status_code == 404
                assert denied_cancel.status_code == 404
                assert denied_resume.status_code == 404
                own_thread = await _create_thread(client, headers_b)
                assert own_thread["thread_id"] != shared_thread
                usable = await client.post(
                    f"/threads/{own_thread['thread_id']}/runs/stream",
                    json=_payload("mine"),
                    headers=headers_b,
                )
                assert usable.status_code == 200
                assert str(user_b.id) in usable.text
            with pytest.raises(PermissionError):
                await accounts.update_agent_for_user(
                    user_b.id,
                    app.state.settings.DEVELOPMENT_AGENT_ID,
                    name="Hijacked",
                )
            renamed = await accounts.update_global_agent(
                app.state.settings.DEVELOPMENT_AGENT_ID,
                name="AgriHub Research Agent",
                actor_user_id=user_a.id,
            )
            assert renamed == app.state.settings.DEVELOPMENT_AGENT_ID
            async with session_scope(app.state.session_factory) as session:
                from agent_platform.db.repositories import ArtifactRepository

                artifact = await ArtifactRepository(session).create(
                    owner_user_id=user_a.id,
                    thread_id=uuid.UUID(shared_thread),
                    kind="note",
                    media_type="text/plain",
                    text_content="tenant-a-only",
                )
                artifact_id = artifact.id
            with pytest.raises(LookupError):
                await accounts.read_artifact(user_b.id, artifact_id)
            assert await accounts.read_artifact(user_a.id, artifact_id) == artifact_id
            async with session_scope(app.state.session_factory) as session:
                audits = list((await session.scalars(select(AuditLog))).all())
            actions = {row.action for row in audits}
            assert "access.denied" in actions
            assert "thread.created" in actions
            assert "run.created" in actions
            assert "agent.global_updated" in actions

    asyncio.run(scenario())


def test_admin_cli_prints_a_key_once(postgres_database_uri: str):
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URI": postgres_database_uri,
            "ENVIRONMENT": "test",
            "AUTH_MODE": "api_key",
            "API_KEY_PEPPER": PEPPER,
        }
    )

    def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "agent_platform", *args],
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    created = run_cli("create-user", "--display-name", "CLI User")
    assert created.returncode == 0, created.stderr
    user_id = json.loads(created.stdout)["user_id"]
    issued = run_cli("issue-key", "--user-id", user_id, "--label", "cli")
    assert issued.returncode == 0, issued.stderr
    payload = json.loads(issued.stdout)
    assert payload["api_key"].startswith("aghub_")
    assert payload["api_key"] not in issued.stderr
    listed = run_cli("list-keys", "--user-id", user_id)
    assert listed.returncode == 0
    assert payload["api_key"] not in listed.stdout
    assert payload["key_prefix"] in listed.stdout
    revoked = run_cli("revoke-key", "--key-id", payload["id"])
    assert revoked.returncode == 0
    missing = run_cli("revoke-key", "--key-id", str(uuid.uuid4()))
    assert missing.returncode == 1
    disabled = run_cli("disable-user", "--user-id", user_id)
    assert disabled.returncode == 0
    enabled = run_cli("enable-user", "--user-id", user_id)
    assert enabled.returncode == 0
    deleted = run_cli("delete-user", "--user-id", user_id)
    assert deleted.returncode == 0
    cannot_enable = run_cli("enable-user", "--user-id", user_id)
    assert cannot_enable.returncode == 1


def test_sdk_contract_with_api_key(postgres_database_uri: str):
    async def scenario() -> None:
        app = create_app(
            settings=_auth_settings(postgres_database_uri),
            graph_builder=_echo_graph(),
        )
        async with app.router.lifespan_context(app):
            accounts: AccountService = app.state.accounts
            user = await accounts.create_user(display_name="SDK User")
            issued = await accounts.issue_api_key(user_id=user.id, label="sdk")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            import uvicorn

            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    log_level="warning",
                    lifespan="off",
                )
            )
            server_task = asyncio.create_task(server.serve())
            try:
                for _ in range(100):
                    if server.started:
                        break
                    await asyncio.sleep(0.02)
                assert server.started
                script = """
import { Client } from "@langchain/langgraph-sdk";
const client = new Client({
  apiUrl: process.env.SDK_API_URL,
  apiKey: process.env.SDK_API_KEY,
});
const thread = await client.threads.create({ metadata: { graph_id: "agrihub" } });
const events = [];
for await (const chunk of client.runs.stream(thread.thread_id, "agrihub", {
  input: { messages: [{ type: "human", content: "sdk" }] },
  streamMode: ["values"],
})) {
  events.push(chunk.event);
}
const state = await client.threads.getState(thread.thread_id);
let invalid = "";
try {
  const bad = new Client({
    apiUrl: process.env.SDK_API_URL,
    apiKey: "aghub_invalidinvalidinvalid",
  });
  await bad.threads.create({ metadata: { graph_id: "agrihub" } });
} catch (error) {
  invalid = String(error && error.message ? error.message : error);
}
console.log(JSON.stringify({
  events,
  message: state.values.messages.at(-1).content,
  invalid,
}));
"""
                completed = await asyncio.to_thread(
                    subprocess.run,
                    ["node", "--input-type=module", "-e", script],
                    cwd=PROJECT_ROOT / "frontend",
                    env={
                        **os.environ,
                        "SDK_API_URL": f"http://127.0.0.1:{port}",
                        "SDK_API_KEY": issued.plaintext,
                    },
                    check=False,
                    capture_output=True,
                    text=True,
                )
                assert completed.returncode == 0, completed.stderr
                result = json.loads(completed.stdout.strip().splitlines()[-1])
                assert "metadata" in result["events"]
                assert "Echo: sdk" in result["message"]
                assert "401" in result["invalid"] or "invalid authentication" in result["invalid"]
                assert issued.plaintext not in result["invalid"]
                assert issued.plaintext not in completed.stderr
            finally:
                server.should_exit = True
                await server_task

    asyncio.run(scenario())
