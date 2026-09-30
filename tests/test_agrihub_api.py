"""PostgreSQL-backed run of the agrihub_study graph over the HTTP API."""

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
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from psycopg import sql
from sqlalchemy import select

from agent_platform.core.settings import Settings
from agent_platform.db.models import Artifact
from agent_platform.db.session import session_scope
from agent_platform.main import create_app
from agrihub import events
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.postgres
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STUDY = {
    "mode": "snps",
    "species": "soybean",
    "assembly": "Wm82.a2.v1",
    "trait_text": "plant height",
    "snps": [
        {"raw": "S5_2899164", "chrom": "5", "pos": 2_899_164},
        {"raw": "S18_9263941", "chrom": "18", "pos": 9_263_941},
        {"raw": "S18_51620945", "chrom": "18", "pos": 51_620_945},
    ],
}


def _database_uri_with_name(uri: str, database_name: str) -> str:
    parsed = urlsplit(uri)
    return urlunsplit(parsed._replace(path=f"/{database_name}"))


@pytest.fixture
def postgres_database_uri() -> Iterator[str]:
    """Create and migrate a fresh database for each study API test."""
    admin_uri = os.getenv("TEST_DATABASE_URI")
    if not admin_uri:
        pytest.skip("set TEST_DATABASE_URI to run PostgreSQL integration tests")

    database_name = f"agent_platform_study_test_{uuid.uuid4().hex}"
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


def _echo_builder(*, checkpointer: Any, store: Any) -> Any:
    async def respond(state: MessagesState) -> dict[str, Any]:
        return {"messages": [AIMessage(content=f"Echo: {state['messages'][-1].content}")]}

    builder = StateGraph(MessagesState)
    builder.add_node("respond", respond)
    builder.add_edge(START, "respond")
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=checkpointer, store=store)


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


def test_study_run_streams_custom_events_and_stores_artifacts(
    postgres_database_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path))

    async def scenario() -> None:
        app = create_app(
            settings=Settings(
                ENVIRONMENT="test",
                DATABASE_URI=postgres_database_uri,
                _env_file=None,
            ),
            graph_builder=_echo_builder,
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = (
                    await client.post(
                        "/threads",
                        json={"metadata": {"graph_id": "agrihub_study", "kind": "study"}},
                    )
                ).json()
                assert thread["metadata"]["graph_id"] == "agrihub_study"
                response = await client.post(
                    f"/threads/{thread['thread_id']}/runs/stream",
                    json={
                        "assistant_id": "agrihub_study",
                        "input": {"study": STUDY},
                        "stream_mode": ["values", "custom"],
                        "stream_subgraphs": True,
                    },
                )
                assert response.status_code == 200, response.text
                streamed = _parse_sse(response.text)
                run_id = response.headers["x-run-id"]

                names = [item["event"] for item in streamed]
                assert names[-1] == "end"
                assert streamed[-1]["data"] == {"status": "success"}
                assert not any(name.startswith("values|") for name in names)
                run_events = [
                    item["data"]
                    for item in streamed
                    if item["event"].split("|")[0] == "custom"
                ]
                assert run_events
                assert all(event["schema"] == events.SCHEMA for event in run_events)
                lane_names = {name for name in names if name.startswith("custom|specialist:")}
                assert len(lane_names) == 5
                started = [event for event in run_events if event["type"] == "agent.started"]
                assert len({event["agent"]["id"] for event in started}) == 5
                phases = [
                    event["data"]["phase"]
                    for event in run_events
                    if event["type"] == "run.phase" and event["data"]["status"] == "started"
                ]
                assert phases == [
                    "intake",
                    "loci",
                    "harvest",
                    "planning",
                    "specialists",
                    "ranking",
                    "reporting",
                ]
                created = {
                    event["data"]["kind"]: event["data"]["artifact_id"]
                    for event in run_events
                    if event["type"] == "artifact.created"
                }

                state = (await client.get(f"/threads/{thread['thread_id']}/state")).json()
                report = state["values"]["report"]
                assert report["species"] == "soybean"
                assert len(report["loci"]) == 3

                species = await client.get("/registry/species")
                assert species.status_code == 200
                assert [item["species"] for item in species.json()] == [
                    "maize",
                    "rice",
                    "sorghum",
                    "soybean",
                ]

                chat = (
                    await client.post("/threads", json={"metadata": {"graph_id": "agrihub"}})
                ).json()
                echoed = await client.post(
                    f"/threads/{chat['thread_id']}/runs/stream",
                    json={
                        "assistant_id": "agrihub",
                        "input": {"messages": [{"type": "human", "content": "hello"}]},
                        "stream_mode": ["values"],
                    },
                )
                assert "Echo: hello" in echoed.text

            async with session_scope(app.state.session_factory) as session:
                artifacts = list(
                    (
                        await session.scalars(
                            select(Artifact).where(Artifact.run_id == uuid.UUID(run_id))
                        )
                    ).all()
                )
            by_kind = {artifact.kind: artifact for artifact in artifacts}
            assert set(by_kind) == {"report", "evidence_snapshot"}
            assert str(by_kind["report"].id) == created["report"]
            assert str(by_kind["evidence_snapshot"].id) == created["evidence_snapshot"]
            snapshot = by_kind["evidence_snapshot"].content or {}
            assert len(snapshot["evidence"]) == report["evidence_count"]
            assert len(snapshot["findings"]) == report["finding_count"]
            assert (tmp_path / run_id / "evidence.duckdb").exists()

    asyncio.run(scenario())
