"""Thread ownership, metadata, and checkpoint state orchestration."""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from agent_platform.api.schemas import (
    ThreadCreateRequest,
    ThreadHistoryRequest,
    ThreadResponse,
    ThreadSearchRequest,
    ThreadStateResponse,
)
from agent_platform.db.models import Thread
from agent_platform.db.repositories import RunRepository, ThreadRepository
from agent_platform.db.session import AsyncSessionFactory, session_scope
from agent_platform.services.errors import CheckpointReferenceError
from agent_platform.services.graph_registry import GraphRegistry, RegisteredGraph
from agent_platform.services.snapshots import (
    agent_protocol_status,
    interrupt_map,
    snapshot_interrupted,
    snapshot_to_state,
)

CHECKPOINT_READ_CONCURRENCY = 4
PROTOCOL_STATUSES = {"idle", "busy", "interrupted", "error"}
PLATFORM_STATUSES = {"active", "archived"}
_UNTRUSTED_METADATA_KEYS = {"owner_user_id", "user_id", "owner_id"}


@dataclass(frozen=True, slots=True)
class ThreadView:
    """Detached thread fields safe to use after the session closes."""

    id: uuid.UUID
    agent_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    metadata: dict[str, Any]
    platform_status: str

    @classmethod
    def from_thread(cls, thread: Thread) -> "ThreadView":
        """Copy loaded columns before the transaction ends."""
        return cls(
            id=thread.id,
            agent_id=thread.agent_id,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            last_activity_at=thread.last_activity_at,
            metadata=dict(thread.metadata_ or {}),
            platform_status=thread.status,
        )


class ThreadService:
    """Coordinate relational thread metadata with LangGraph checkpoints."""

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        registry: GraphRegistry,
        owner_user_id: uuid.UUID,
        default_graph_id: str,
        auditor: Any | None = None,
    ) -> None:
        """Bind application-scoped dependencies for one principal."""
        self.session_factory = session_factory
        self.registry = registry
        self.owner_user_id = owner_user_id
        self.default_graph_id = default_graph_id
        self.auditor = auditor

    async def create(self, request: ThreadCreateRequest) -> ThreadResponse:
        """Create one owner-scoped thread with trusted graph identity tags."""
        identifier = str(
            request.metadata.get("assistant_id")
            or request.metadata.get("graph_id")
            or self.default_graph_id
        )
        registered = self._resolve_graph(identifier)
        view, created = await self._insert_thread(request, registered)
        if created:
            await self._audit(
                "thread.created",
                "thread",
                str(view.id),
                {"graph_id": registered.graph_id},
            )
        signal = await self._signal_for(view.id)
        snapshot = await self._read_snapshot(view)
        return self._response(view, signal, snapshot)

    async def search(self, request: ThreadSearchRequest) -> list[ThreadResponse]:
        """Search the caller's threads and hydrate checkpoints with a bounded fan-out."""
        if request.status == "deleted":
            return []
        protocol_status = (
            request.status if request.status in PROTOCOL_STATUSES else None
        )
        platform_status = (
            request.status if request.status in PLATFORM_STATUSES else None
        )
        if request.status is not None and protocol_status is None and platform_status is None:
            return []

        await self._audit_foreign_search_ids(request.ids)
        async with session_scope(self.session_factory) as session:
            repository = ThreadRepository(session)
            if protocol_status is None:
                threads = await repository.list_for_owner(
                    self.owner_user_id,
                    status=platform_status,
                    metadata=request.metadata,
                    ids=request.ids,
                    limit=request.limit,
                    offset=request.offset,
                )
            else:
                threads = await repository.list_for_owner_by_protocol_status(
                    self.owner_user_id,
                    protocol_status,
                    metadata=request.metadata,
                    ids=request.ids,
                    platform_status=platform_status,
                    limit=request.limit,
                    offset=request.offset,
                )
            views = [ThreadView.from_thread(thread) for thread in threads]
            signals = await RunRepository(session).summarize_for_owner(
                [view.id for view in views],
                self.owner_user_id,
            )

        return await self._hydrate(views, signals)

    async def get(self, thread_id: uuid.UUID) -> ThreadResponse:
        """Get an accessible thread or fail without leaking its existence."""
        view = await self._require_thread(thread_id)
        signal = await self._signal_for(view.id)
        snapshot = await self._read_snapshot(view)
        return self._response(view, signal, snapshot)

    async def get_state(self, thread_id: uuid.UUID) -> ThreadStateResponse:
        """Load current values after the metadata transaction has closed."""
        view = await self._require_thread(thread_id)
        snapshot = await self._read_snapshot(view)
        return snapshot_to_state(view.id, snapshot)

    async def history(
        self,
        thread_id: uuid.UUID,
        request: ThreadHistoryRequest,
    ) -> list[ThreadStateResponse]:
        """Return checkpoint history in the shape used by the LangGraph SDK."""
        view = await self._require_thread(thread_id)
        registered = self._resolve_graph(str(view.agent_id))
        config = self._history_config(view.id, request.checkpoint)
        before = self._history_config(view.id, request.before) if request.before else None
        states: list[ThreadStateResponse] = []
        async for snapshot in registered.graph.aget_state_history(
            config,
            filter=request.metadata,
            before=before,
            limit=request.limit,
        ):
            states.append(snapshot_to_state(view.id, snapshot))
        return states

    async def _insert_thread(
        self,
        request: ThreadCreateRequest,
        registered: RegisteredGraph,
    ) -> tuple[ThreadView, bool]:
        supplied = {
            key: value
            for key, value in request.metadata.items()
            if key not in _UNTRUSTED_METADATA_KEYS
        }
        metadata = {
            **supplied,
            "graph_id": registered.graph_id,
            "assistant_id": str(registered.agent_id),
        }
        async with session_scope(self.session_factory) as session:
            repository = ThreadRepository(session)
            if request.thread_id is not None:
                existing = await repository.get_by_id(request.thread_id)
                if existing is not None:
                    return await self._existing_thread(existing, request, repository), False

            duplicate = False
            thread: Thread | None = None
            try:
                async with session.begin_nested():
                    thread = await repository.create(
                        thread_id=request.thread_id,
                        owner_user_id=self.owner_user_id,
                        agent_id=registered.agent_id,
                        metadata=metadata,
                    )
            except IntegrityError as exc:
                if not _is_unique_violation(exc):
                    raise
                duplicate = True
            if duplicate:
                if request.thread_id is None:
                    raise FileExistsError("thread already exists")
                existing = await repository.get_by_id(request.thread_id)
                if existing is None:
                    raise FileExistsError("thread already exists")
                return await self._existing_thread(existing, request, repository), False
            if thread is None:
                raise RuntimeError("thread insert did not return a row")
            return ThreadView.from_thread(thread), True

    async def _existing_thread(
        self,
        existing: Thread,
        request: ThreadCreateRequest,
        repository: ThreadRepository,
    ) -> ThreadView:
        if existing.owner_user_id != self.owner_user_id or existing.status == "deleted":
            raise LookupError("thread not found")
        if request.if_exists in {"do_nothing", "return"}:
            return ThreadView.from_thread(existing)
        raise FileExistsError("thread already exists")

    async def _require_thread(self, thread_id: uuid.UUID) -> ThreadView:
        foreign = False
        async with session_scope(self.session_factory) as session:
            thread = await ThreadRepository(session).get_by_id(thread_id)
            if thread is None:
                raise LookupError("thread not found")
            if thread.owner_user_id != self.owner_user_id:
                foreign = True
            elif thread.status == "deleted":
                raise LookupError("thread not found")
            else:
                return ThreadView.from_thread(thread)
        if foreign:
            await self._audit(
                "access.denied",
                "thread",
                str(thread_id),
                {"scope": "thread"},
            )
        raise LookupError("thread not found")

    async def _audit_foreign_search_ids(
        self,
        ids: list[uuid.UUID] | None,
    ) -> None:
        if not ids:
            return
        async with session_scope(self.session_factory) as session:
            repository = ThreadRepository(session)
            foreign_ids = []
            for thread_id in ids:
                thread = await repository.get_by_id(thread_id)
                if thread is not None and thread.owner_user_id != self.owner_user_id:
                    foreign_ids.append(str(thread_id))
        for foreign_id in foreign_ids:
            await self._audit(
                "access.denied",
                "thread",
                foreign_id,
                {"scope": "search"},
            )

    async def _audit(
        self,
        action: str,
        resource_type: str,
        resource_id: str,
        metadata: dict[str, Any],
    ) -> None:
        if self.auditor is None:
            return
        await self.auditor.record(
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor_user_id=self.owner_user_id,
            metadata=metadata,
        )

    async def _signal_for(self, thread_id: uuid.UUID) -> tuple[bool, str | None]:
        async with session_scope(self.session_factory) as session:
            signals = await RunRepository(session).summarize_for_owner(
                [thread_id],
                self.owner_user_id,
            )
        return signals.get(thread_id, (False, None))

    async def _hydrate(
        self,
        views: list[ThreadView],
        signals: dict[uuid.UUID, tuple[bool, str | None]],
    ) -> list[ThreadResponse]:
        semaphore = asyncio.Semaphore(CHECKPOINT_READ_CONCURRENCY)

        async def hydrate(view: ThreadView) -> ThreadResponse:
            async with semaphore:
                snapshot = await self._read_snapshot(view)
            return self._response(view, signals.get(view.id, (False, None)), snapshot)

        return list(await asyncio.gather(*(hydrate(view) for view in views)))

    async def _read_snapshot(self, view: ThreadView) -> Any:
        registered = self._resolve_graph(str(view.agent_id))
        return await registered.graph.aget_state(
            {
                "configurable": {
                    "thread_id": str(view.id),
                    "checkpoint_ns": "",
                }
            }
        )

    def _response(
        self,
        view: ThreadView,
        signal: tuple[bool, str | None],
        snapshot: Any,
    ) -> ThreadResponse:
        state = snapshot_to_state(view.id, snapshot)
        has_active, latest_status = signal
        return ThreadResponse(
            thread_id=str(view.id),
            created_at=view.created_at,
            updated_at=view.updated_at,
            state_updated_at=view.last_activity_at,
            metadata=view.metadata,
            status=agent_protocol_status(
                has_active_run=has_active,
                latest_run_status=latest_status,
                checkpoint_interrupted=snapshot_interrupted(snapshot),
            ),
            values=state.values,
            interrupts=interrupt_map(snapshot),
        )

    def _resolve_graph(self, identifier: str) -> RegisteredGraph:
        registered = self.registry.resolve(identifier)
        if registered is None:
            raise KeyError("assistant not found")
        return registered

    @staticmethod
    def _history_config(
        thread_id: uuid.UUID,
        checkpoint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if checkpoint is None:
            return {
                "configurable": {
                    "thread_id": str(thread_id),
                    "checkpoint_ns": "",
                }
            }
        source = checkpoint.get("configurable") if "configurable" in checkpoint else checkpoint
        source = dict(source or {})
        supplied_thread = source.get("thread_id")
        if supplied_thread is not None and str(supplied_thread) != str(thread_id):
            raise CheckpointReferenceError(
                "checkpoint belongs to a different thread",
                422,
            )
        configurable: dict[str, Any] = {
            "thread_id": str(thread_id),
            "checkpoint_ns": str(source.get("checkpoint_ns") or ""),
        }
        if source.get("checkpoint_id"):
            configurable["checkpoint_id"] = str(source["checkpoint_id"])
        if isinstance(source.get("checkpoint_map"), dict):
            configurable["checkpoint_map"] = source["checkpoint_map"]
        return {"configurable": configurable}


def _is_unique_violation(exc: IntegrityError) -> bool:
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return sqlstate == "23505"
