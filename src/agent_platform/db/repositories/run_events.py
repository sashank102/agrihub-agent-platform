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
                Thread.status != "deleted",
            )
            .with_for_update(of=Run)
        )
        if locked_run_id is None:
            raise LookupError("run is not owned by the user")
        return await self._append_locked(
            run_id,
            event_type,
            payload,
        )

    async def append_for_system(
        self,
        *,
        run_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent:
        """Append an event from startup or shutdown without an owner scope."""
        locked_run_id = await self.session.scalar(
            select(Run.id).where(Run.id == run_id).with_for_update()
        )
        if locked_run_id is None:
            raise LookupError("run not found")
        return await self._append_locked(run_id, event_type, payload)

    async def append_terminal_if_absent(
        self,
        *,
        run_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[RunEvent, bool]:
        """Append one terminal event unless this run already has one.

        The run row is locked first so concurrent repairs keep a single
        monotonic sequence and do not insert a second end or error event.
        """
        locked_run_id = await self.session.scalar(
            select(Run.id).where(Run.id == run_id).with_for_update()
        )
        if locked_run_id is None:
            raise LookupError("run not found")
        existing = await self.session.scalar(
            select(RunEvent)
            .where(
                RunEvent.run_id == run_id,
                RunEvent.event_type.in_(("end", "error")),
            )
            .order_by(RunEvent.sequence)
            .limit(1)
        )
        if existing is not None:
            return existing, False
        created = await self._append_locked(run_id, event_type, payload)
        return created, True

    async def _append_locked(
        self,
        run_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any] | None,
    ) -> RunEvent:
        """Allocate the next sequence for a run already locked in this transaction."""
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
