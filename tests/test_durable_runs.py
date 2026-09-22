"""PostgreSQL tests for Plan 04 remediation and durable single-process runs."""

import asyncio
import json
import os
import socket
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
import uvicorn
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt
from psycopg import sql
from sqlalchemy import select

from agent_platform.core.settings import Settings
from agent_platform.db.models import Run, RunEvent
from agent_platform.db.repositories import (
    AgentRepository,
    ArtifactRepository,
    RunEventRepository,
    RunRepository,
    ThreadRepository,
    UserRepository,
)
from agent_platform.db.repositories.runs import RunRepository as RunRepositoryClass
from agent_platform.db.session import (
    create_platform_engine,
    create_session_factory,
    session_scope,
)
from agent_platform.main import create_app
from agent_platform.services.run_manager import RunManager
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.postgres
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _database_uri_with_name(uri: str, database_name: str) -> str:
    parsed = urlsplit(uri)
    return urlunsplit(parsed._replace(path=f"/{database_name}"))


@pytest.fixture
def postgres_database_uri() -> Iterator[str]:
    """Create and migrate a fresh database for each durable-run test."""
    admin_uri = os.getenv("TEST_DATABASE_URI")
    if not admin_uri:
        pytest.skip("set TEST_DATABASE_URI to run PostgreSQL integration tests")

    database_name = f"agent_platform_run_test_{uuid.uuid4().hex}"
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


def _echo_graph(*, delay: float = 0, gates: dict[str, asyncio.Event] | None = None):
    def build(*, checkpointer: Any, store: Any):
        async def respond(state: MessagesState) -> dict[str, Any]:
            if delay:
                await asyncio.sleep(delay)
            if gates is not None and "started" in gates:
                gates["started"].set()
                await gates["release"].wait()
            content = str(state["messages"][-1].content)
            if content == "fail":
                raise RuntimeError("provider secret must not escape")
            return {"messages": [AIMessage(content=f"Echo: {content}")]}

        builder = StateGraph(MessagesState)
        builder.add_node("respond", respond)
        builder.add_edge(START, "respond")
        builder.add_edge("respond", END)
        return builder.compile(checkpointer=checkpointer, store=store)

    return build


def _staged_graph(gates: dict[str, asyncio.Event]):
    def build(*, checkpointer: Any, store: Any):
        async def first(state: MessagesState) -> dict[str, Any]:
            return {"messages": [AIMessage(content="one")]}

        async def second(state: MessagesState) -> dict[str, Any]:
            gates["waiting"].set()
            await gates["release"].wait()
            return {"messages": [AIMessage(content="two")]}

        builder = StateGraph(MessagesState)
        builder.add_node("first", first)
        builder.add_node("second", second)
        builder.add_edge(START, "first")
        builder.add_edge("first", "second")
        builder.add_edge("second", END)
        return builder.compile(checkpointer=checkpointer, store=store)

    return build


def _hitl_graph():
    def build(*, checkpointer: Any, store: Any):
        async def review(state: MessagesState) -> dict[str, Any]:
            content = str(state["messages"][-1].content)
            if not content.startswith("interrupt"):
                return {"messages": [AIMessage(content=f"Echo: {content}")]}
            decision = interrupt(
                {
                    "action_requests": [
                        {
                            "name": "publish",
                            "args": {"text": "draft"},
                            "description": "Review the draft",
                        }
                    ],
                    "review_configs": [
                        {
                            "action_name": "publish",
                            "allowed_decisions": ["approve", "edit", "reject"],
                        }
                    ],
                }
            )
            kind = "approve"
            text = "draft"
            if isinstance(decision, dict):
                first = (decision.get("decisions") or [{}])[0]
                kind = first.get("type", "approve")
                if kind == "edit":
                    text = (
                        first.get("edited_action", {})
                        .get("args", {})
                        .get("text", text)
                    )
                elif kind == "reject":
                    text = first.get("message") or "rejected"
            return {"messages": [AIMessage(content=f"decision:{kind}:{text}")]}

        builder = StateGraph(MessagesState)
        builder.add_node("review", review)
        builder.add_edge(START, "review")
        builder.add_edge("review", END)
        return builder.compile(checkpointer=checkpointer, store=store)

    return build


def _sdk_graph():
    def build(*, checkpointer: Any, store: Any):
        async def step(state: MessagesState) -> dict[str, Any]:
            content = str(state["messages"][-1].content)
            if content == "interrupt":
                decision = interrupt(
                    {
                        "action_requests": [
                            {
                                "name": "publish",
                                "args": {"text": "draft"},
                                "description": "Review the draft",
                            }
                        ],
                        "review_configs": [
                            {
                                "action_name": "publish",
                                "allowed_decisions": ["approve", "edit", "reject"],
                            }
                        ],
                    }
                )
                kind = "approve"
                if isinstance(decision, dict) and decision.get("decisions"):
                    kind = decision["decisions"][0].get("type", "approve")
                return {"messages": [AIMessage(content=f"decision:{kind}")]}
            if content == "slow":
                await asyncio.sleep(1.2)
            return {"messages": [AIMessage(content=f"Echo: {content}")]}

        async def tail(state: MessagesState) -> dict[str, Any]:
            return {"messages": [AIMessage(content="tail")]}

        builder = StateGraph(MessagesState)
        builder.add_node("step", step)
        builder.add_node("tail", tail)
        builder.add_edge(START, "step")
        builder.add_edge("step", "tail")
        builder.add_edge("tail", END)
        return builder.compile(checkpointer=checkpointer, store=store)

    return build


def _payload(content: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "assistant_id": "agrihub",
        "input": {"messages": [{"type": "human", "content": content}]},
        "stream_mode": ["values"],
        "stream_resumable": True,
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
        if item["event"] is not None or item["data"] is not None:
            events.append(item)
    return events


async def _create_thread(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/threads",
        json={"metadata": {"graph_id": "agrihub", "client": "test"}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_deleted_cross_owner_and_duplicate_threads_are_controlled(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        settings = _settings(postgres_database_uri)
        app = create_app(settings=settings, graph_builder=_echo_graph())
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                created = await _create_thread(client)
                thread_id = uuid.UUID(created["thread_id"])
                async with session_scope(app.state.session_factory) as session:
                    await ThreadRepository(session).update_for_owner(
                        thread_id,
                        settings.DEVELOPMENT_USER_ID,
                        status="deleted",
                    )
                search = await client.post(
                    "/threads/search",
                    json={
                        "ids": [created["thread_id"]],
                        "metadata": {"graph_id": "agrihub"},
                        "status": "deleted",
                        "limit": 100,
                    },
                )
                assert search.status_code == 200
                assert search.json() == []
                assert (await client.get(f"/threads/{thread_id}")).status_code == 404
                assert (
                    await client.get(f"/threads/{thread_id}/state")
                ).status_code == 404
                assert (
                    await client.post(f"/threads/{thread_id}/history", json={"limit": 5})
                ).status_code == 404
                assert (
                    await client.post(
                        f"/threads/{thread_id}/runs/stream",
                        json=_payload("nope"),
                    )
                ).status_code == 404

                async with session_scope(app.state.session_factory) as session:
                    with pytest.raises(LookupError, match="thread is not owned"):
                        await ArtifactRepository(session).create(
                            owner_user_id=settings.DEVELOPMENT_USER_ID,
                            thread_id=thread_id,
                            kind="note",
                            media_type="text/plain",
                            text_content="hidden",
                        )

                chosen = str(uuid.uuid4())
                first = await client.post(
                    "/threads",
                    json={"thread_id": chosen, "metadata": {"graph_id": "agrihub"}},
                )
                second = await client.post(
                    "/threads",
                    json={"thread_id": chosen, "metadata": {"graph_id": "agrihub"}},
                )
                assert first.status_code == 200
                assert second.status_code == 409

                foreign_id = uuid.uuid4()
                async with session_scope(app.state.session_factory) as session:
                    stranger = await UserRepository(session).create(
                        display_name="Stranger"
                    )
                    agent = await AgentRepository(session).create(
                        owner_user_id=stranger.id,
                        graph_id="foreign",
                        name="Foreign",
                    )
                    await ThreadRepository(session).create(
                        thread_id=foreign_id,
                        owner_user_id=stranger.id,
                        agent_id=agent.id,
                    )
                foreign = await client.post(
                    "/threads",
                    json={
                        "thread_id": str(foreign_id),
                        "metadata": {"graph_id": "agrihub"},
                    },
                )
                assert foreign.status_code == 404

                raced = str(uuid.uuid4())
                responses = await asyncio.gather(
                    client.post(
                        "/threads",
                        json={"thread_id": raced, "metadata": {"graph_id": "agrihub"}},
                    ),
                    client.post(
                        "/threads",
                        json={"thread_id": raced, "metadata": {"graph_id": "agrihub"}},
                    ),
                )
                assert sorted(response.status_code for response in responses) == [200, 409]

    asyncio.run(scenario())


def test_thread_status_reflects_runs_and_checkpoint_reads_are_bounded(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        holder: dict[str, Any] = {}

        def build(*, checkpointer: Any, store: Any):
            async def respond(state: MessagesState) -> dict[str, Any]:
                content = str(state["messages"][-1].content)
                if content == "fail":
                    raise RuntimeError("boom")
                return {"messages": [AIMessage(content=f"Echo: {content}")]}

            builder = StateGraph(MessagesState)
            builder.add_node("respond", respond)
            builder.add_edge(START, "respond")
            builder.add_edge("respond", END)
            graph = builder.compile(checkpointer=checkpointer, store=store)

            class TrackingGraph:
                def __init__(self) -> None:
                    self.active = 0
                    self.maximum = 0
                    self._lock = asyncio.Lock()

                def __getattr__(self, name: str) -> Any:
                    return getattr(graph, name)

                async def aget_state(self, config: dict[str, Any]) -> Any:
                    async with self._lock:
                        self.active += 1
                        self.maximum = max(self.maximum, self.active)
                    try:
                        await asyncio.sleep(0.05)
                        return await graph.aget_state(config)
                    finally:
                        async with self._lock:
                            self.active -= 1

            holder["graph"] = TrackingGraph()
            return holder["graph"]

        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=build,
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                idle = await _create_thread(client)
                failed = await _create_thread(client)
                running = await _create_thread(client)
                for _ in range(3):
                    await _create_thread(client)
                failed_run = await client.post(
                    f"/threads/{failed['thread_id']}/runs/stream",
                    json=_payload("fail"),
                )
                assert failed_run.status_code == 200
                holder["graph"].maximum = 0
                search = await client.post("/threads/search", json={"limit": 100})
                assert search.status_code == 200
                by_id = {item["thread_id"]: item["status"] for item in search.json()}
                assert by_id[idle["thread_id"]] == "idle"
                assert by_id[failed["thread_id"]] == "error"
                assert holder["graph"].maximum <= 4
                assert holder["graph"].maximum >= 2

                async with session_scope(app.state.session_factory) as session:
                    thread = await ThreadRepository(session).get_for_owner(
                        uuid.UUID(running["thread_id"]),
                        app.state.settings.DEVELOPMENT_USER_ID,
                    )
                    assert thread is not None
                    await RunRepository(session).create_idempotent(
                        owner_user_id=app.state.settings.DEVELOPMENT_USER_ID,
                        thread_id=thread.id,
                        agent_id=thread.agent_id,
                    )
                busy = await client.get(f"/threads/{running['thread_id']}")
                assert busy.json()["status"] == "busy"

    asyncio.run(scenario())


def test_graph_success_stays_distinct_from_metadata_persistence_failure(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_echo_graph(),
        )
        original = RunRepositoryClass.set_status_for_owner
        calls = {"completed": 0}

        async def wrapped(self, run_id, owner_user_id, status, **kwargs):
            if status == "completed":
                calls["completed"] += 1
                if calls["completed"] == 1:
                    raise RuntimeError("metadata store unavailable")
            return await original(self, run_id, owner_user_id, status, **kwargs)

        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = await _create_thread(client)
                with patch.object(RunRepositoryClass, "set_status_for_owner", wrapped):
                    response = await client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json=_payload("hello"),
                    )
                assert response.status_code == 200
                assert "persistence_failed" in response.text
                assert "run_failed" not in response.text
                assert "Echo: hello" in response.text
                state = await client.get(f"/threads/{thread['thread_id']}/state")
                assert "Echo: hello" in state.text

            async with session_scope(app.state.session_factory) as session:
                run = await session.scalar(select(Run))
                assert run is not None
                assert run.status == "completed"
                assert run.error_message != "Graph execution failed"
                assert run.output_summary["persistence_reconciled"] is True

    asyncio.run(scenario())


def test_rejected_run_fields_and_always_on_event_persistence(
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
                endpoint = f"/threads/{thread['thread_id']}/runs/stream"
                rejected = await client.post(
                    endpoint,
                    json=_payload("hello", webhook="https://example.test/hook"),
                )
                assert rejected.status_code == 422
                modes = await client.post(
                    endpoint,
                    json=_payload("hello", stream_mode=["messages"]),
                )
                assert modes.status_code == 422
                strategy = await client.post(
                    endpoint,
                    json=_payload("hello", multitask_strategy="enqueue"),
                )
                assert strategy.status_code == 422
                accepted = await client.post(
                    endpoint,
                    json=_payload(
                        "hello",
                        stream_resumable=False,
                        stream_subgraphs=True,
                        durability="sync",
                    ),
                )
                assert accepted.status_code == 200
                assert "id: 1" in accepted.text

            async with session_scope(app.state.session_factory) as session:
                events = list((await session.scalars(select(RunEvent))).all())
                assert events
                assert [event.sequence for event in events] == list(
                    range(1, len(events) + 1)
                )

    asyncio.run(scenario())


def test_events_commit_before_publish_and_sequences_stay_ordered(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        settings = _settings(postgres_database_uri)
        app = create_app(settings=settings, graph_builder=_echo_graph())
        visible: list[bool] = []
        original = RunManager._publish

        async def spy(self, run_id, event):
            async with session_scope(app.state.session_factory) as session:
                rows = await RunEventRepository(session).replay(
                    run_id=run_id,
                    owner_user_id=settings.DEVELOPMENT_USER_ID,
                    after_sequence=event.sequence - 1,
                    limit=1,
                )
            visible.append(bool(rows) and rows[0].sequence == event.sequence)
            await original(self, run_id, event)

        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = await _create_thread(client)
                with patch.object(RunManager, "_publish", spy):
                    response = await client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json=_payload("ordered"),
                    )
                assert response.status_code == 200
                assert visible
                assert all(visible)

                async with session_scope(app.state.session_factory) as session:
                    run = await session.scalar(select(Run))
                    assert run is not None
                    run_id = run.id
                    owner_id = settings.DEVELOPMENT_USER_ID

                async def emit(number: int) -> None:
                    await app.state.run_manager.persist_and_publish(
                        run_id=run_id,
                        owner_user_id=owner_id,
                        event_type="chunk",
                        payload={"number": number},
                    )

                await asyncio.gather(*(emit(number) for number in range(8)))
                async with session_scope(app.state.session_factory) as session:
                    rows = await RunEventRepository(session).replay(
                        run_id=run_id,
                        owner_user_id=owner_id,
                    )
                sequences = [row.sequence for row in rows]
                assert sequences == list(range(1, len(sequences) + 1))
                numbers = [
                    row.payload["number"]
                    for row in rows
                    if row.event_type == "chunk"
                ]
                assert sorted(numbers) == list(range(8))

    asyncio.run(scenario())


def test_disconnect_reconnect_replays_without_gaps_or_duplicates(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        gates = {"waiting": asyncio.Event(), "release": asyncio.Event()}
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_staged_graph(gates),
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                timeout=30,
            ) as client:
                thread = await _create_thread(client)
                endpoint = f"/threads/{thread['thread_id']}/runs/stream"
                disconnected = asyncio.create_task(
                    client.post(endpoint, json=_payload("stage"))
                )
                await gates["waiting"].wait()
                disconnected.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await disconnected

                async def release_after_replay() -> None:
                    await asyncio.sleep(0.2)
                    gates["release"].set()

                releaser = asyncio.create_task(release_after_replay())
                async with session_scope(app.state.session_factory) as session:
                    run = await session.scalar(select(Run).order_by(Run.created_at.desc()))
                    assert run is not None
                    assert run.status == "running"
                    run_id = run.id
                live = await client.get(
                    f"/threads/{thread['thread_id']}/runs/{run_id}/stream",
                    headers={"Last-Event-ID": "1"},
                )
                await releaser
                assert live.status_code == 200, live.text
                replayed = _parse_sse(live.text)
                assert [int(item["id"]) for item in replayed] == [2, 3, 4, 5]
                assert [item["event"] for item in replayed] == [
                    "values",
                    "values",
                    "values",
                    "end",
                ]
                assert replayed[-1]["data"] == {"status": "success"}
                assert live.text.count('"content":"one"') == 2
                assert live.text.count('"content":"two"') == 1
                again = await client.get(
                    f"/threads/{thread['thread_id']}/runs/{run_id}/stream",
                    headers={"Last-Event-ID": "5"},
                )
                assert again.status_code == 200
                assert _parse_sse(again.text) == []

    asyncio.run(scenario())


def test_cancellation_is_idempotent_and_does_not_look_like_graph_failure(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        gates = {"started": asyncio.Event(), "release": asyncio.Event()}
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_echo_graph(gates=gates),
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                timeout=30,
            ) as client:
                thread = await _create_thread(client)
                stream_task = asyncio.create_task(
                    client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json=_payload("wait"),
                    )
                )
                await gates["started"].wait()
                await asyncio.sleep(0.05)
                async with session_scope(app.state.session_factory) as session:
                    run = await session.scalar(select(Run))
                    assert run is not None
                    run_id = run.id
                cancelled = await client.post(
                    f"/threads/{thread['thread_id']}/runs/{run_id}/cancel",
                    params={"wait": "0", "action": "interrupt"},
                )
                assert cancelled.status_code == 200, cancelled.text
                assert cancelled.json()["status"] == "cancelled"
                repeated = await client.post(
                    f"/threads/{thread['thread_id']}/runs/{run_id}/cancel",
                    params={"wait": "1", "action": "interrupt"},
                )
                assert repeated.status_code == 200
                assert repeated.json()["status"] == "cancelled"
                rollback = await client.post(
                    f"/threads/{thread['thread_id']}/runs/{run_id}/cancel",
                    params={"action": "rollback"},
                )
                assert rollback.status_code == 422
                gates["release"].set()
                streamed = await stream_task
                assert "run_failed" not in streamed.text

            async with session_scope(app.state.session_factory) as session:
                run = await session.get(Run, run_id)
                assert run is not None
                assert run.status == "cancelled"
                assert run.error_message == "Run cancelled"
                assert run.status != "running"

    asyncio.run(scenario())


def test_startup_interrupts_orphaned_runs_without_continuing_them(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        settings = _settings(postgres_database_uri)
        calls = {"count": 0}

        def build(*, checkpointer: Any, store: Any):
            async def respond(state: MessagesState) -> dict[str, Any]:
                calls["count"] += 1
                return {"messages": [AIMessage(content="should-not-run")]}

            builder = StateGraph(MessagesState)
            builder.add_node("respond", respond)
            builder.add_edge(START, "respond")
            builder.add_edge("respond", END)
            return builder.compile(checkpointer=checkpointer, store=store)

        first = create_app(settings=settings, graph_builder=build)
        async with first.router.lifespan_context(first):
            pass

        engine = create_platform_engine(postgres_database_uri)
        factory = create_session_factory(engine)
        try:
            async with session_scope(factory) as session:
                thread = await ThreadRepository(session).create(
                    owner_user_id=settings.DEVELOPMENT_USER_ID,
                    agent_id=settings.DEVELOPMENT_AGENT_ID,
                )
                run, _created = await RunRepository(session).create_idempotent(
                    owner_user_id=settings.DEVELOPMENT_USER_ID,
                    thread_id=thread.id,
                    agent_id=settings.DEVELOPMENT_AGENT_ID,
                    input={"messages": [{"type": "human", "content": "orphan"}]},
                )
                await RunRepository(session).set_status_internal(
                    run.id,
                    "running",
                    started_at=datetime.now(UTC),
                )
                run_id = run.id
                thread_id = thread.id
        finally:
            await engine.dispose()

        second = create_app(settings=settings, graph_builder=build)
        async with second.router.lifespan_context(second):
            assert calls["count"] == 0
            async with AsyncClient(
                transport=ASGITransport(app=second),
                base_url="http://test",
            ) as client:
                fetched = await client.get(f"/threads/{thread_id}")
                assert fetched.json()["status"] == "interrupted"
                resumed = await client.post(
                    f"/threads/{thread_id}/runs/stream",
                    json=_payload("retry"),
                )
                assert resumed.status_code == 200
                assert "should-not-run" in resumed.text
            assert calls["count"] == 1
            async with session_scope(second.state.session_factory) as session:
                orphan = await session.get(Run, run_id)
                assert orphan is not None
                assert orphan.status == "interrupted"
                assert orphan.error_code == "process_restart"
                events = await RunEventRepository(session).replay(
                    run_id=run_id,
                    owner_user_id=settings.DEVELOPMENT_USER_ID,
                )
                assert events[-1].payload["reason"] == "process_restart"

    asyncio.run(scenario())


def test_shutdown_cancels_registered_tasks(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        gates = {"started": asyncio.Event(), "release": asyncio.Event()}
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_echo_graph(gates=gates),
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                timeout=30,
            ) as client:
                thread = await _create_thread(client)
                task = asyncio.create_task(
                    client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json=_payload("shutdown"),
                    )
                )
                await gates["started"].wait()
                await app.state.run_manager.shutdown()
                await task
            async with session_scope(app.state.session_factory) as session:
                run = await session.scalar(select(Run))
                assert run is not None
                assert run.status in {"cancelled", "interrupted"}
                assert run.status != "running"

    asyncio.run(scenario())


def test_history_regenerate_and_cross_thread_checkpoints(
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
                first = await _create_thread(client)
                second = await _create_thread(client)
                assert (
                    await client.post(
                        f"/threads/{first['thread_id']}/runs/stream",
                        json=_payload("original"),
                    )
                ).status_code == 200
                assert (
                    await client.post(
                        f"/threads/{second['thread_id']}/runs/stream",
                        json=_payload("other"),
                    )
                ).status_code == 200
                history = await client.post(
                    f"/threads/{first['thread_id']}/history",
                    json={"limit": 10},
                )
                assert history.status_code == 200, history.text
                states = history.json()
                assert len(states) > 1
                assert states[0]["checkpoint"]["thread_id"] == first["thread_id"]
                assert states[0]["parent_checkpoint"]["checkpoint_id"]
                assert "original" in json.dumps(states[0]["values"])
                limited = await client.post(
                    f"/threads/{first['thread_id']}/history",
                    json={"limit": 1},
                )
                assert len(limited.json()) == 1
                parent = states[0]["parent_checkpoint"]
                edited = await client.post(
                    f"/threads/{first['thread_id']}/runs/stream",
                    json=_payload(
                        "edited",
                        checkpoint={
                            "checkpoint_ns": parent.get("checkpoint_ns") or "",
                            "checkpoint_id": parent["checkpoint_id"],
                        },
                    ),
                )
                assert edited.status_code == 200, edited.text
                state = (
                    await client.get(f"/threads/{first['thread_id']}/state")
                ).json()
                assert "edited" in json.dumps(state["values"])
                other_history = (
                    await client.post(
                        f"/threads/{second['thread_id']}/history",
                        json={"limit": 10},
                    )
                ).json()
                foreign = await client.post(
                    f"/threads/{first['thread_id']}/runs/stream",
                    json=_payload(
                        "nope",
                        checkpoint={
                            "thread_id": second["thread_id"],
                            "checkpoint_id": other_history[0]["checkpoint"]["checkpoint_id"],
                        },
                    ),
                )
                assert foreign.status_code == 422
                missing_thread = await client.post(
                    f"/threads/{first['thread_id']}/runs/stream",
                    json=_payload(
                        "nope",
                        checkpoint={
                            "checkpoint_id": other_history[0]["checkpoint"]["checkpoint_id"],
                        },
                    ),
                )
                assert missing_thread.status_code == 404

    asyncio.run(scenario())


def test_hitl_approve_edit_reject_and_reject_resume_when_idle(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_hitl_graph(),
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                idle = await _create_thread(client)
                refused = await client.post(
                    f"/threads/{idle['thread_id']}/runs/stream",
                    json={
                        "assistant_id": "agrihub",
                        "command": {"resume": {"decisions": [{"type": "approve"}]}},
                    },
                )
                assert refused.status_code == 409

                decisions = {
                    "approve": {"decisions": [{"type": "approve"}]},
                    "edit": {
                        "decisions": [
                            {
                                "type": "edit",
                                "edited_action": {
                                    "name": "publish",
                                    "args": {"text": "changed"},
                                },
                            }
                        ]
                    },
                    "reject": {
                        "decisions": [{"type": "reject", "message": "rejected"}]
                    },
                }
                for kind, resume in decisions.items():
                    thread = await _create_thread(client)
                    paused = await client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json=_payload("interrupt"),
                    )
                    assert paused.status_code == 200, paused.text
                    fetched = await client.get(f"/threads/{thread['thread_id']}")
                    assert fetched.json()["status"] == "interrupted"
                    state = await client.get(f"/threads/{thread['thread_id']}/state")
                    interrupts = [
                        interrupt_item
                        for task in state.json()["tasks"]
                        for interrupt_item in task["interrupts"]
                    ]
                    assert interrupts
                    assert interrupts[0]["value"]["action_requests"][0]["name"] == (
                        "publish"
                    )
                    resumed = await client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json={
                            "assistant_id": "agrihub",
                            "command": {"resume": resume},
                            "stream_mode": ["values"],
                        },
                    )
                    assert resumed.status_code == 200, resumed.text
                    finished = await client.get(f"/threads/{thread['thread_id']}/state")
                    assert f"decision:{kind}:" in json.dumps(finished.json()["values"])
                    assert (
                        await client.get(f"/threads/{thread['thread_id']}")
                    ).json()["status"] == "idle"

    asyncio.run(scenario())


def test_slow_subscriber_queue_stays_bounded(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        graph_gates = {"waiting": asyncio.Event(), "release": asyncio.Event()}
        graph_gates["release"].set()
        app = create_app(
            settings=_settings(
                postgres_database_uri,
                API_STREAM_SUBSCRIBER_QUEUE_SIZE=1,
            ),
            graph_builder=_staged_graph(graph_gates),
        )
        started = asyncio.Event()
        release = asyncio.Event()
        original = RunManager.persist_and_publish

        async def blocked(self, **kwargs):
            started.set()
            await release.wait()
            return await original(self, **kwargs)

        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = await _create_thread(client)
            manager = app.state.run_manager
            with patch.object(RunManager, "persist_and_publish", blocked):
                start = asyncio.create_task(
                    manager.start_run(
                        uuid.UUID(thread["thread_id"]),
                        __import__(
                            "agent_platform.api.schemas",
                            fromlist=["RunStreamRequest"],
                        ).RunStreamRequest.model_validate(_payload("bounded")),
                        app.state.settings.DEVELOPMENT_USER_ID,
                    )
                )
                await started.wait()
                view = await start
                subscriber = await manager._subscribe(
                    view.id,
                    cancel_on_disconnect=False,
                )
                assert subscriber is not None
                entry = manager._runs[view.id]
                release.set()
                try:
                    await entry.task
                except asyncio.CancelledError:
                    pass
            assert subscriber.lagging
            assert entry.task.cancelled() is False
            assert subscriber.queue.qsize() <= 1
            async with session_scope(app.state.session_factory) as session:
                rows = await RunEventRepository(session).replay(
                    run_id=view.id,
                    owner_user_id=app.state.settings.DEVELOPMENT_USER_ID,
                )
            assert [row.sequence for row in rows] == list(range(1, len(rows) + 1))
            assert len(rows) >= 3

    asyncio.run(scenario())


def test_sdk_contract_for_history_reconnect_cancel_and_hitl(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_sdk_graph(),
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                lifespan="on",
            )
        )
        server_task = asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.02)
            assert server.started
            script = r"""
import { Client } from "@langchain/langgraph-sdk";
const client = new Client({ apiUrl: process.env.SDK_API_URL });
const thread = await client.threads.create({
  metadata: { graph_id: "agrihub" },
});
const threads = await client.threads.search({
  metadata: { graph_id: "agrihub" },
  limit: 100,
});
const first = [];
for await (const chunk of client.runs.stream(thread.thread_id, "agrihub", {
  input: { messages: [{ type: "human", content: "hello" }] },
  streamMode: ["values"],
  streamResumable: true,
})) {
  first.push({ id: chunk.id ?? null, event: chunk.event });
}
const history = await client.threads.getHistory(thread.thread_id, { limit: 10 });
const parent = history[0]?.parent_checkpoint;
const editedEvents = [];
for await (const chunk of client.runs.stream(thread.thread_id, "agrihub", {
  input: { messages: [{ type: "human", content: "edited" }] },
  checkpoint: parent,
  streamMode: ["values"],
})) {
  editedEvents.push(chunk.event);
}
const editedState = await client.threads.getState(thread.thread_id);

const reconnectThread = await client.threads.create({
  metadata: { graph_id: "agrihub" },
});
const stream = client.runs.stream(reconnectThread.thread_id, "agrihub", {
  input: { messages: [{ type: "human", content: "reconnect" }] },
  streamMode: ["values"],
});
const iterator = stream[Symbol.asyncIterator]();
const metadataEvent = await iterator.next();
const secondEvent = await iterator.next();
if (iterator.return) await iterator.return();
const runId = metadataEvent.value.data.run_id;
const rejoined = [];
for await (const chunk of client.runs.joinStream(
  reconnectThread.thread_id,
  runId,
  { lastEventId: secondEvent.value.id },
)) {
  rejoined.push({ id: chunk.id ?? null, event: chunk.event });
}

const slowThread = await client.threads.create({
  metadata: { graph_id: "agrihub" },
});
const slow = client.runs.stream(slowThread.thread_id, "agrihub", {
  input: { messages: [{ type: "human", content: "slow" }] },
  streamMode: ["values"],
});
const slowIterator = slow[Symbol.asyncIterator]();
const slowMetadata = await slowIterator.next();
const cancelled = await client.runs.cancel(
  slowThread.thread_id,
  slowMetadata.value.data.run_id,
);
if (slowIterator.return) await slowIterator.return();

const hitlThread = await client.threads.create({
  metadata: { graph_id: "agrihub" },
});
for await (const _chunk of client.runs.stream(hitlThread.thread_id, "agrihub", {
  input: { messages: [{ type: "human", content: "interrupt" }] },
  streamMode: ["values"],
})) {}
const paused = await client.threads.getState(hitlThread.thread_id);
const interrupts = (paused.tasks ?? []).flatMap((task) => task.interrupts ?? []);
for await (const _chunk of client.runs.stream(hitlThread.thread_id, "agrihub", {
  command: { resume: { decisions: [{ type: "approve" }] } },
  streamMode: ["values"],
})) {}
const resumed = await client.threads.getState(hitlThread.thread_id);
console.log(JSON.stringify({
  found: threads.some((item) => item.thread_id === thread.thread_id),
  firstEvent: first[0]?.event ?? null,
  historyCount: history.length,
  parentCheckpoint: Boolean(parent?.checkpoint_id),
  edited: JSON.stringify(editedState.values),
  editedEvents,
  rejoinIds: rejoined.map((item) => item.id),
  cursor: secondEvent.value.id,
  cancelled: cancelled.status,
  interruptName: interrupts[0]?.value?.action_requests?.[0]?.name ?? null,
  resumed: JSON.stringify(resumed.values),
}));
"""
            completed = await asyncio.to_thread(
                subprocess.run,
                ["node", "--input-type=module", "-e", script],
                cwd=PROJECT_ROOT / "frontend",
                env={**os.environ, "SDK_API_URL": f"http://127.0.0.1:{port}"},
                check=False,
                capture_output=True,
                text=True,
            )
            assert completed.returncode == 0, completed.stderr
            result = json.loads(completed.stdout.strip().splitlines()[-1])
            assert result["found"] is True
            assert result["firstEvent"] == "metadata"
            assert result["historyCount"] > 1
            assert result["parentCheckpoint"] is True
            assert "edited" in result["edited"]
            assert "values" in result["editedEvents"]
            assert result["cursor"] not in result["rejoinIds"]
            assert result["cancelled"] == "cancelled"
            assert result["interruptName"] == "publish"
            assert "decision:approve" in result["resumed"]
        finally:
            server.should_exit = True
            await server_task

    asyncio.run(scenario())
