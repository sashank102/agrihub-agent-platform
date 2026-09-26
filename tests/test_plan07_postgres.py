"""Plan 07 remediation checks that need PostgreSQL."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from sqlalchemy import select

from agent_platform.db.models import ApiKey, Run, RunEvent
from agent_platform.db.repositories import RunEventRepository, RunRepository
from agent_platform.db.repositories.runs import RunRepository as RunRepositoryClass
from agent_platform.db.session import session_scope
from agent_platform.main import create_app
from agent_platform.services.accounts import AccountService
from agent_platform.services.run_manager import RunManager
from tests.test_durable_runs import (
    _create_thread,
    _echo_graph,
    _payload,
    _settings,
)
from tests.test_plan06_postgres import _auth_settings

pytest_plugins = ["tests.test_durable_runs"]
pytestmark = pytest.mark.postgres
PEPPER = "plan07-pepper-value"


def _counting_graph(calls: dict[str, int]):
    def build(*, checkpointer: Any, store: Any):
        async def respond(state: MessagesState) -> dict[str, Any]:
            calls["count"] += 1
            content = str(state["messages"][-1].content)
            return {"messages": [AIMessage(content=f"Echo: {content}")]}

        builder = StateGraph(MessagesState)
        builder.add_node("respond", respond)
        builder.add_edge(START, "respond")
        builder.add_edge("respond", END)
        return builder.compile(checkpointer=checkpointer, store=store)

    return build


def test_restart_after_terminal_write_failure_keeps_completed_checkpoint(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        settings = _settings(postgres_database_uri)
        app = create_app(settings=settings, graph_builder=_echo_graph())
        original = RunRepositoryClass.set_status_for_owner

        async def fail_completed(self, run_id, owner_user_id, status, **kwargs):
            if status == "completed":
                raise RuntimeError("metadata store unavailable")
            return await original(self, run_id, owner_user_id, status, **kwargs)

        with patch.object(RunRepositoryClass, "set_status_for_owner", fail_completed):
            async with app.router.lifespan_context(app):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                ) as client:
                    thread = await _create_thread(client)
                    response = await client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json=_payload("hello"),
                    )
                    assert response.status_code == 200
                    assert "Echo: hello" in response.text
                async with session_scope(app.state.session_factory) as session:
                    run = await session.scalar(select(Run))
                    assert run is not None
                    assert run.status in {"pending", "running"}
                    thread_id = run.thread_id

        restarted = create_app(settings=settings, graph_builder=_echo_graph())
        async with restarted.router.lifespan_context(restarted):
            async with session_scope(restarted.state.session_factory) as session:
                run = await session.scalar(select(Run))
                assert run is not None
                assert run.status == "completed"
                assert run.status != "interrupted"
                events = list(
                    (
                        await session.scalars(
                            select(RunEvent)
                            .where(RunEvent.run_id == run.id)
                            .order_by(RunEvent.sequence)
                        )
                    ).all()
                )
            terminal = [event for event in events if event.event_type in {"end", "error"}]
            assert len(terminal) == 1
            assert terminal[0].payload["status"] == "success"
            async with AsyncClient(
                transport=ASGITransport(app=restarted),
                base_url="http://test",
            ) as client:
                state = await client.get(f"/threads/{thread_id}/state")
                assert state.status_code == 200
                assert "Echo: hello" in state.text

    asyncio.run(scenario())


def test_cancel_before_registration_and_before_graph_invocation(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        calls = {"count": 0}
        settings = _settings(postgres_database_uri)
        app = create_app(
            settings=settings,
            graph_builder=_counting_graph(calls),
        )
        owner = settings.DEVELOPMENT_USER_ID

        async def cancel_before_attach(self, run_id):
            async with session_scope(self.session_factory) as session:
                await RunRepository(session).request_cancellation_for_owner(
                    run_id,
                    owner,
                )

        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = await _create_thread(client)
                with patch.object(RunManager, "_before_task_attach", cancel_before_attach):
                    early = await client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json=_payload("early"),
                    )
                assert early.status_code == 200, early.text
                assert "Echo: early" not in early.text
                assert calls["count"] == 0

                async def cancel_before_graph(self, run_id):
                    async with session_scope(self.session_factory) as session:
                        await RunRepository(session).request_cancellation_for_owner(
                            run_id,
                            owner,
                        )

                with patch.object(
                    RunManager,
                    "_before_graph_execution",
                    cancel_before_graph,
                ):
                    later = await client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json=_payload("later"),
                    )
                assert later.status_code == 200, later.text
                assert "Echo: later" not in later.text
                assert calls["count"] == 0
            async with session_scope(app.state.session_factory) as session:
                runs = list((await session.scalars(select(Run))).all())
            assert runs
            assert {run.status for run in runs} == {"cancelled"}
            assert all(run.status != "running" for run in runs)

    asyncio.run(scenario())


def test_missing_terminal_event_is_repaired_once(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        settings = _settings(postgres_database_uri)
        app = create_app(settings=settings, graph_builder=_echo_graph())
        original = RunEventRepository.append_terminal_if_absent

        async def fail_terminal(self, **kwargs):
            raise RuntimeError("event store unavailable")

        with patch.object(
            RunEventRepository,
            "append_terminal_if_absent",
            fail_terminal,
        ):
            async with app.router.lifespan_context(app):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                ) as client:
                    thread = await _create_thread(client)
                    response = await client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json=_payload("repair"),
                    )
                    assert response.status_code == 200
                    assert "Echo: repair" in response.text
                async with session_scope(app.state.session_factory) as session:
                    run = await session.scalar(select(Run))
                    assert run is not None
                    assert run.status == "completed"
                    run_id = run.id
                    events = list(
                        (
                            await session.scalars(
                                select(RunEvent).where(RunEvent.run_id == run_id)
                            )
                        ).all()
                    )
                    assert all(event.event_type not in {"end", "error"} for event in events)

        restored = create_app(settings=settings, graph_builder=_echo_graph())
        async with restored.router.lifespan_context(restored):
            async with session_scope(restored.state.session_factory) as session:
                events = list(
                    (
                        await session.scalars(
                            select(RunEvent)
                            .where(RunEvent.run_id == run_id)
                            .order_by(RunEvent.sequence)
                        )
                    ).all()
                )
            terminal = [event for event in events if event.event_type in {"end", "error"}]
            assert len(terminal) == 1
            assert [event.sequence for event in events] == list(range(1, len(events) + 1))
            again = await restored.state.run_manager.repair_terminal_events()
            assert again == 0
            async with session_scope(restored.state.session_factory) as session:
                events = list(
                    (
                        await session.scalars(
                            select(RunEvent).where(RunEvent.run_id == run_id)
                        )
                    ).all()
                )
            assert (
                len([event for event in events if event.event_type in {"end", "error"}])
                == 1
            )
            assert original is not fail_terminal

    asyncio.run(scenario())


def test_failed_reconciliation_rejects_a_second_active_run(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_echo_graph(),
        )
        original_owner = RunRepositoryClass.set_status_for_owner
        original_internal = RunRepositoryClass.set_status_internal

        async def fail_completed(self, run_id, owner_user_id, status, **kwargs):
            if status == "completed":
                raise RuntimeError("metadata store unavailable")
            return await original_owner(self, run_id, owner_user_id, status, **kwargs)

        async def fail_internal(self, run_id, status, **kwargs):
            if status == "completed":
                raise RuntimeError("metadata store unavailable")
            return await original_internal(self, run_id, status, **kwargs)

        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = await _create_thread(client)
                with (
                    patch.object(RunRepositoryClass, "set_status_for_owner", fail_completed),
                    patch.object(RunRepositoryClass, "set_status_internal", fail_internal),
                ):
                    first = await client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json=_payload("one"),
                    )
                    assert first.status_code == 200
                    second = await client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json=_payload("two"),
                    )
                    assert second.status_code == 409, second.text
                    body = second.json()["detail"]
                    assert body["message"] == "thread already has an active run"
                    assert body["status"] in {"pending", "running"}
                    assert body["reconciliation_intent"] == "completed"
                    assert body["graph_succeeded"] is True
                async with session_scope(app.state.session_factory) as session:
                    runs = list((await session.scalars(select(Run))).all())
                assert len(runs) == 1
                assert runs[0].status in {"pending", "running"}
                assert runs[0].error_details["reconciliation"]["intent"] == "completed"
                assert runs[0].error_details["reconciliation"]["graph_succeeded"] is True
                recovered = await client.post(
                    f"/threads/{thread['thread_id']}/runs/stream",
                    json=_payload("two"),
                )
                assert recovered.status_code == 200, recovered.text
                assert "Echo: two" in recovered.text
            async with session_scope(app.state.session_factory) as session:
                runs = list((await session.scalars(select(Run))).all())
            assert len([run for run in runs if run.status in {"pending", "running"}]) == 0
            assert {run.status for run in runs} == {"completed"}

    asyncio.run(scenario())


def test_revoke_and_deleted_user_are_rejected_over_http(
    postgres_database_uri: str,
):
    async def scenario() -> None:
        app = create_app(
            settings=_auth_settings(postgres_database_uri),
            graph_builder=_echo_graph(),
        )
        async with app.router.lifespan_context(app):
            accounts: AccountService = app.state.accounts
            revoked_user = await accounts.create_user(display_name="Revoked")
            deleted_user = await accounts.create_user(display_name="Deleted")
            revoked_key = await accounts.issue_api_key(user_id=revoked_user.id)
            deleted_key = await accounts.issue_api_key(user_id=deleted_user.id)
            await accounts.revoke_api_key(revoked_key.id)
            await accounts.delete_user(deleted_user.id)
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                revoked = await client.post(
                    "/threads/search",
                    json={"limit": 1},
                    headers={"X-Api-Key": revoked_key.plaintext},
                )
                deleted = await client.post(
                    "/threads/search",
                    json={"limit": 1},
                    headers={"X-Api-Key": deleted_key.plaintext},
                )
            assert revoked.status_code == 401
            assert deleted.status_code == 401
            assert revoked_key.plaintext not in revoked.text
            assert deleted_key.plaintext not in deleted.text

    asyncio.run(scenario())


def test_key_rotation_expiry_and_configured_prefix(postgres_database_uri: str):
    async def scenario() -> None:
        app = create_app(
            settings=_auth_settings(postgres_database_uri, API_KEY_PREFIX="crop"),
            graph_builder=_echo_graph(),
        )
        async with app.router.lifespan_context(app):
            accounts: AccountService = app.state.accounts
            user = await accounts.create_user(display_name="Rotate")
            active = await accounts.issue_api_key(
                user_id=user.id,
                expires_at=datetime.now(UTC) + timedelta(days=4),
            )
            expired = await accounts.issue_api_key(
                user_id=user.id,
                expires_at=datetime.now(UTC) - timedelta(minutes=5),
            )
            revoked = await accounts.issue_api_key(user_id=user.id)
            await accounts.revoke_api_key(revoked.id)
            assert active.plaintext.startswith("crop_")
            rotated_active = await accounts.rotate_api_key(active.id)
            rotated_expired = await accounts.rotate_api_key(expired.id)
            explicit = datetime.now(UTC) + timedelta(days=2)
            rotated_revoked = await accounts.rotate_api_key(
                revoked.id,
                expires_at=explicit,
                expires_at_set=True,
            )
            async with session_scope(app.state.session_factory) as session:
                active_row = await session.get(ApiKey, rotated_active.id)
                expired_row = await session.get(ApiKey, rotated_expired.id)
                revoked_row = await session.get(ApiKey, rotated_revoked.id)
                assert active_row is not None and active_row.expires_at is not None
                assert expired_row is not None and expired_row.expires_at is None
                assert revoked_row is not None and revoked_row.expires_at is not None
                stored = json.dumps(
                    {
                        "hash": revoked_row.secret_hash,
                        "prefix": revoked_row.key_prefix,
                    }
                )
                assert rotated_revoked.plaintext not in stored
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                accepted = await client.post(
                    "/threads/search",
                    json={"limit": 1},
                    headers={"X-Api-Key": rotated_expired.plaintext},
                )
                rejected = await client.post(
                    "/threads/search",
                    json={"limit": 1},
                    headers={"X-Api-Key": expired.plaintext},
                )
            assert accepted.status_code == 200
            assert rejected.status_code == 401

    asyncio.run(scenario())


def test_cli_rotates_expired_active_and_revoked_keys(postgres_database_uri: str):
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URI": postgres_database_uri,
            "ENVIRONMENT": "test",
            "AUTH_MODE": "api_key",
            "API_KEY_PEPPER": PEPPER,
            "API_KEY_PREFIX": "crop",
        }
    )

    def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "agent_platform", *args],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    created = run_cli("create-user", "--display-name", "Rotation")
    assert created.returncode == 0, created.stderr
    user_id = json.loads(created.stdout)["user_id"]
    issued = run_cli(
        "issue-key",
        "--user-id",
        user_id,
        "--expires-in-days",
        "1",
    )
    assert issued.returncode == 0, issued.stderr
    issued_payload = json.loads(issued.stdout)
    assert issued_payload["api_key"].startswith("crop_")
    assert issued_payload["api_key"] not in issued.stderr
    rotated = run_cli(
        "rotate-key",
        "--key-id",
        issued_payload["id"],
        "--expires-in-days",
        "3",
    )
    assert rotated.returncode == 0, rotated.stderr
    rotated_payload = json.loads(rotated.stdout)
    assert rotated_payload["api_key"].startswith("crop_")
    assert rotated_payload["api_key"] != issued_payload["api_key"]
