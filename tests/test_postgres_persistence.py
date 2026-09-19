"""Integration tests for LangGraph's PostgreSQL-owned persistence tables."""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.config import get_store
from langgraph.graph import END, START, MessagesState, StateGraph
from psycopg import sql

from agent_platform.persistence import open_postgres_persistence
from agent_platform.runtime import open_persistent_graph

LANGGRAPH_TABLES = {
    "checkpoint_blobs",
    "checkpoint_migrations",
    "checkpoint_writes",
    "checkpoints",
    "store",
    "store_migrations",
}
pytestmark = pytest.mark.postgres


class DurableConversationState(MessagesState):
    """Representative complete state persisted by the production graph."""

    supervisor_state: dict[str, Any]
    researcher_state: dict[str, Any]
    notes: list[str]
    final_report: str
    store_value: str


def _database_uri_with_name(uri: str, database_name: str) -> str:
    parsed = urlsplit(uri)
    return urlunsplit(parsed._replace(path=f"/{database_name}"))


@pytest.fixture
def postgres_database_uri() -> Iterator[str]:
    """Create a new empty database for each integration test."""
    admin_uri = os.getenv("TEST_DATABASE_URI")
    if not admin_uri:
        pytest.skip("set TEST_DATABASE_URI to run PostgreSQL integration tests")

    database_name = f"agent_platform_test_{uuid.uuid4().hex}"
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


def _checkpoint(state: dict[str, Any], version: int = 1):
    checkpoint = empty_checkpoint()
    versions = {channel: str(version) for channel in state}
    checkpoint["channel_values"] = state
    checkpoint["channel_versions"] = versions
    return checkpoint, versions


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


async def _list_tables(database_uri: str) -> set[str]:
    connection = await psycopg.AsyncConnection.connect(database_uri)
    try:
        rows = await (
            await connection.execute(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                """
            )
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        await connection.close()


def test_setup_creates_only_langgraph_tables_and_is_idempotent(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        assert await _list_tables(postgres_database_uri) == set()

        async with open_postgres_persistence(postgres_database_uri):
            pass
        async with open_postgres_persistence(postgres_database_uri):
            pass

        assert await _list_tables(postgres_database_uri) == LANGGRAPH_TABLES

    asyncio.run(scenario())


def test_checkpoint_write_read_round_trip(postgres_database_uri: str):
    async def scenario() -> None:
        thread_id = str(uuid.uuid4())
        human = HumanMessage(content="Investigate drought resistance.")
        assistant = AIMessage(
            content="I will inspect the evidence.",
            tool_calls=[
                {
                    "name": "search",
                    "args": {"query": "drought resistance genes"},
                    "id": "search-1",
                    "type": "tool_call",
                }
            ],
        )
        tool = ToolMessage(
            content="Candidate evidence",
            name="search",
            tool_call_id="search-1",
        )
        state = {
            "messages": [human, assistant, tool],
            "supervisor_state": {"research_iterations": 2},
            "researcher_state": {"raw_notes": ["Candidate evidence"]},
            "notes": ["Durable note"],
            "final_report": "Durable report",
        }
        checkpoint, versions = _checkpoint(state)

        async with open_postgres_persistence(
            postgres_database_uri
        ) as persistence:
            await persistence.checkpointer.aput(
                _config(thread_id),
                checkpoint,
                {"source": "loop", "step": 1, "parents": {}},
                versions,
            )
            restored = await persistence.checkpointer.aget_tuple(
                _config(thread_id)
            )

        assert restored is not None
        values = restored.checkpoint["channel_values"]
        assert values["messages"] == [human, assistant, tool]
        assert values["supervisor_state"]["research_iterations"] == 2
        assert values["researcher_state"]["raw_notes"] == [
            "Candidate evidence"
        ]
        assert values["notes"] == ["Durable note"]
        assert values["final_report"] == "Durable report"

    asyncio.run(scenario())


def test_store_write_read_round_trip_after_restart(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        namespace = ("threads", str(uuid.uuid4()))
        async with open_postgres_persistence(
            postgres_database_uri
        ) as persistence:
            await persistence.store.aput(
                namespace,
                "research-memory",
                {"fact": "Store data survives closed resources."},
            )

        async with open_postgres_persistence(
            postgres_database_uri
        ) as persistence:
            item = await persistence.store.aget(
                namespace,
                "research-memory",
            )

        assert item is not None
        assert item.value["fact"] == "Store data survives closed resources."

    asyncio.run(scenario())


def test_production_graph_compiles_with_postgres_dependencies(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        async with open_persistent_graph(postgres_database_uri) as graph:
            assert graph.checkpointer is not None
            assert graph.store is not None
            assert "final_report_generation" in graph.get_graph().nodes

    asyncio.run(scenario())


def _conversation_graph(checkpointer, store):
    async def record_turn(
        state: DurableConversationState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        turn = len(
            [
                message
                for message in state["messages"]
                if isinstance(message, HumanMessage)
            ]
        )
        memory_store = get_store()
        namespace = ("thread-memory", thread_id)
        existing = await memory_store.aget(namespace, "research")
        store_value = (
            existing.value["fact"]
            if existing is not None
            else "Memory written through get_store()."
        )
        await memory_store.aput(
            namespace,
            "research",
            {"fact": store_value, "last_turn": turn},
        )
        tool_call_id = f"search-{turn}"
        return {
            "messages": [
                AIMessage(
                    content=f"Assistant turn {turn}",
                    tool_calls=[
                        {
                            "name": "search",
                            "args": {"turn": turn},
                            "id": tool_call_id,
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(
                    content=f"Tool result {turn}",
                    name="search",
                    tool_call_id=tool_call_id,
                ),
            ],
            "supervisor_state": {
                "research_iterations": turn,
                "status": "complete",
            },
            "researcher_state": {
                "tool_call_iterations": turn,
                "raw_notes": [f"Raw note {turn}"],
            },
            "notes": [f"Note {turn}"],
            "final_report": f"Final report {turn}",
            "store_value": store_value,
        }

    builder = StateGraph(DurableConversationState)
    builder.add_node("record_turn", record_turn)
    builder.add_edge(START, "record_turn")
    builder.add_edge("record_turn", END)
    return builder.compile(checkpointer=checkpointer, store=store)


def test_complete_multiturn_state_history_and_store_survive_restart(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        thread_id = str(uuid.uuid4())
        config = _config(thread_id)

        async with open_postgres_persistence(
            postgres_database_uri
        ) as persistence:
            graph = _conversation_graph(
                persistence.checkpointer,
                persistence.store,
            )
            await graph.ainvoke(
                {"messages": [HumanMessage(content="First question")]},
                config,
            )
            await graph.ainvoke(
                {"messages": [HumanMessage(content="Follow-up question")]},
                config,
            )

        async with open_postgres_persistence(
            postgres_database_uri
        ) as persistence:
            graph = _conversation_graph(
                persistence.checkpointer,
                persistence.store,
            )
            snapshot = await graph.aget_state(config)
            history = [
                item async for item in graph.aget_state_history(config)
            ]
            stored = await persistence.store.aget(
                ("thread-memory", thread_id),
                "research",
            )

        messages = snapshot.values["messages"]
        assert [message.content for message in messages] == [
            "First question",
            "Assistant turn 1",
            "Tool result 1",
            "Follow-up question",
            "Assistant turn 2",
            "Tool result 2",
        ]
        assert isinstance(messages[1], AIMessage)
        assert messages[1].tool_calls[0]["name"] == "search"
        assert isinstance(messages[2], ToolMessage)
        assert messages[2].tool_call_id == "search-1"
        assert snapshot.values["supervisor_state"] == {
            "research_iterations": 2,
            "status": "complete",
        }
        assert snapshot.values["researcher_state"]["raw_notes"] == [
            "Raw note 2"
        ]
        assert snapshot.values["notes"] == ["Note 2"]
        assert snapshot.values["final_report"] == "Final report 2"
        assert len(history) >= 4
        assert len({item.config["configurable"]["checkpoint_id"] for item in history}) == len(
            history
        )
        assert stored is not None
        assert stored.value == {
            "fact": "Memory written through get_store().",
            "last_turn": 2,
        }

    asyncio.run(scenario())


def test_checkpoint_history_and_thread_ids_are_isolated(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        first_thread = str(uuid.uuid4())
        second_thread = str(uuid.uuid4())

        async with open_postgres_persistence(
            postgres_database_uri
        ) as persistence:
            first_config = _config(first_thread)
            for step in (1, 2):
                checkpoint, versions = _checkpoint(
                    {"final_report": f"First thread, step {step}"},
                    version=step,
                )
                first_config = await persistence.checkpointer.aput(
                    first_config,
                    checkpoint,
                    {"source": "loop", "step": step, "parents": {}},
                    versions,
                )

            checkpoint, versions = _checkpoint(
                {"final_report": "Second thread only"},
            )
            await persistence.checkpointer.aput(
                _config(second_thread),
                checkpoint,
                {"source": "loop", "step": 1, "parents": {}},
                versions,
            )

        async with open_postgres_persistence(
            postgres_database_uri
        ) as persistence:
            first_history = [
                item
                async for item in persistence.checkpointer.alist(
                    _config(first_thread)
                )
            ]
            second_history = [
                item
                async for item in persistence.checkpointer.alist(
                    _config(second_thread)
                )
            ]

        assert len(first_history) == 2
        assert len(second_history) == 1
        assert {
            item.checkpoint["channel_values"]["final_report"]
            for item in first_history
        } == {"First thread, step 1", "First thread, step 2"}
        assert second_history[0].checkpoint["channel_values"][
            "final_report"
        ] == "Second thread only"

    asyncio.run(scenario())


def test_state_survives_separate_writer_and_reader_processes(
    postgres_database_uri: str,
):
    thread_id = str(uuid.uuid4())
    common = """
import asyncio
import json
import os
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from agent_platform.persistence import open_postgres_persistence

uri = os.environ["PROCESS_TEST_DATABASE_URI"]
thread_id = os.environ["PROCESS_TEST_THREAD_ID"]
config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
"""
    writer = (
        common
        + """
async def main():
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(content="Cross-process question"),
            AIMessage(
                content="Cross-process answer",
                tool_calls=[{
                    "name": "search",
                    "args": {"query": "evidence"},
                    "id": "cross-process-tool",
                    "type": "tool_call",
                }],
            ),
            ToolMessage(
                content="Cross-process tool result",
                name="search",
                tool_call_id="cross-process-tool",
            ),
        ],
        "supervisor_state": {"status": "complete"},
        "researcher_state": {"raw_notes": ["Cross-process note"]},
        "notes": ["Cross-process note"],
        "final_report": "Cross-process report",
    }
    versions = {key: "1" for key in checkpoint["channel_values"]}
    checkpoint["channel_versions"] = versions
    async with open_postgres_persistence(uri) as persistence:
        await persistence.checkpointer.aput(
            config,
            checkpoint,
            {"source": "loop", "step": 1, "parents": {}},
            versions,
        )
        await persistence.store.aput(
            ("process-test", thread_id),
            "memory",
            {"value": "Cross-process store value"},
        )

asyncio.run(main())
"""
    )
    reader = (
        common
        + """
async def main():
    async with open_postgres_persistence(uri) as persistence:
        restored = await persistence.checkpointer.aget_tuple(config)
        stored = await persistence.store.aget(
            ("process-test", thread_id),
            "memory",
        )
        history = [
            item async for item in persistence.checkpointer.alist(config)
        ]
    values = restored.checkpoint["channel_values"]
    print(json.dumps({
        "messages": [message.content for message in values["messages"]],
        "tool_call": values["messages"][1].tool_calls[0]["name"],
        "tool_result_id": values["messages"][2].tool_call_id,
        "supervisor_state": values["supervisor_state"],
        "researcher_state": values["researcher_state"],
        "notes": values["notes"],
        "final_report": values["final_report"],
        "store_value": stored.value["value"],
        "history_count": len(history),
    }))

asyncio.run(main())
"""
    )
    environment = {
        **os.environ,
        "PROCESS_TEST_DATABASE_URI": postgres_database_uri,
        "PROCESS_TEST_THREAD_ID": thread_id,
    }

    subprocess.run(
        [sys.executable, "-c", writer],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    completed = subprocess.run(
        [sys.executable, "-c", reader],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    restored = json.loads(completed.stdout.strip().splitlines()[-1])

    assert restored == {
        "messages": [
            "Cross-process question",
            "Cross-process answer",
            "Cross-process tool result",
        ],
        "tool_call": "search",
        "tool_result_id": "cross-process-tool",
        "supervisor_state": {"status": "complete"},
        "researcher_state": {"raw_notes": ["Cross-process note"]},
        "notes": ["Cross-process note"],
        "final_report": "Cross-process report",
        "store_value": "Cross-process store value",
        "history_count": 1,
    }
