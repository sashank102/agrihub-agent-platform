"""Durable run lifecycle and basic Agent Protocol SSE streaming."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi.encoders import jsonable_encoder

from agent_platform.api.schemas import RunStreamRequest
from agent_platform.db.models import Run
from agent_platform.db.repositories import RunRepository, ThreadRepository
from agent_platform.db.session import AsyncSessionFactory, session_scope
from agent_platform.services.graph_registry import GraphRegistry, RegisteredGraph

logger = logging.getLogger(__name__)


class RunService:
    """Create durable run rows and stream in-process graph execution."""

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        registry: GraphRegistry,
        semaphore: asyncio.Semaphore,
        owner_user_id: uuid.UUID,
    ) -> None:
        """Bind application-scoped run dependencies."""
        self.session_factory = session_factory
        self.registry = registry
        self.semaphore = semaphore
        self.owner_user_id = owner_user_id

    async def create(
        self,
        thread_id: uuid.UUID,
        request: RunStreamRequest,
    ) -> tuple[Run, RegisteredGraph]:
        """Resolve a graph and commit a pending run before streaming."""
        registered = self.registry.resolve(request.assistant_id)
        if registered is None:
            raise KeyError("assistant not found")

        async with session_scope(self.session_factory) as session:
            run, _ = await RunRepository(session).create_idempotent(
                owner_user_id=self.owner_user_id,
                thread_id=thread_id,
                agent_id=registered.agent_id,
                input=request.input,
                configuration=request.config,
            )
        return run, registered

    async def stream(
        self,
        run: Run,
        registered: RegisteredGraph,
        request: RunStreamRequest,
    ) -> AsyncIterator[bytes]:
        """Yield metadata and values events while persisting terminal status."""
        yield self._event(
            "metadata",
            {
                "run_id": str(run.id),
                "thread_id": str(run.thread_id),
            },
        )
        try:
            async with self.semaphore:
                await self._mark_running(run.id)
                latest: Any = {}
                config = dict(request.config or {})
                configurable = dict(config.get("configurable") or {})
                configurable.update(
                    {
                        "thread_id": str(run.thread_id),
                        "checkpoint_ns": "",
                    }
                )
                config["configurable"] = configurable

                async for values in registered.graph.astream(
                    request.input or {},
                    config,
                    stream_mode="values",
                ):
                    latest = values
                    yield self._event("values", jsonable_encoder(values))

                await self._mark_completed(run.id, run.thread_id, latest)
        except Exception as exc:
            logger.exception("Graph run %s failed", run.id)
            await self._mark_failed(run.id, exc)
            yield self._event(
                "error",
                {
                    "error": "run_failed",
                    "message": "Graph execution failed",
                    "run_id": str(run.id),
                    "thread_id": str(run.thread_id),
                },
            )

    async def _mark_running(self, run_id: uuid.UUID) -> None:
        async with session_scope(self.session_factory) as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise RuntimeError("durable run disappeared before execution")
            await RunRepository(session).set_status(
                run,
                "running",
                started_at=datetime.now(UTC),
            )

    async def _mark_completed(
        self,
        run_id: uuid.UUID,
        thread_id: uuid.UUID,
        latest: Any,
    ) -> None:
        async with session_scope(self.session_factory) as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise RuntimeError("durable run disappeared before completion")
            encoded = jsonable_encoder(latest)
            summary = (
                {"keys": sorted(encoded)}
                if isinstance(encoded, dict)
                else {"result_type": type(encoded).__name__}
            )
            await RunRepository(session).set_status(
                run,
                "completed",
                finished_at=datetime.now(UTC),
                output_summary=summary,
            )
            thread = await ThreadRepository(session).get_for_owner(
                thread_id,
                self.owner_user_id,
            )
            if thread is not None:
                await ThreadRepository(session).update(thread, touch=True)

    async def _mark_failed(self, run_id: uuid.UUID, exc: Exception) -> None:
        async with session_scope(self.session_factory) as session:
            run = await session.get(Run, run_id)
            if run is None:
                return
            await RunRepository(session).set_status(
                run,
                "failed",
                finished_at=datetime.now(UTC),
                error_code=type(exc).__name__,
                error_message="Graph execution failed",
            )

    @staticmethod
    def _event(event: str, data: Any) -> bytes:
        payload = json.dumps(
            jsonable_encoder(data),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return f"event: {event}\ndata: {payload}\n\n".encode()
