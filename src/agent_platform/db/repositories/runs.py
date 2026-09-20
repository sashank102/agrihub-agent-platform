"""Async persistence operations for run metadata."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.db.models import Run, Thread


class RunRepository:
    """Persist runs without committing the caller's transaction."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a caller-owned session."""
        self.session = session

    async def create_idempotent(
        self,
        *,
        owner_user_id: uuid.UUID,
        thread_id: uuid.UUID,
        agent_id: uuid.UUID,
        input: dict[str, Any] | None = None,
        configuration: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[Run, bool]:
        """Create a run or return the existing thread/idempotency-key run."""
        matching_thread = await self.session.scalar(
            select(Thread.id).where(
                Thread.id == thread_id,
                Thread.owner_user_id == owner_user_id,
                Thread.agent_id == agent_id,
            )
        )
        if matching_thread is None:
            raise LookupError("thread is not owned by the user and agent")

        values = {
            "id": uuid.uuid4(),
            "thread_id": thread_id,
            "agent_id": agent_id,
            "input": input or {},
            "configuration": configuration or {},
            "idempotency_key": idempotency_key,
        }
        if idempotency_key is None:
            run = Run(**values)
            self.session.add(run)
            await self.session.flush()
            return run, True

        statement = (
            insert(Run)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[Run.thread_id, Run.idempotency_key],
                index_where=Run.idempotency_key.is_not(None),
            )
            .returning(Run)
        )
        run = (await self.session.execute(statement)).scalar_one_or_none()
        if run is not None:
            return run, True

        existing = await self.session.scalar(
            select(Run).where(
                Run.thread_id == thread_id,
                Run.idempotency_key == idempotency_key,
            )
        )
        if existing is None:  # Defensive against external constraint changes.
            raise RuntimeError("idempotent run conflict could not be resolved")
        return existing, False

    async def get_for_owner(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> Run | None:
        """Return a run only through an owned thread."""
        return await self.session.scalar(
            select(Run)
            .join(Thread, Thread.id == Run.thread_id)
            .where(
                Run.id == run_id,
                Thread.owner_user_id == owner_user_id,
            )
        )

    async def list_for_thread(
        self,
        thread_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        *,
        status: str | None = None,
    ) -> list[Run]:
        """List runs for one owned thread in creation order."""
        statement = (
            select(Run)
            .join(Thread, Thread.id == Run.thread_id)
            .where(
                Run.thread_id == thread_id,
                Thread.owner_user_id == owner_user_id,
            )
        )
        if status is not None:
            statement = statement.where(Run.status == status)
        statement = statement.order_by(Run.created_at, Run.id)
        return list((await self.session.scalars(statement)).all())

    async def set_status(
        self,
        run: Run,
        status: str,
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        output_summary: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> Run:
        """Apply a run lifecycle transition and flush it."""
        run.status = status
        if started_at is not None:
            run.started_at = started_at
        if finished_at is not None:
            run.finished_at = finished_at
        if output_summary is not None:
            run.output_summary = output_summary
        if error_code is not None:
            run.error_code = error_code
        if error_message is not None:
            run.error_message = error_message
        if error_details is not None:
            run.error_details = error_details
        await self.session.flush()
        return run

    async def request_cancellation(self, run: Run) -> Run:
        """Mark cancellation as requested and flush."""
        run.cancellation_requested = True
        await self.session.flush()
        return run
