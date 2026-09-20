"""PostgreSQL-backed tests for the minimal FastAPI Agent Protocol."""

import asyncio
import json
import os
import socket
import subprocess
import uuid
from collections.abc import Iterator
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
from psycopg import sql
from sqlalchemy import select

from agent_platform.core.settings import Settings
from agent_platform.db.models import Run
from agent_platform.db.session import session_scope
from agent_platform.main import create_app
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.postgres
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _database_uri_with_name(uri: str, database_name: str) -> str:
    parsed = urlsplit(uri)
    return urlunsplit(parsed._replace(path=f"/{database_name}"))


@pytest.fixture
def postgres_database_uri() -> Iterator[str]:
    """Create and migrate a fresh database for each API test."""
    admin_uri = os.getenv("TEST_DATABASE_URI")
    if not admin_uri:
        pytest.skip("set TEST_DATABASE_URI to run PostgreSQL integration tests")

    database_name = f"agent_platform_api_test_{uuid.uuid4().hex}"
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


def _settings(database_uri: str, *, concurrent_runs: int = 2) -> Settings:
    return Settings(
        ENVIRONMENT="test",
        DATABASE_URI=database_uri,
        API_MAX_CONCURRENT_RUNS=concurrent_runs,
        _env_file=None,
    )


def _graph_builder(
    tracker: dict[str, int] | None = None,
    *,
    delay: float = 0,
):
    def build(*, checkpointer: Any, store: Any):
        async def respond(state: MessagesState) -> dict[str, Any]:
            if tracker is not None:
                tracker["active"] += 1
                tracker["maximum"] = max(
                    tracker["maximum"],
                    tracker["active"],
                )
            try:
                if delay:
                    await asyncio.sleep(delay)
                content = str(state["messages"][-1].content)
                if content == "fail":
                    raise RuntimeError("provider secret must not escape")
                return {"messages": [AIMessage(content=f"Echo: {content}")]}
            finally:
                if tracker is not None:
                    tracker["active"] -= 1

        builder = StateGraph(MessagesState)
        builder.add_node("respond", respond)
        builder.add_edge(START, "respond")
        builder.add_edge("respond", END)
        return builder.compile(checkpointer=checkpointer, store=store)

    return build


async def _create_thread(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/threads",
        json={"metadata": {"graph_id": "agrihub", "client": "test"}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _run_payload(content: str, assistant_id: str = "agrihub") -> dict[str, Any]:
    return {
        "assistant_id": assistant_id,
        "input": {"messages": [{"type": "human", "content": content}]},
        "stream_mode": ["values"],
        "stream_resumable": True,
        "multitask_strategy": "reject",
    }


def test_lifespan_health_ready_info_and_cors(postgres_database_uri: str):
    async def scenario() -> None:
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_graph_builder(),
        )
        assert app.state.ready is False
        async with app.router.lifespan_context(app):
            assert app.state.ready is True
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                assert (await client.get("/health")).json() == {"status": "ok"}
                assert (await client.get("/ready")).json() == {"status": "ready"}
                info = (await client.get("/info")).json()
                assert info["authentication"] is False
                assert info["capabilities"]["stream_modes"] == ["values"]
                assert info["capabilities"]["resumable_streams"] is False

                preflight = await client.options(
                    "/threads",
                    headers={
                        "Origin": "http://127.0.0.1:3000",
                        "Access-Control-Request-Method": "POST",
                    },
                )
                assert preflight.status_code == 200
                assert (
                    preflight.headers["access-control-allow-origin"]
                    == "http://127.0.0.1:3000"
                )
        assert app.state.ready is False

    asyncio.run(scenario())


def test_thread_create_search_get_state_and_unknowns(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_graph_builder(),
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                created = await _create_thread(client)
                assert len(created["thread_id"]) == 36
                assert created["metadata"]["graph_id"] == "agrihub"
                assert created["metadata"]["assistant_id"]
                assert created["status"] == "idle"

                search = await client.post(
                    "/threads/search",
                    json={"metadata": {"graph_id": "agrihub"}, "limit": 100},
                )
                assert [item["thread_id"] for item in search.json()] == [
                    created["thread_id"]
                ]
                fetched = await client.get(f"/threads/{created['thread_id']}")
                assert fetched.json()["thread_id"] == created["thread_id"]
                state = await client.get(
                    f"/threads/{created['thread_id']}/state"
                )
                assert state.status_code == 200
                assert state.json()["values"] == {}
                assert state.json()["checkpoint"]["thread_id"] == created["thread_id"]

                unknown_id = str(uuid.uuid4())
                assert (await client.get(f"/threads/{unknown_id}")).status_code == 404
                unknown_agent = await client.post(
                    "/threads",
                    json={"metadata": {"graph_id": "missing"}},
                )
                assert unknown_agent.status_code == 404
                unknown_run = await client.post(
                    f"/threads/{unknown_id}/runs/stream",
                    json=_run_payload("hello"),
                )
                assert unknown_run.status_code == 404
                bad_agent_run = await client.post(
                    f"/threads/{created['thread_id']}/runs/stream",
                    json=_run_payload("hello", assistant_id="missing"),
                )
                assert bad_agent_run.status_code == 404

    asyncio.run(scenario())


def test_streamed_run_persists_completed_and_failed_status(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_graph_builder(),
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = await _create_thread(client)
                endpoint = f"/threads/{thread['thread_id']}/runs/stream"
                completed = await client.post(
                    endpoint,
                    json=_run_payload("hello"),
                )
                assert completed.status_code == 200
                assert completed.headers["content-location"].startswith(
                    f"/threads/{thread['thread_id']}/runs/"
                )
                assert "event: metadata" in completed.text
                assert "event: values" in completed.text
                assert "Echo: hello" in completed.text

                failed = await client.post(
                    endpoint,
                    json=_run_payload("fail"),
                )
                assert failed.status_code == 200
                assert "event: error" in failed.text
                assert "provider secret" not in failed.text

            async with session_scope(app.state.session_factory) as session:
                runs = list(
                    (
                        await session.scalars(
                            select(Run).order_by(Run.created_at, Run.id)
                        )
                    ).all()
                )
                assert [run.status for run in runs] == ["completed", "failed"]
                assert runs[0].finished_at is not None
                assert runs[1].error_message == "Graph execution failed"
                assert runs[1].finished_at is not None

    asyncio.run(scenario())


def test_concurrent_runs_use_configured_semaphore(postgres_database_uri: str):
    async def scenario() -> None:
        tracker = {"active": 0, "maximum": 0}
        app = create_app(
            settings=_settings(postgres_database_uri, concurrent_runs=1),
            graph_builder=_graph_builder(tracker, delay=0.1),
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                first = await _create_thread(client)
                second = await _create_thread(client)
                responses = await asyncio.gather(
                    client.post(
                        f"/threads/{first['thread_id']}/runs/stream",
                        json=_run_payload("one"),
                    ),
                    client.post(
                        f"/threads/{second['thread_id']}/runs/stream",
                        json=_run_payload("two"),
                    ),
                )
                assert [response.status_code for response in responses] == [200, 200]
                assert tracker["maximum"] == 1

    asyncio.run(scenario())


def test_state_is_restored_after_fastapi_restart(postgres_database_uri: str):
    async def scenario() -> None:
        settings = _settings(postgres_database_uri)
        first_app = create_app(
            settings=settings,
            graph_builder=_graph_builder(),
        )
        async with first_app.router.lifespan_context(first_app):
            async with AsyncClient(
                transport=ASGITransport(app=first_app),
                base_url="http://test",
            ) as client:
                thread = await _create_thread(client)
                response = await client.post(
                    f"/threads/{thread['thread_id']}/runs/stream",
                    json=_run_payload("survive"),
                )
                assert response.status_code == 200

        second_app = create_app(
            settings=settings,
            graph_builder=_graph_builder(),
        )
        async with second_app.router.lifespan_context(second_app):
            async with AsyncClient(
                transport=ASGITransport(app=second_app),
                base_url="http://test",
            ) as client:
                state = (
                    await client.get(f"/threads/{thread['thread_id']}/state")
                ).json()
                assert [message["content"] for message in state["values"]["messages"]] == [
                    "survive",
                    "Echo: survive",
                ]

    asyncio.run(scenario())


def test_javascript_sdk_contract_smoke(postgres_database_uri: str):
    async def scenario() -> None:
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_graph_builder(),
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

            script = """
import { Client } from "@langchain/langgraph-sdk";
const client = new Client({ apiUrl: process.env.SDK_API_URL });
const thread = await client.threads.create({
  metadata: { graphId: "agrihub" },
  graphId: "agrihub",
});
const events = [];
for await (const chunk of client.runs.stream(thread.thread_id, "agrihub", {
  input: { messages: [{ type: "human", content: "sdk" }] },
  streamMode: ["values"],
})) {
  events.push(chunk.event);
}
const state = await client.threads.getState(thread.thread_id);
const threads = await client.threads.search({
  metadata: { graph_id: "agrihub" },
  limit: 100,
});
console.log(JSON.stringify({
  threadId: thread.thread_id,
  events,
  message: state.values.messages.at(-1).content,
  found: threads.some((item) => item.thread_id === thread.thread_id),
}));
"""
            environment = {
                **os.environ,
                "SDK_API_URL": f"http://127.0.0.1:{port}",
            }
            completed = await asyncio.to_thread(
                subprocess.run,
                ["node", "--input-type=module", "-e", script],
                cwd=PROJECT_ROOT / "frontend",
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(completed.stdout.strip().splitlines()[-1])
            assert len(result["threadId"]) == 36
            assert result["events"][0] == "metadata"
            assert "values" in result["events"]
            assert result["message"] == "Echo: sdk"
            assert result["found"] is True
        finally:
            server.should_exit = True
            await server_task

    asyncio.run(scenario())
