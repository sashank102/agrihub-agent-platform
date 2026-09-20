"""Async persistence operations for agent definitions."""

import uuid
from typing import Any

from sqlalchemy import or_, select, update
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
        agent_id: uuid.UUID | None = None,
    ) -> Agent:
        """Add and flush an agent version."""
        agent = Agent(
            id=agent_id or uuid.uuid4(),
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

    async def update_for_owner(
        self,
        agent_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        *,
        name: str | None = None,
        configuration: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        active: bool | None = None,
    ) -> Agent | None:
        """Update an agent only when it is owned by the requesting user."""
        return await self._update_scoped(
            Agent.id == agent_id,
            Agent.owner_user_id == owner_user_id,
            name=name,
            configuration=configuration,
            metadata=metadata,
            active=active,
        )

    async def update_global(
        self,
        agent_id: uuid.UUID,
        *,
        name: str | None = None,
        configuration: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        active: bool | None = None,
    ) -> Agent | None:
        """Explicitly update a global agent from a trusted system/admin path."""
        return await self._update_scoped(
            Agent.id == agent_id,
            Agent.owner_user_id.is_(None),
            name=name,
            configuration=configuration,
            metadata=metadata,
            active=active,
        )

    async def _update_scoped(
        self,
        *scope: object,
        name: str | None,
        configuration: dict[str, Any] | None,
        metadata: dict[str, Any] | None,
        active: bool | None,
    ) -> Agent | None:
        """Apply fields with ownership enforced in the UPDATE statement."""
        values: dict[str, Any] = {}
        if name is not None:
            values["name"] = name
        if configuration is not None:
            values["configuration"] = configuration
        if metadata is not None:
            values["metadata_"] = metadata
        if active is not None:
            values["active"] = active

        if not values:
            return await self.session.scalar(select(Agent).where(*scope))
        statement = update(Agent).where(*scope).values(**values).returning(Agent)
        return (await self.session.execute(statement)).scalar_one_or_none()
