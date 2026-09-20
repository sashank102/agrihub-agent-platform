"""Async persistence operations for agent definitions."""

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.db.models import Agent


class AgentRepository:
    """Persist and query owner-scoped agent definitions."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a caller-owned session."""
        self.session = session

    async def create(
        self,
        *,
        graph_id: str,
        name: str,
        owner_user_id: uuid.UUID | None = None,
        version: int = 1,
        configuration: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        active: bool = True,
    ) -> Agent:
        """Add and flush an agent version."""
        agent = Agent(
            owner_user_id=owner_user_id,
            graph_id=graph_id,
            name=name,
            version=version,
            configuration=configuration or {},
            metadata_=metadata or {},
            active=active,
        )
        self.session.add(agent)
        await self.session.flush()
        return agent

    async def get_for_owner(
        self,
        agent_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        *,
        include_global: bool = True,
    ) -> Agent | None:
        """Return an owned agent and, optionally, a global agent."""
        ownership = Agent.owner_user_id == owner_user_id
        if include_global:
            ownership = or_(ownership, Agent.owner_user_id.is_(None))
        return await self.session.scalar(
            select(Agent).where(Agent.id == agent_id, ownership)
        )

    async def list_for_owner(
        self,
        owner_user_id: uuid.UUID,
        *,
        include_global: bool = True,
        active_only: bool = True,
    ) -> list[Agent]:
        """List agent versions visible to an owner."""
        ownership = Agent.owner_user_id == owner_user_id
        if include_global:
            ownership = or_(ownership, Agent.owner_user_id.is_(None))
        statement = select(Agent).where(ownership)
        if active_only:
            statement = statement.where(Agent.active.is_(True))
        statement = statement.order_by(Agent.graph_id, Agent.version)
        return list((await self.session.scalars(statement)).all())

    async def update(
        self,
        agent: Agent,
        *,
        name: str | None = None,
        configuration: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        active: bool | None = None,
    ) -> Agent:
        """Update mutable agent fields and flush."""
        if name is not None:
            agent.name = name
        if configuration is not None:
            agent.configuration = configuration
        if metadata is not None:
            agent.metadata_ = metadata
        if active is not None:
            agent.active = active
        await self.session.flush()
        return agent
