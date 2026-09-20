"""Atomic append and replay operations for run events."""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.db.models import Run, RunEvent, Thread


class RunEventRepository:
    """Append monotonically sequenced events under a per-run row lock."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a caller-owned session."""
        self.session = session

    async def append(
        self,
        *,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent:
        """Lock the owned run and append its next sequence atomically."""
        locked_run_id = await self.session.scalar(
            select(Run.id)
            .join(Thread, Thread.id == Run.thread_id)
            .where(
                Run.id == run_id,
                Thread.owner_user_id == owner_user_id,
            )
            .with_for_update(of=Run)
        )
        if locked_run_id is None:
            raise LookupError("run is not owned by the user")

        current_sequence = await self.session.scalar(
            select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)
        )
        event = RunEvent(
            run_id=run_id,
            sequence=(current_sequence or 0) + 1,
            event_type=event_type,
            payload=payload or {},
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def replay(
        self,
        *,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> list[RunEvent]:
        """Return an owned run's events in deterministic replay order."""
        statement = (
            select(RunEvent)
            .join(Run, Run.id == RunEvent.run_id)
            .join(Thread, Thread.id == Run.thread_id)
            .where(
                RunEvent.run_id == run_id,
                RunEvent.sequence > after_sequence,
                Thread.owner_user_id == owner_user_id,
            )
            .order_by(RunEvent.sequence)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return list((await self.session.scalars(statement)).all())
