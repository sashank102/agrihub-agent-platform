"""Thread ownership, metadata, and checkpoint state orchestration."""

import uuid
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.api.schemas import (
    CheckpointResponse,
    ThreadCreateRequest,
    ThreadResponse,
    ThreadSearchRequest,
    ThreadStateResponse,
)
from agent_platform.db.models import Thread
from agent_platform.db.repositories import ThreadRepository
from agent_platform.services.graph_registry import GraphRegistry, RegisteredGraph


class ThreadService:
    """Coordinate relational thread metadata with LangGraph checkpoints."""

    def __init__(
        self,
        session: AsyncSession,
        registry: GraphRegistry,
        owner_user_id: uuid.UUID,
        default_graph_id: str,
    ) -> None:
        """Bind one request-scoped service."""
        self.session = session
        self.registry = registry
        self.owner_user_id = owner_user_id
        self.default_graph_id = default_graph_id
        self.repository = ThreadRepository(session)

    async def create(self, request: ThreadCreateRequest) -> ThreadResponse:
        """Create one owner-scoped thread with trusted graph identity tags."""
        identifier = str(
            request.metadata.get("assistant_id")
            or request.metadata.get("graph_id")
            or self.default_graph_id
        )
        registered = self._resolve_graph(identifier)

        if request.thread_id is not None:
            existing = await self.repository.get_for_owner(
                request.thread_id,
                self.owner_user_id,
            )
            if existing is not None:
                if request.if_exists in {"do_nothing", "return"}:
                    return await self.to_response(existing)
                raise FileExistsError("thread already exists")

        metadata = {
            **request.metadata,
            "graph_id": registered.graph_id,
            "assistant_id": str(registered.agent_id),
        }
        thread = await self.repository.create(
            thread_id=request.thread_id,
            owner_user_id=self.owner_user_id,
            agent_id=registered.agent_id,
            metadata=metadata,
        )
        return await self.to_response(thread)

    async def search(
        self,
        request: ThreadSearchRequest,
    ) -> list[ThreadResponse]:
        """Search only the development principal's threads."""
        platform_status = None
        if request.status is not None:
            if request.status == "idle":
                platform_status = "active"
            elif request.status not in {"busy", "interrupted", "error"}:
                platform_status = request.status
            else:
                return []
        threads = await self.repository.list_for_owner(
            self.owner_user_id,
            status=platform_status,
            metadata=request.metadata,
            ids=request.ids,
            limit=request.limit,
            offset=request.offset,
        )
        return [await self.to_response(thread) for thread in threads]

    async def get(self, thread_id: uuid.UUID) -> ThreadResponse:
        """Get an accessible thread or fail without leaking its existence."""
        thread = await self._get_model(thread_id)
        return await self.to_response(thread)

    async def get_state(self, thread_id: uuid.UUID) -> ThreadStateResponse:
        """Load current values from the thread's LangGraph checkpoint."""
        thread = await self._get_model(thread_id)
        registered = self._resolve_graph(str(thread.agent_id))
        snapshot = await registered.graph.aget_state(self._config(thread.id))
        return self._state_response(thread.id, snapshot)

    async def to_response(self, thread: Thread) -> ThreadResponse:
        """Convert relational metadata plus latest checkpoint values."""
        registered = self._resolve_graph(str(thread.agent_id))
        snapshot = await registered.graph.aget_state(self._config(thread.id))
        values = jsonable_encoder(getattr(snapshot, "values", {}) or {})
        return ThreadResponse(
            thread_id=str(thread.id),
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            state_updated_at=thread.last_activity_at,
            metadata=dict(thread.metadata_),
            status="idle",
            values=values,
            interrupts={},
        )

    async def _get_model(self, thread_id: uuid.UUID) -> Thread:
        thread = await self.repository.get_for_owner(
            thread_id,
            self.owner_user_id,
        )
        if thread is None or thread.status == "deleted":
            raise LookupError("thread not found")
        return thread

    def _resolve_graph(self, identifier: str) -> RegisteredGraph:
        registered = self.registry.resolve(identifier)
        if registered is None:
            raise KeyError("assistant not found")
        return registered

    @staticmethod
    def _config(thread_id: uuid.UUID) -> dict[str, dict[str, str]]:
        return {
            "configurable": {
                "thread_id": str(thread_id),
                "checkpoint_ns": "",
            }
        }

    @classmethod
    def _state_response(
        cls,
        thread_id: uuid.UUID,
        snapshot: Any,
    ) -> ThreadStateResponse:
        config = getattr(snapshot, "config", None) or cls._config(thread_id)
        parent = getattr(snapshot, "parent_config", None)
        return ThreadStateResponse(
            values=jsonable_encoder(getattr(snapshot, "values", {}) or {}),
            next=list(getattr(snapshot, "next", ()) or ()),
            checkpoint=cls._checkpoint(thread_id, config),
            metadata=jsonable_encoder(getattr(snapshot, "metadata", None)),
            created_at=getattr(snapshot, "created_at", None),
            parent_checkpoint=(
                cls._checkpoint(thread_id, parent) if parent is not None else None
            ),
            tasks=[],
        )

    @staticmethod
    def _checkpoint(
        thread_id: uuid.UUID,
        config: dict[str, Any],
    ) -> CheckpointResponse:
        configurable = config.get("configurable", {})
        return CheckpointResponse(
            thread_id=str(configurable.get("thread_id") or thread_id),
            checkpoint_ns=str(configurable.get("checkpoint_ns") or ""),
            checkpoint_id=configurable.get("checkpoint_id"),
            checkpoint_map=configurable.get("checkpoint_map"),
        )
