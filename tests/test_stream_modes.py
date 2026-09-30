"""Stream modes, v2 part handling, payload encoding, and durable replay."""

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph
from psycopg import sql

from agent_platform.api.schemas import RunStreamRequest
from agent_platform.core.settings import Settings
from agent_platform.main import create_app
from agent_platform.services.errors import UnsupportedRunOption
from agent_platform.services.run_fields import (
    astream_options,
    validate_run_stream_request,
)
from agent_platform.services.run_manager import (
    MAX_PAYLOAD_STRING_BYTES,
    _encode_payload,
    _stream_event_name,
)
from agrihub import events
from alembic import command
from alembic.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BIG_STRING = "x" * 20_000
LIST_PAYLOAD = ["lane-list", {"count": 2}]
LANE = events.AgentRef(id="lane-1", name="Fixture lane", kind="specialist")


def _request(**overrides: Any) -> RunStreamRequest:
    payload: dict[str, Any] = {"assistant_id": "agrihub", "input": {"messages": []}}
    payload.update(overrides)
    return RunStreamRequest.model_validate(payload)


def _subgraph_builder(*, checkpointer: Any, store: Any) -> Any:
    """Root ``respond`` node plus a ``lane`` subgraph that emits run events."""

    async def work(state: MessagesState) -> dict[str, Any]:
        events.agent_started(LANE, focus={"genes": 1}, max_steps=1)
        writer = get_stream_writer()
        writer(LIST_PAYLOAD)
        writer({"big": BIG_STRING, "raw_notes": ["scratch"], "kept": "yes"})
        events.agent_completed(LANE, summary="done", findings=0, duration_ms=1)
        return {"messages": [AIMessage(content="lane done")]}

    lane_builder = StateGraph(MessagesState)
    lane_builder.add_node("work", work)
    lane_builder.add_edge(START, "work")
    lane_builder.add_edge("work", END)
    lane = lane_builder.compile()

    async def respond(state: MessagesState) -> dict[str, Any]:
        events.phase("intake")
        return {"messages": [AIMessage(content="root")]}

    builder = StateGraph(MessagesState)
    builder.add_node("respond", respond)
    builder.add_node("lane", lane)
    builder.add_edge(START, "respond")
    builder.add_edge("respond", "lane")
    builder.add_edge("lane", END)
    return builder.compile(checkpointer=checkpointer, store=store)


def test_astream_options_request_v2_values_and_custom():
    options = astream_options(_request(stream_subgraphs=True))
    assert options["stream_mode"] == ["values", "custom"]
    assert options["version"] == "v2"
    assert options["subgraphs"] is True

    with_updates = astream_options(
        _request(stream_mode=["values", "updates", "tools", "messages-tuple"])
    )
    assert with_updates["stream_mode"] == ["values", "custom", "updates"]
    assert with_updates["subgraphs"] is False


def test_sdk_only_modes_are_accepted_but_unknown_modes_are_not():
    validate_run_stream_request(
        _request(stream_mode=["values", "updates", "custom", "tools", "messages-tuple"])
    )
    with pytest.raises(UnsupportedRunOption):
        validate_run_stream_request(_request(stream_mode=["values", "debug"]))
    with pytest.raises(UnsupportedRunOption):
        validate_run_stream_request(_request(stream_mode=["custom"]))


def test_event_names_encode_the_namespace():
    assert _stream_event_name("custom", ()) == "custom"
    assert _stream_event_name("custom", ("lane:abc", "work:def")) == "custom|lane:abc|work:def"


def test_encode_payload_trims_long_strings_and_drops_raw_fields():
    encoded = _encode_payload(
        {
            "big": BIG_STRING,
            "nested": [{"raw_content": "page", "text": "é" * 9_000}],
            "raw_notes": ["scratch"],
        }
    )
    assert isinstance(encoded, dict)
    assert "raw_notes" not in encoded
    assert encoded["nested"][0] == {"text": encoded["nested"][0]["text"]}
    assert encoded["big"].startswith("x" * MAX_PAYLOAD_STRING_BYTES)
    assert encoded["big"].endswith(
        f"…[truncated {20_000 - MAX_PAYLOAD_STRING_BYTES} bytes]"
    )
    multibyte = encoded["nested"][0]["text"]
    assert "[truncated" in multibyte
    assert len(multibyte.split("…[truncated")[0].encode()) <= MAX_PAYLOAD_STRING_BYTES


def test_encode_payload_keeps_lists_and_wraps_scalars():
    assert _encode_payload(LIST_PAYLOAD) == LIST_PAYLOAD
    assert _encode_payload([]) == []
    assert _encode_payload("text") == {"value": "text"}
    assert _encode_payload({"message": HumanMessage(content="hi")})["message"]["content"] == "hi"


def test_emit_outside_a_graph_returns_the_event_without_writing():
    event = events.phase("intake")
    wire = event.wire()
    assert wire["schema"] == events.SCHEMA
    assert wire["type"] == "run.phase"
    assert wire["agent"]["id"] == events.PIPELINE.id
    assert wire["ns"] == []


def test_subgraph_custom_parts_carry_namespace_and_envelope():
    async def scenario() -> list[dict[str, Any]]:
        graph = _subgraph_builder(checkpointer=None, store=None)
        options = astream_options(_request(stream_subgraphs=True))
        options.pop("durability")
        return [
            part
            async for part in graph.astream(
                {"messages": [HumanMessage(content="go")]},
                **options,
            )
        ]

    parts = asyncio.run(scenario())
    lane_custom = [part for part in parts if part["type"] == "custom" and part["ns"]]
    assert lane_custom
    assert all(part["ns"][0].startswith("lane:") for part in lane_custom)
    envelopes = [part["data"] for part in lane_custom if isinstance(part["data"], dict)]
    started = [item for item in envelopes if item.get("type") == "agent.started"]
    assert started and started[0]["schema"] == events.SCHEMA
    assert started[0]["ns"][0].startswith("lane:")
    assert started[0]["agent"]["id"] == LANE.id


def _database_uri_with_name(uri: str, database_name: str) -> str:
    parsed = urlsplit(uri)
    return urlunsplit(parsed._replace(path=f"/{database_name}"))


@pytest.fixture
def postgres_database_uri() -> Iterator[str]:
    """Create and migrate a fresh database for each streaming test."""
    admin_uri = os.getenv("TEST_DATABASE_URI")
    if not admin_uri:
        pytest.skip("set TEST_DATABASE_URI to run PostgreSQL integration tests")

    database_name = f"agent_platform_stream_test_{uuid.uuid4().hex}"
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


def _parse_sse(raw: str) -> list[dict[str, Any]]:
    parsed = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        item: dict[str, Any] = {"id": None, "event": None, "data": None}
        for line in block.splitlines():
            if line.startswith("id:"):
                item["id"] = int(line.split(":", 1)[1].strip())
            elif line.startswith("event:"):
                item["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                item["data"] = json.loads(line.split(":", 1)[1].strip())
        parsed.append(item)
    return parsed


@pytest.mark.postgres
def test_custom_events_stream_namespaced_and_replay_identically(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        app = create_app(
            settings=Settings(
                ENVIRONMENT="test",
                DATABASE_URI=postgres_database_uri,
                _env_file=None,
            ),
            graph_builder=_subgraph_builder,
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = (
                    await client.post("/threads", json={"metadata": {"graph_id": "agrihub"}})
                ).json()
                response = await client.post(
                    f"/threads/{thread['thread_id']}/runs/stream",
                    json={
                        "assistant_id": "agrihub",
                        "input": {"messages": [{"type": "human", "content": "go"}]},
                        "stream_mode": ["values", "updates", "custom", "messages-tuple"],
                        "stream_subgraphs": True,
                    },
                )
                assert response.status_code == 200, response.text
                live = _parse_sse(response.text)

                names = [item["event"] for item in live]
                assert [item["id"] for item in live] == list(range(1, len(live) + 1))
                assert names[0] == "metadata"
                assert names[-1] == "end"
                assert not any(name.startswith("values|") for name in names)
                assert not any(name.startswith("updates|") for name in names)
                assert "updates" in names

                root_custom = [item for item in live if item["event"] == "custom"]
                assert root_custom[0]["data"]["type"] == "run.phase"
                lane_custom = [item for item in live if item["event"].startswith("custom|lane:")]
                envelopes = [
                    item["data"]
                    for item in lane_custom
                    if isinstance(item["data"], dict) and "schema" in item["data"]
                ]
                assert [item["type"] for item in envelopes] == [
                    "agent.started",
                    "agent.completed",
                ]
                assert all(item["schema"] == events.SCHEMA for item in envelopes)
                assert LIST_PAYLOAD in [item["data"] for item in lane_custom]
                trimmed = next(
                    item["data"]
                    for item in lane_custom
                    if isinstance(item["data"], dict) and "big" in item["data"]
                )
                assert "raw_notes" not in trimmed
                assert trimmed["kept"] == "yes"
                assert trimmed["big"].endswith("bytes]")
                assert len(trimmed["big"]) < MAX_PAYLOAD_STRING_BYTES + 64

                root_values = [item["data"] for item in live if item["event"] == "values"]
                assert [message["content"] for message in root_values[-1]["messages"]] == [
                    "go",
                    "root",
                    "lane done",
                ]

                replay = await client.get(
                    f"/threads/{thread['thread_id']}/runs/{response.headers['x-run-id']}/stream",
                    headers={"Last-Event-ID": "0"},
                )
                assert replay.status_code == 200
                assert _parse_sse(replay.text) == live

    asyncio.run(scenario())
