"""Async persistence operations for thread metadata."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.db.models import Agent, Thread


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
    ) -> list[Thread]:
        """List an owner's threads by most recent activity."""
        statement = select(Thread).where(Thread.owner_user_id == owner_user_id)
        if status is not None:
            statement = statement.where(Thread.status == status)
        statement = statement.order_by(Thread.last_activity_at.desc())
        return list((await self.session.scalars(statement)).all())

    async def update(
        self,
        thread: Thread,
        *,
        title: str | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        touch: bool = False,
    ) -> Thread:
        """Update mutable thread fields and flush."""
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
