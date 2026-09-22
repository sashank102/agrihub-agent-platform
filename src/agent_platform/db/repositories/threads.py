"""Async persistence operations for thread metadata."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.db.models import Agent, Run, Thread
from agent_platform.db.models.run import ACTIVE_RUN_STATUSES


class ThreadRepository:
    """Persist and query threads within a user ownership boundary."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a caller-owned session."""
        self.session = session

    async def create(
        self,
        *,
        owner_user_id: uuid.UUID,
        agent_id: uuid.UUID,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        thread_id: uuid.UUID | None = None,
    ) -> Thread:
        """Add a thread for an owned or global agent."""
        visible_agent_id = await self.session.scalar(
            select(Agent.id).where(
                Agent.id == agent_id,
                or_(
                    Agent.owner_user_id == owner_user_id,
                    Agent.owner_user_id.is_(None),
                ),
            )
        )
        if visible_agent_id is None:
            raise LookupError("agent is not visible to the thread owner")

        thread = Thread(
            id=thread_id or uuid.uuid4(),
            owner_user_id=owner_user_id,
            agent_id=agent_id,
            title=title,
            metadata_=metadata or {},
        )
        self.session.add(thread)
        await self.session.flush()
        return thread

    async def get_for_owner(
        self,
        thread_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> Thread | None:
        """Return a thread only when the owner matches."""
        return await self.session.scalar(
            select(Thread).where(
                Thread.id == thread_id,
                Thread.owner_user_id == owner_user_id,
            )
        )

    async def list_for_owner(
        self,
        owner_user_id: uuid.UUID,
        *,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        ids: list[uuid.UUID] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Thread]:
        """List an owner's threads by most recent activity."""
        statement = select(Thread).where(
            Thread.owner_user_id == owner_user_id,
            Thread.status != "deleted",
        )
        if status is not None:
            statement = statement.where(Thread.status == status)
        if metadata:
            statement = statement.where(Thread.metadata_.contains(metadata))
        if ids is not None:
            statement = statement.where(Thread.id.in_(ids))
        statement = statement.order_by(Thread.last_activity_at.desc(), Thread.id)
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        return list((await self.session.scalars(statement)).all())

    async def list_for_owner_by_protocol_status(
        self,
        owner_user_id: uuid.UUID,
        protocol_status: str,
        *,
        metadata: dict[str, Any] | None = None,
        ids: list[uuid.UUID] | None = None,
        platform_status: str | None = None,
        limit: int,
        offset: int = 0,
    ) -> list[Thread]:
        """Page threads by durable run status without loading checkpoint rows.

        ``busy``, ``error``, and ``interrupted`` come from run rows. Every other
        accessible thread is ``idle``. Ordering matches ``list_for_owner``.
        """
        active_run = exists(
            select(Run.id).where(
                Run.thread_id == Thread.id,
                Run.status.in_(ACTIVE_RUN_STATUSES),
            )
        )
        latest_status = (
            select(Run.status)
            .where(Run.thread_id == Thread.id)
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        if protocol_status == "busy":
            status_clause = active_run
        elif protocol_status == "error":
            status_clause = and_(~active_run, latest_status == "failed")
        elif protocol_status == "interrupted":
            status_clause = and_(~active_run, latest_status == "interrupted")
        elif protocol_status == "idle":
            status_clause = and_(
                ~active_run,
                or_(
                    latest_status.is_(None),
                    latest_status.notin_(("failed", "interrupted")),
                ),
            )
        else:
            raise ValueError("unsupported protocol status")

        statement = select(Thread).where(
            Thread.owner_user_id == owner_user_id,
            Thread.status != "deleted",
            status_clause,
        )
        if platform_status is not None:
            statement = statement.where(Thread.status == platform_status)
        if metadata:
            statement = statement.where(Thread.metadata_.contains(metadata))
        if ids is not None:
            statement = statement.where(Thread.id.in_(ids))
        statement = statement.order_by(Thread.last_activity_at.desc(), Thread.id)
        if offset:
            statement = statement.offset(offset)
        statement = statement.limit(limit)
        return list((await self.session.scalars(statement)).all())

    async def get_by_id(self, thread_id: uuid.UUID) -> Thread | None:
        """Return a thread by primary key without applying an owner scope."""
        return await self.session.get(Thread, thread_id)

    async def update_for_owner(
        self,
        thread_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        *,
        title: str | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        touch: bool = False,
    ) -> Thread | None:
        """Update one owned, non-deleted thread loaded inside this session."""
        thread = await self.session.scalar(
            select(Thread)
            .where(
                Thread.id == thread_id,
                Thread.owner_user_id == owner_user_id,
                Thread.status != "deleted",
            )
            .with_for_update()
        )
        if thread is None:
            return None
        if title is not None:
            thread.title = title
        if status is not None:
            thread.status = status
        if metadata is not None:
            thread.metadata_ = metadata
        if touch:
            thread.last_activity_at = datetime.now(UTC)
        await self.session.flush()
        return thread

    async def update_status_internal(
        self,
        thread_id: uuid.UUID,
        status: str,
    ) -> Thread | None:
        """Update thread lifecycle from a trusted system path."""
        thread = await self.session.scalar(
            select(Thread).where(Thread.id == thread_id).with_for_update()
        )
        if thread is None:
            return None
        thread.status = status
        await self.session.flush()
        return thread
