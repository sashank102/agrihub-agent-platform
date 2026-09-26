"""Async persistence operations for run metadata."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.db.models import Run, RunEvent, Thread
from agent_platform.db.models.run import ACTIVE_RUN_STATUSES, TERMINAL_RUN_STATUSES

TERMINAL_EVENT_TYPES = frozenset({"end", "error"})


@dataclass(frozen=True, slots=True)
class ActiveRunRecord:
    """Active run fields needed to reconcile after a process restart."""

    id: uuid.UUID
    thread_id: uuid.UUID
    agent_id: uuid.UUID
    owner_user_id: uuid.UUID
    error_details: dict[str, Any]
    cancellation_requested: bool
    status: str


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
        run_id: uuid.UUID | None = None,
    ) -> tuple[Run, bool]:
        """Create a run or return the existing thread/idempotency-key run."""
        matching_thread = await self.session.scalar(
            select(Thread.id).where(
                Thread.id == thread_id,
                Thread.owner_user_id == owner_user_id,
                Thread.agent_id == agent_id,
                Thread.status != "deleted",
            )
        )
        if matching_thread is None:
            raise LookupError("thread is not owned by the user and agent")

        values = {
            "id": run_id or uuid.uuid4(),
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

    async def find_active_id(
        self,
        thread_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """Return one pending or running run id for an owned thread."""
        return await self.session.scalar(
            select(Run.id)
            .join(Thread, Thread.id == Run.thread_id)
            .where(
                Run.thread_id == thread_id,
                Thread.owner_user_id == owner_user_id,
                Thread.status != "deleted",
                Run.status.in_(ACTIVE_RUN_STATUSES),
            )
            .limit(1)
        )

    async def list_active_records(self) -> list["ActiveRunRecord"]:
        """Return pending and running runs with the thread owner."""
        rows = (
            await self.session.execute(
                select(
                    Run.id,
                    Run.thread_id,
                    Run.agent_id,
                    Thread.owner_user_id,
                    Run.error_details,
                    Run.cancellation_requested,
                    Run.status,
                )
                .join(Thread, Thread.id == Run.thread_id)
                .where(Run.status.in_(ACTIVE_RUN_STATUSES))
                .order_by(Run.created_at, Run.id)
            )
        ).all()
        return [
            ActiveRunRecord(
                id=row.id,
                thread_id=row.thread_id,
                agent_id=row.agent_id,
                owner_user_id=row.owner_user_id,
                error_details=dict(row.error_details or {}),
                cancellation_requested=bool(row.cancellation_requested),
                status=row.status,
            )
            for row in rows
        ]

    async def list_missing_terminal_events(self) -> list[Run]:
        """Return terminal runs that do not yet have an end or error event."""
        terminal_event = (
            select(RunEvent.run_id)
            .where(
                RunEvent.run_id == Run.id,
                RunEvent.event_type.in_(tuple(TERMINAL_EVENT_TYPES)),
            )
            .exists()
        )
        return list(
            (
                await self.session.scalars(
                    select(Run)
                    .where(
                        Run.status.in_(TERMINAL_RUN_STATUSES),
                        ~terminal_event,
                    )
                    .order_by(Run.created_at, Run.id)
                )
            ).all()
        )

    async def list_active_ids(self) -> list[uuid.UUID]:
        """Return every pending or running run id for process reconciliation."""
        return list(
            (
                await self.session.scalars(
                    select(Run.id)
                    .where(Run.status.in_(ACTIVE_RUN_STATUSES))
                    .order_by(Run.created_at, Run.id)
                )
            ).all()
        )

    async def summarize_for_owner(
        self,
        thread_ids: list[uuid.UUID],
        owner_user_id: uuid.UUID,
    ) -> dict[uuid.UUID, tuple[bool, str | None]]:
        """Return active-run presence and latest status for owned threads."""
        if not thread_ids:
            return {}
        latest_rows = (
            await self.session.execute(
                select(Run.thread_id, Run.status)
                .join(Thread, Thread.id == Run.thread_id)
                .where(
                    Thread.owner_user_id == owner_user_id,
                    Run.thread_id.in_(thread_ids),
                )
                .distinct(Run.thread_id)
                .order_by(Run.thread_id, Run.created_at.desc(), Run.id.desc())
            )
        ).all()
        active_ids = set(
            (
                await self.session.scalars(
                    select(Run.thread_id)
                    .join(Thread, Thread.id == Run.thread_id)
                    .where(
                        Thread.owner_user_id == owner_user_id,
                        Run.thread_id.in_(thread_ids),
                        Run.status.in_(ACTIVE_RUN_STATUSES),
                    )
                    .distinct()
                )
            ).all()
        )
        summary = {
            thread_id: (thread_id in active_ids, status)
            for thread_id, status in latest_rows
        }
        for thread_id in thread_ids:
            summary.setdefault(thread_id, (thread_id in active_ids, None))
        return summary

    async def set_status_for_owner(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        status: str,
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        output_summary: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_details: dict[str, Any] | None = None,
        cancellation_requested: bool | None = None,
        touch_thread: bool = False,
    ) -> Run | None:
        """Transition an owned run after reloading it inside this session."""
        run = await self._lock_owned(run_id, owner_user_id)
        if run is None:
            return None
        if _running_transition_blocked(run, status):
            return run
        if run.status in TERMINAL_RUN_STATUSES and run.status != status:
            return run
        self._apply_status(
            run,
            status,
            started_at=started_at,
            finished_at=finished_at,
            output_summary=output_summary,
            error_code=error_code,
            error_message=error_message,
            error_details=error_details,
            cancellation_requested=cancellation_requested,
        )
        if touch_thread:
            from agent_platform.db.repositories.threads import ThreadRepository

            await ThreadRepository(self.session).update_for_owner(
                run.thread_id,
                owner_user_id,
                touch=True,
            )
        await self.session.flush()
        return run

    async def set_status_internal(
        self,
        run_id: uuid.UUID,
        status: str,
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        output_summary: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_details: dict[str, Any] | None = None,
        cancellation_requested: bool | None = None,
        only_active: bool = False,
    ) -> Run | None:
        """Transition a run from a trusted startup, shutdown, or repair path."""
        run = await self.session.scalar(
            select(Run).where(Run.id == run_id).with_for_update()
        )
        if run is None:
            return None
        if _running_transition_blocked(run, status):
            return run
        if only_active and run.status not in ACTIVE_RUN_STATUSES:
            return run
        if run.status in TERMINAL_RUN_STATUSES and run.status != status:
            return run
        self._apply_status(
            run,
            status,
            started_at=started_at,
            finished_at=finished_at,
            output_summary=output_summary,
            error_code=error_code,
            error_message=error_message,
            error_details=error_details,
            cancellation_requested=cancellation_requested,
        )
        await self.session.flush()
        return run

    async def request_cancellation_for_owner(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> Run | None:
        """Record a cancellation request for one owned run."""
        run = await self._lock_owned(run_id, owner_user_id)
        if run is None:
            return None
        run.cancellation_requested = True
        await self.session.flush()
        return run

    async def merge_reconciliation(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        reconciliation: dict[str, Any],
    ) -> None:
        """Store terminal intent on an active run without changing its status."""
        run = await self._lock_owned(run_id, owner_user_id)
        if run is None or run.status in TERMINAL_RUN_STATUSES:
            return
        details = dict(run.error_details or {})
        details["reconciliation"] = reconciliation
        run.error_details = details
        await self.session.flush()

    async def _lock_owned(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> Run | None:
        return await self.session.scalar(
            select(Run)
            .join(Thread, Thread.id == Run.thread_id)
            .where(
                Run.id == run_id,
                Thread.owner_user_id == owner_user_id,
                Thread.status != "deleted",
            )
            .with_for_update(of=Run)
        )

    @staticmethod
    def _apply_status(
        run: Run,
        status: str,
        *,
        started_at: datetime | None,
        finished_at: datetime | None,
        output_summary: dict[str, Any] | None,
        error_code: str | None,
        error_message: str | None,
        error_details: dict[str, Any] | None,
        cancellation_requested: bool | None,
    ) -> None:
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
        if cancellation_requested is not None:
            run.cancellation_requested = cancellation_requested


def _running_transition_blocked(run: Run, status: str) -> bool:
    """Refuse to move a cancelled or already terminal run back to running."""
    if status != "running":
        return False
    if run.cancellation_requested:
        return True
    return run.status in TERMINAL_RUN_STATUSES
