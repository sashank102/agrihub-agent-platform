"""Plan 08 admission, cancellation, and retention checks."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agent_platform.db.models import Run, RunEvent
from agent_platform.db.session import session_scope
from agent_platform.main import create_app
from agent_platform.services.retention import apply_retention
from agent_platform.services.run_manager import RunManager
from tests.test_durable_runs import _create_thread, _echo_graph, _payload, _settings

pytest_plugins = ["tests.test_durable_runs"]
pytestmark = pytest.mark.postgres


def test_one_active_run_index_rejects_a_second_pending_row(postgres_database_uri: str):
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
                started = asyncio.Event()
                release = asyncio.Event()

                async def hold(self, run_id):
                    started.set()
                    await release.wait()

                async def post(text: str):
                    return await client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json=_payload(text),
                    )

                with pytest.MonkeyPatch.context() as patch:
                    patch.setattr(RunManager, "_before_task_attach", hold)
                    first = asyncio.create_task(post("one"))
                    await started.wait()
                    with pytest.raises(IntegrityError):
                        async with session_scope(app.state.session_factory) as session:
                            existing = await session.scalar(select(Run))
                            assert existing is not None
                            session.add(
                                Run(
                                    id=uuid.uuid4(),
                                    thread_id=existing.thread_id,
                                    agent_id=existing.agent_id,
                                    status="pending",
                                    input={},
                                    configuration={},
                                )
                            )
                            await session.flush()
                    release.set()
                    response = await first
                    assert response.status_code == 200

    asyncio.run(scenario())


def test_concurrent_admission_keeps_one_active_run(postgres_database_uri: str):
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_echo_graph(gates={"started": started, "release": release}),
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = await _create_thread(client)

                async def post(text: str):
                    return await client.post(
                        f"/threads/{thread['thread_id']}/runs/stream",
                        json=_payload(text),
                    )

                first = asyncio.create_task(post("one"))
                await started.wait()
                second = await post("two")
                assert second.status_code == 409
                detail = second.json()["detail"]
                assert detail["message"] == "thread already has an active run"
                assert detail["status"] in {"pending", "running"}
                async with session_scope(app.state.session_factory) as session:
                    runs = list((await session.scalars(select(Run))).all())
                assert len([run for run in runs if run.status in {"pending", "running"}]) == 1
                release.set()
                first_response = await first
                assert first_response.status_code == 200

    asyncio.run(scenario())


def test_cancel_during_binding_matches_durable_status(postgres_database_uri: str):
    async def scenario() -> None:
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_echo_graph(),
        )
        started = asyncio.Event()
        release = asyncio.Event()
        seen: dict[str, str] = {}

        async def pause(self, run_id):
            seen["run_id"] = str(run_id)
            started.set()
            await release.wait()

        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = await _create_thread(client)
                with pytest.MonkeyPatch.context() as patch:
                    patch.setattr(RunManager, "_before_task_attach", pause)

                    async def stream():
                        return await client.post(
                            f"/threads/{thread['thread_id']}/runs/stream",
                            json=_payload("early"),
                        )

                    stream_task = asyncio.create_task(stream())
                    await started.wait()
                    cancel_task = asyncio.create_task(
                        client.post(
                            f"/threads/{thread['thread_id']}/runs/{seen['run_id']}/cancel"
                        )
                    )
                    await asyncio.sleep(0.05)
                    release.set()
                    cancelled = await cancel_task
                    streamed = await stream_task
                assert streamed.status_code == 200
                assert "Echo: early" not in streamed.text
                assert cancelled.status_code == 200
                body = cancelled.json()
                assert body["status"] == "cancelled"
                assert body["status"] not in {"pending", "running"}
                again = await client.post(
                    f"/threads/{thread['thread_id']}/runs/{seen['run_id']}/cancel"
                )
                assert again.status_code == 200
                assert again.json()["status"] == "cancelled"
            async with session_scope(app.state.session_factory) as session:
                run = await session.scalar(select(Run))
            assert run is not None
            assert run.status == "cancelled"

    asyncio.run(scenario())


def test_cancel_binding_timeout_forces_terminal_status(postgres_database_uri: str):
    async def scenario() -> None:
        app = create_app(
            settings=_settings(postgres_database_uri),
            graph_builder=_echo_graph(),
        )
        started = asyncio.Event()
        release = asyncio.Event()
        seen: dict[str, str] = {}

        async def pause(self, run_id):
            seen["run_id"] = str(run_id)
            started.set()
            await release.wait()

        async def no_bound_task(self, run_id):
            return None

        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                thread = await _create_thread(client)
                with pytest.MonkeyPatch.context() as patch:
                    patch.setattr(RunManager, "_before_task_attach", pause)
                    patch.setattr(RunManager, "_task_for_cancel", no_bound_task)
                    stream_task = asyncio.create_task(
                        client.post(
                            f"/threads/{thread['thread_id']}/runs/stream",
                            json=_payload("must-not-run"),
                        )
                    )
                    await started.wait()
                    cancelled = await client.post(
                        f"/threads/{thread['thread_id']}/runs/{seen['run_id']}/cancel"
                    )
                    assert cancelled.status_code == 200
                    assert cancelled.json()["status"] == "cancelled"
                    release.set()
                    streamed = await stream_task

                assert streamed.status_code == 200
                assert "Echo: must-not-run" not in streamed.text

            async with session_scope(app.state.session_factory) as session:
                run = await session.scalar(select(Run))
            assert run is not None
            assert run.status == "cancelled"

    asyncio.run(scenario())


def test_retention_dry_run_does_not_delete_rows(postgres_database_uri: str):
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
                response = await client.post(
                    f"/threads/{thread['thread_id']}/runs/stream",
                    json=_payload("old"),
                )
                assert response.status_code == 200
            old = datetime.now(UTC) - timedelta(days=40)
            async with session_scope(app.state.session_factory) as session:
                run = await session.scalar(select(Run))
                assert run is not None
                run.finished_at = old
                events = list((await session.scalars(select(RunEvent))).all())
                assert events
                for event in events:
                    event.created_at = old
            report = await apply_retention(
                app.state.session_factory,
                apply=False,
                run_events_days=30,
            )
            assert report.dry_run is True
            assert report.run_events >= 1
            async with session_scope(app.state.session_factory) as session:
                still = await session.scalar(select(Run))
            assert still is not None
            applied = await apply_retention(
                app.state.session_factory,
                apply=True,
                run_events_days=30,
            )
            assert applied.dry_run is False
            assert applied.run_events >= 1

    asyncio.run(scenario())
