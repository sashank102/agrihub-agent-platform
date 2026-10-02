"""PostgreSQL-backed runs of the agrihub_study graph over the HTTP API, on the fixture bundle."""

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from agrihub_fixtures import FixtureBundle
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from psycopg import sql
from sqlalchemy import select

from agent_platform.core.settings import Settings
from agent_platform.db.models import Artifact, Run
from agent_platform.db.session import session_scope
from agent_platform.main import create_app
from agent_platform.services.retention import apply_retention
from agrihub import events, evidence_store
from agrihub.nodes import harvest
from alembic import command
from alembic.config import Config

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("fake_llm")]
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
MIXED_STUDY = {
    **STUDY,
    "snps": [
        {"raw": "S5_2899164", "chrom": "Gm05", "pos": 2_899_164},
        {"raw": "S5_2900000", "chrom": "5", "pos": 2_900_000},
        {"raw": "S18_99999999", "chrom": "18", "pos": 99_999_999},
        {"raw": "S18_9263941", "chrom": "chr18", "pos": 9_263_941},
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


@pytest.fixture
def run_dir(fixture_env: FixtureBundle, tmp_path: Path) -> Path:
    """Run studies on the fixture bundle; return the run directory root."""
    return tmp_path / "runs"


def _echo_builder(*, checkpointer: Any, store: Any) -> Any:
    async def respond(state: MessagesState) -> dict[str, Any]:
        return {"messages": [AIMessage(content=f"Echo: {state['messages'][-1].content}")]}

    builder = StateGraph(MessagesState)
    builder.add_node("respond", respond)
    builder.add_edge(START, "respond")
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=checkpointer, store=store)


def _app(database_uri: str) -> Any:
    return create_app(
        settings=Settings(ENVIRONMENT="test", DATABASE_URI=database_uri, _env_file=None),
        graph_builder=_echo_builder,
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


async def _run_study(client: AsyncClient, study: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    thread = (
        await client.post("/threads", json={"metadata": {"graph_id": "agrihub_study", "kind": "study"}})
    ).json()
    assert thread["metadata"]["graph_id"] == "agrihub_study"
    response = await client.post(
        f"/threads/{thread['thread_id']}/runs/stream",
        json={
            "assistant_id": "agrihub_study",
            "input": {"study": study},
            "stream_mode": ["values", "custom"],
            "stream_subgraphs": True,
        },
    )
    assert response.status_code == 200, response.text
    return thread, _parse_sse(response.text), response.headers["x-run-id"]


def _run_events(streamed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item["data"] for item in streamed if item["event"].split("|")[0] == "custom"]


def _store_is_open(run_dir: Path, run_id: str) -> bool:
    return (run_dir / run_id / evidence_store.DATABASE_NAME).resolve() in evidence_store._open_stores


async def _store_closes(run_dir: Path, run_id: str) -> bool:
    """Wait for the run task's terminal hook, which runs just after the terminal event is published."""
    for _ in range(100):
        if not _store_is_open(run_dir, run_id):
            return True
        await asyncio.sleep(0.02)
    return False


def test_study_run_streams_custom_events_and_stores_artifacts(postgres_database_uri: str, run_dir: Path):
    async def scenario() -> None:
        app = _app(postgres_database_uri)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                thread, streamed, run_id = await _run_study(client, STUDY)

                names = [item["event"] for item in streamed]
                assert names[-1] == "end"
                assert streamed[-1]["data"] == {"status": "success"}
                assert not any(name.startswith("values|") for name in names)
                run_events = _run_events(streamed)
                assert run_events
                assert all(event["schema"] == events.SCHEMA for event in run_events)
                lane_names = {name for name in names if name.startswith("custom|specialist:")}
                started = [event for event in run_events if event["type"] == "agent.started"]
                assert len(lane_names) == len({event["agent"]["id"] for event in started}) >= 3
                phases = [
                    (event["data"]["phase"], event["data"]["status"])
                    for event in run_events
                    if event["type"] == "run.phase"
                ]
                assert [phase for phase, status in phases if status == "started"] == [
                    "intake",
                    "loci",
                    "harvest",
                    "planning",
                    "specialists",
                    "ranking",
                    "reporting",
                ]
                assert {("intake", "completed"), ("loci", "completed"), ("harvest", "completed")} <= set(phases)
                progress: dict[str, dict[str, Any]] = {}
                for event in run_events:
                    if event["type"] == "evidence.progress":
                        progress[event["data"]["category"]] = event["data"]
                assert progress and all(item["done"] == item["total"] > 0 for item in progress.values())
                created = {
                    event["data"]["kind"]: event["data"]
                    for event in run_events
                    if event["type"] == "artifact.created"
                }
                assert [row["locus_id"] for row in created["loci_table"]["rows"]] == ["L1", "L2", "L3"]

                state = (await client.get(f"/threads/{thread['thread_id']}/state")).json()
                report = state["values"]["report"]
                assert report["species"] == "soybean"
                assert len(report["loci"]) == 3
                ranked = report["candidates"]
                assert ranked and [item["rank"] for item in ranked] == list(range(1, len(ranked) + 1))
                assert {item["tier"] for item in ranked} <= {"T1", "T2", "T3", "T4"}
                assert "Glyma.18G092200" in {item["gene_id"] for item in ranked}
                assert all(item["evidence_ids"] and item["category_points"] for item in ranked)

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
            assert str(by_kind["report"].id) == created["report"]["artifact_id"]
            assert str(by_kind["evidence_snapshot"].id) == created["evidence_snapshot"]["artifact_id"]
            snapshot = by_kind["evidence_snapshot"].content or {}
            assert len(snapshot["evidence"]) == report["evidence_count"]
            assert len(snapshot["findings"]) == report["finding_count"]
            assert (run_dir / run_id / "evidence.duckdb").exists()
            assert not _store_is_open(run_dir, run_id)

    asyncio.run(scenario())


def test_invalid_snps_and_mixed_chromosome_aliases_become_warnings(postgres_database_uri: str, run_dir: Path):
    async def scenario() -> None:
        app = _app(postgres_database_uri)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                thread, streamed, _ = await _run_study(client, MIXED_STUDY)
                assert streamed[-1]["data"] == {"status": "success"}
                intake = next(
                    event["data"]
                    for event in _run_events(streamed)
                    if event["type"] == "run.phase" and (event["data"]["phase"], event["data"]["status"]) == ("intake", "completed")
                )
                warnings = {warning["code"]: warning for warning in intake["warnings"]}
                assert set(warnings) == {"out_of_bounds", "chromosome_aliases"}
                assert warnings["out_of_bounds"]["snp"] == "S18_99999999"
                assert warnings["chromosome_aliases"]["message"] == "Gm05 was given as 5, Gm05; all were normalized to Gm05"
                assert intake["detail"] == "3 valid SNPs of 4 submitted; 2 warnings"
                state = (await client.get(f"/threads/{thread['thread_id']}/state")).json()
                report = state["values"]["report"]
                assert [locus["chrom"] for locus in report["loci"]] == ["Gm05", "Gm18"]
                assert report["loci"][0]["merged_from"] == ["S5_2899164", "S5_2900000"]
                assert {warning["code"] for warning in report["warnings"]} == {"out_of_bounds", "chromosome_aliases"}
                assert any("2 input warnings" in item for item in report["limitations"])

    asyncio.run(scenario())


def test_a_failed_study_run_closes_its_evidence_store(
    postgres_database_uri: str,
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def broken(study: dict[str, Any]) -> Any:
        raise RuntimeError("bundle exploded")

    monkeypatch.setattr(harvest, "harvest_context", broken)

    async def scenario() -> None:
        app = _app(postgres_database_uri)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                _, streamed, run_id = await _run_study(client, STUDY)
                assert streamed[-1]["event"] == "error"
                assert streamed[-1]["data"]["message"] == "Graph execution failed"
                phases = [event["data"]["phase"] for event in _run_events(streamed) if event["type"] == "run.phase"]
                assert phases[-1] == "harvest"
                assert (run_dir / run_id / "evidence.duckdb").exists()
                assert await _store_closes(run_dir, run_id)
                _, failed_intake, _ = await _run_study(
                    client, {**STUDY, "snps": [{"raw": "S99_1", "chrom": "99", "pos": 1}]}
                )
                assert failed_intake[-1]["event"] == "error"
                failure = [
                    event["data"]
                    for event in _run_events(failed_intake)
                    if event["type"] == "run.phase" and event["data"]["status"] == "failed"
                ]
                assert failure and failure[0]["phase"] == "intake"
                assert "none of the 1 SNPs could be placed" in failure[0]["detail"]
                assert failed_intake[-1]["data"]["message"] == failure[0]["detail"]
                _, malformed, _ = await _run_study(client, {**STUDY, "snps": [{"raw": "S5_1", "chrom": "5"}]})
                assert malformed[-1]["event"] == "error"
                assert malformed[-1]["data"]["message"].startswith("the study request is invalid: snps.0:")
                rejected = next(
                    event["data"]
                    for event in _run_events(malformed)
                    if event["type"] == "run.phase" and event["data"]["status"] == "failed"
                )
                assert rejected["errors"] == [
                    {"loc": ["snps", 0], "message": "Value error, chrom and pos must be given together"}
                ]
            async with session_scope(app.state.session_factory) as session:
                statuses = {str(run.id): run.status for run in (await session.scalars(select(Run))).all()}
            assert statuses[run_id] == "failed"

    asyncio.run(scenario())


def test_thread_runs_are_listed_newest_first_for_their_owner_only(postgres_database_uri: str, run_dir: Path):
    async def scenario() -> None:
        app = create_app(
            settings=Settings(
                ENVIRONMENT="test",
                DATABASE_URI=postgres_database_uri,
                AUTH_MODE="api_key",
                API_KEY_PEPPER="study-runs-pepper-0123456789",
                _env_file=None,
            ),
            graph_builder=_echo_builder,
        )
        async with app.router.lifespan_context(app):
            accounts = app.state.accounts
            owner = await accounts.create_user(display_name="Study owner")
            other = await accounts.create_user(display_name="Someone else")
            owner_key = (await accounts.issue_api_key(user_id=owner.id, label="owner")).plaintext
            other_key = (await accounts.issue_api_key(user_id=other.id, label="other")).plaintext
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"X-Api-Key": owner_key},
            ) as client:
                thread, first, first_run = await _run_study(client, STUDY)
                thread_id = thread["thread_id"]
                second = await client.post(
                    f"/threads/{thread_id}/runs/stream",
                    json={"assistant_id": "agrihub_study", "input": {"study": {**STUDY, "snps": []}}},
                )
                second_run = second.headers["x-run-id"]
                assert first[-1]["event"] == "end" and _parse_sse(second.text)[-1]["event"] == "error"

                listed = await client.get(f"/threads/{thread_id}/runs")
                assert listed.status_code == 200
                runs = listed.json()
                assert [item["run_id"] for item in runs] == [second_run, first_run]
                assert [item["status"] for item in runs] == ["failed", "completed"]
                assert set(runs[0]) == {"run_id", "status", "created_at", "finished_at"}
                assert all(item["finished_at"] for item in runs)
                assert runs[0]["created_at"] >= runs[1]["created_at"]

                foreign = await client.get(f"/threads/{thread_id}/runs", headers={"X-Api-Key": other_key})
                missing = await client.get(f"/threads/{uuid.uuid4()}/runs")
                anonymous = await client.get(f"/threads/{thread_id}/runs", headers={"X-Api-Key": ""})
                assert (foreign.status_code, foreign.json()) == (404, {"detail": "thread not found"})
                assert missing.status_code == 404
                assert anonymous.status_code == 401
                assert (await client.get("/threads/not-a-uuid/runs")).status_code == 422

    asyncio.run(scenario())


def test_retention_removes_run_directories_only_after_their_snapshot_is_exported(
    postgres_database_uri: str,
    run_dir: Path,
):
    async def scenario() -> None:
        app = _app(postgres_database_uri)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                _, _, exported = await _run_study(client, STUDY)
                chat = (await client.post("/threads", json={"metadata": {"graph_id": "agrihub"}})).json()
                response = await client.post(
                    f"/threads/{chat['thread_id']}/runs/stream",
                    json={"assistant_id": "agrihub", "input": {"messages": [{"type": "human", "content": "x"}]}},
                )
                unexported = response.headers["x-run-id"]
            (run_dir / unexported).mkdir(parents=True)
            old = datetime.now(UTC) - timedelta(days=40)
            async with session_scope(app.state.session_factory) as session:
                for run in (await session.scalars(select(Run))).all():
                    run.finished_at = old
            dry = await apply_retention(app.state.session_factory, apply=False, run_dirs_days=30, run_root=run_dir)
            assert dry.run_directories == 1 and (run_dir / exported).is_dir()
            recent = await apply_retention(app.state.session_factory, apply=True, run_dirs_days=60, run_root=run_dir)
            assert recent.run_directories == 0 and (run_dir / exported).is_dir()
            applied = await apply_retention(app.state.session_factory, apply=True, run_dirs_days=30, run_root=run_dir)
            assert applied.as_dict()["run_directories"] == 1
            assert not (run_dir / exported).exists() and (run_dir / unexported).is_dir()

    asyncio.run(scenario())
