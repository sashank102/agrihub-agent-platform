"""In-process durable run execution, replay, and cancellation.

Consistency boundary: this manager keeps task handles and subscriber queues
in one process. PostgreSQL is the replay log. A second Uvicorn worker cannot
observe those queues. Startup does not resume model or tool calls. It settles
leftover pending and running rows from durable reconciliation intent or the
latest LangGraph checkpoint. A completed checkpoint stays completed. An
unknown outcome is interrupted with an explicit reconciliation reason. No
intent can be stored while PostgreSQL itself is unavailable.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi.encoders import jsonable_encoder
from langgraph.errors import GraphInterrupt, NodeCancelledError
from sqlalchemy.exc import IntegrityError

from agent_platform.api.schemas import RunStreamRequest
from agent_platform.db.models.run import TERMINAL_RUN_STATUSES, Run
from agent_platform.db.repositories import (
    RunEventRepository,
    RunRepository,
    ThreadRepository,
)
from agent_platform.db.repositories.audit_log import AuditLogRepository
from agent_platform.db.repositories.runs import ActiveRunRecord
from agent_platform.db.session import AsyncSessionFactory, session_scope
from agent_platform.services.errors import (
    ActiveRunConflict,
    CancellationNotSettled,
    UnsupportedRunOption,
)
from agent_platform.services.graph_registry import GraphRegistry
from agent_platform.services.run_fields import (
    astream_options,
    checkpoint_reference,
    disconnect_policy,
    ensure_checkpoint_belongs_to_thread,
    execution_config,
    graph_context,
    graph_input_for,
    run_configuration,
    validate_run_stream_request,
)
from agent_platform.services.snapshots import (
    snapshot_interrupted,
    values_with_interrupts,
)
from agent_platform.services.tenant_context import TenantIdentity, tenant_user_id

logger = logging.getLogger(__name__)

TERMINAL_EVENT_TYPES = frozenset({"end", "error"})
READ_BATCH = 200


@dataclass(frozen=True, slots=True)
class RunView:
    """Detached run fields safe to return after a transaction commits."""

    id: uuid.UUID
    thread_id: uuid.UUID
    agent_id: uuid.UUID
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancellation_requested: bool
    error_code: str | None
    error_message: str | None

    @classmethod
    def from_run(cls, run: Any) -> "RunView":
        """Copy loaded columns before the session closes."""
        return cls(
            id=run.id,
            thread_id=run.thread_id,
            agent_id=run.agent_id,
            status=run.status,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            cancellation_requested=bool(run.cancellation_requested),
            error_code=run.error_code,
            error_message=run.error_message,
        )


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One durable event that can be replayed or delivered live."""

    sequence: int
    event_type: str
    payload: dict[str, Any]
    terminal: bool = False


@dataclass
class _Subscriber:
    """A bounded queue for one SSE consumer.

    ``lagging`` means the queue overflowed. The consumer catches up from
    PostgreSQL. Overflow is not a disconnect and must not cancel the run.
    """

    queue: asyncio.Queue
    lagging: bool = False
    cancel_on_disconnect: bool = False


@dataclass
class _ThreadLockSlot:
    """A per-thread lock plus the coroutines holding or waiting for it."""

    lock: asyncio.Lock
    refs: int


@dataclass
class _PendingTerminal:
    """A terminal write that has not yet been confirmed in PostgreSQL."""

    run_id: uuid.UUID
    owner_user_id: uuid.UUID
    thread_id: uuid.UUID
    status: str
    status_kwargs: dict[str, Any]
    event_type: str
    event_payload: dict[str, Any]
    graph_succeeded: bool
    status_confirmed: bool = False


@dataclass
class _ActiveRun:
    """Process-local state for one executing run."""

    task: asyncio.Task | None
    owner_user_id: uuid.UUID
    thread_id: uuid.UUID
    on_disconnect: str
    subscribers: list[_Subscriber] = field(default_factory=list)
    cancel_requested: bool = False


class RunManager:
    """Own in-process run tasks and publish only events already committed."""

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
        registry: GraphRegistry,
        *,
        max_concurrent_runs: int,
        subscriber_queue_size: int = 16,
        terminal_retry_delays: tuple[float, ...] = (0.0, 0.05),
    ) -> None:
        """Create an empty task registry and the process-wide run semaphore."""
        self.session_factory = session_factory
        self.registry = registry
        self.subscriber_queue_size = subscriber_queue_size
        self._terminal_retry_delays = terminal_retry_delays or (0.0,)
        self._semaphore = asyncio.Semaphore(max_concurrent_runs)
        self._lock = asyncio.Lock()
        self._thread_locks: dict[uuid.UUID, _ThreadLockSlot] = {}
        self._runs: dict[uuid.UUID, _ActiveRun] = {}
        self._pending_terminals: dict[uuid.UUID, _PendingTerminal] = {}
        self._reconcile_lock = asyncio.Lock()
        self._reconcile_task: asyncio.Task | None = None
        self._shutting_down = False
        self._closed = False

    async def reconcile_orphaned_runs(self, *, reason: str) -> int:
        """Settle leftover pending and running rows without invoking the graph.

        ``process_restart`` is used at startup. ``shutdown`` covers rows that
        were still active after registered tasks were cancelled. In-memory
        terminal retries are left for ``reconcile_pending``. Every other
        leftover row is settled from durable intent or the latest checkpoint.
        """
        async with session_scope(self.session_factory) as session:
            records = await RunRepository(session).list_active_records()
        async with self._lock:
            pending_ids = set(self._pending_terminals)
        changed = 0
        for record in records:
            if record.id in pending_ids or await self._genuinely_active(record.id):
                continue
            if await self._settle_record(record, fallback_reason=reason):
                changed += 1
        await self.repair_terminal_events()
        return changed

    async def repair_terminal_events(self) -> int:
        """Append the one missing terminal event for each terminal run."""
        async with session_scope(self.session_factory) as session:
            runs = await RunRepository(session).list_missing_terminal_events()
            pending = [
                (
                    run.id,
                    run.thread_id,
                    run.status,
                    dict(run.error_details or {}),
                    run.error_code,
                    run.error_message,
                )
                for run in runs
            ]
        repaired = 0
        for run_id, thread_id, status, details, error_code, error_message in pending:
            if await self._backfill_terminal_event(
                run_id=run_id,
                thread_id=thread_id,
                status=status,
                error_details=details,
                error_code=error_code,
                error_message=error_message,
            ):
                repaired += 1
        return repaired

    async def shutdown(self) -> None:
        """Cancel registered tasks and interrupt any rows that remain active."""
        if self._closed:
            return
        self._shutting_down = True
        async with self._lock:
            tasks = [
                entry.task
                for entry in self._runs.values()
                if entry.task is not None and not entry.task.done()
            ]
            for entry in self._runs.values():
                entry.cancel_requested = True
                if entry.task is not None and not entry.task.done():
                    entry.task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Run task failed while shutting down")
        await self.reconcile_pending()
        reconcile_task = self._reconcile_task
        if reconcile_task is not None and not reconcile_task.done():
            reconcile_task.cancel()
            try:
                await reconcile_task
            except asyncio.CancelledError:
                pass
        await self.reconcile_orphaned_runs(reason="shutdown")
        self._closed = True

    async def start_run(
        self,
        thread_id: uuid.UUID,
        request: RunStreamRequest,
        owner_user_id: uuid.UUID,
    ) -> RunView:
        """Validate, commit a pending run, and execute it independently of HTTP."""
        validate_run_stream_request(request)
        registered = self.registry.resolve(request.assistant_id)
        if registered is None:
            raise KeyError("assistant not found")

        if not await self._thread_visible(thread_id, owner_user_id, registered.agent_id):
            raise LookupError("thread not found")

        reference = checkpoint_reference(thread_id, request)
        await ensure_checkpoint_belongs_to_thread(
            registered.graph,
            thread_id,
            reference,
        )
        interrupted = await self._thread_is_interrupted(
            registered.graph,
            thread_id,
        )
        graph_input = graph_input_for(request, interrupted=interrupted)
        options = astream_options(request)

        await self.reconcile_pending()
        async with self._thread_guard(thread_id):
            await self._release_stale_active(thread_id, owner_user_id)
            active_to_settle: uuid.UUID | None = None
            async with session_scope(self.session_factory) as session:
                repository = ThreadRepository(session)
                thread = await repository.get_for_owner(thread_id, owner_user_id)
                if thread is None or thread.status == "deleted":
                    raise LookupError("thread not found")
                if thread.agent_id != registered.agent_id:
                    raise LookupError("thread not found")
                active = await RunRepository(session).find_active_id(
                    thread_id,
                    owner_user_id,
                )
                if active is not None and await self._genuinely_active(active):
                    raise await self._active_run_conflict(
                        thread_id,
                        owner_user_id,
                        active,
                    )
                if active is not None:
                    active_to_settle = active
            if active_to_settle is not None:
                await self._settle_active_id(
                    active_to_settle,
                    fallback_reason="stale_active_run",
                )
                async with session_scope(self.session_factory) as session:
                    active = await RunRepository(session).find_active_id(
                        thread_id,
                        owner_user_id,
                    )
                if active is not None:
                    raise await self._active_run_conflict(
                        thread_id,
                        owner_user_id,
                        active,
                    )
            run_id = uuid.uuid4()
            await self._reserve_run(
                run_id,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                on_disconnect=disconnect_policy(request),
            )
            try:
                async with session_scope(self.session_factory) as session:
                    repository = ThreadRepository(session)
                    thread = await repository.get_for_owner(thread_id, owner_user_id)
                    if thread is None or thread.status == "deleted":
                        raise LookupError("thread not found")
                    if thread.agent_id != registered.agent_id:
                        raise LookupError("thread not found")
                    active = await RunRepository(session).find_active_id(
                        thread_id,
                        owner_user_id,
                    )
                    if active is not None and active != run_id:
                        raise await self._active_run_conflict(
                            thread_id,
                            owner_user_id,
                            active,
                        )
                    run, _created = await RunRepository(session).create_idempotent(
                        owner_user_id=owner_user_id,
                        thread_id=thread_id,
                        agent_id=registered.agent_id,
                        input=request.input,
                        configuration=run_configuration(request),
                        run_id=run_id,
                    )
                    await AuditLogRepository(session).append(
                        actor_user_id=owner_user_id,
                        action="run.created",
                        resource_type="run",
                        resource_id=str(run.id),
                        metadata={"thread_id": str(thread_id)},
                    )
                    view = RunView.from_run(run)
            except IntegrityError as exc:
                await self._discard_reservation(run_id)
                if not _is_active_run_uniqueness(exc):
                    raise
                raise await self._active_run_conflict(
                    thread_id,
                    owner_user_id,
                    None,
                ) from exc
            except Exception:
                await self._discard_reservation(run_id)
                raise

            identity = TenantIdentity(
                user_id=str(owner_user_id),
                thread_id=str(thread_id),
                run_id=str(view.id),
            )
            config = execution_config(thread_id, request, identity)
            options = {**options, "context": graph_context(request, identity)}
            await self._before_task_attach(view.id)
            if await self._cancellation_requested(view.id):
                await self._mark_cancelled(view.id, owner_user_id, thread_id)
                await self._discard_reservation(view.id)
                refreshed = await self._visible_run(thread_id, view.id, owner_user_id)
                return refreshed or view
            try:
                await self._register_task(
                    view=view,
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    on_disconnect=disconnect_policy(request),
                    graph=registered.graph,
                    graph_input=graph_input,
                    config=config,
                    options=options,
                    subgraphs=bool(request.stream_subgraphs),
                )
            except Exception:
                await self._discard_reservation(view.id)
                raise
        return view

    async def get_owned_run(
        self,
        thread_id: uuid.UUID,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> RunView:
        """Return one owned run or raise ``LookupError`` when it is inaccessible."""
        view = await self._visible_run(thread_id, run_id, owner_user_id)
        if view is None:
            raise LookupError("run not found")
        return view

    async def stream_events(
        self,
        *,
        thread_id: uuid.UUID,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        after_sequence: int,
        cancel_on_disconnect: bool = False,
    ) -> AsyncIterator[bytes]:
        """Replay committed events and follow the live queue while the run is active."""
        if await self._visible_run(thread_id, run_id, owner_user_id) is None:
            raise LookupError("run not found")
        async for event in self._iterate(
            run_id=run_id,
            owner_user_id=owner_user_id,
            after_sequence=after_sequence,
            cancel_on_disconnect=cancel_on_disconnect,
        ):
            yield _encode_sse(event)

    async def cancel_run(
        self,
        thread_id: uuid.UUID,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        *,
        action: str,
    ) -> dict[str, Any]:
        """Request cancellation and wait until a terminal status is durable."""
        if action != "interrupt":
            raise UnsupportedRunOption(
                "cancel action only supports interrupt; rollback is not supported"
            )
        current = await self._visible_run(thread_id, run_id, owner_user_id)
        if current is None:
            raise LookupError("run not found")
        if current.status in TERMINAL_RUN_STATUSES:
            return self._cancel_payload(current)

        async with session_scope(self.session_factory) as session:
            await RunRepository(session).request_cancellation_for_owner(
                run_id,
                owner_user_id,
            )
            await AuditLogRepository(session).append(
                actor_user_id=owner_user_id,
                action="run.cancelled",
                resource_type="run",
                resource_id=str(run_id),
                metadata={"thread_id": str(thread_id)},
            )
        async with self._lock:
            entry = self._runs.get(run_id)
            if entry is not None:
                entry.cancel_requested = True
        task = await self._task_for_cancel(run_id)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Run %s failed while cancelling", run_id)
        else:
            pending = await self._pending_for(run_id)
            if pending is not None and pending.graph_succeeded:
                await self.reconcile_pending()
            else:
                await self._mark_cancelled(run_id, owner_user_id, thread_id)
                await self._discard_reservation(run_id)

        refreshed = await self._visible_run(thread_id, run_id, owner_user_id)
        if refreshed is None:
            raise LookupError("run not found")
        if refreshed.status in {"pending", "running"}:
            await self._mark_cancelled(run_id, owner_user_id, thread_id)
            await self._discard_reservation(run_id)
            refreshed = await self._visible_run(thread_id, run_id, owner_user_id)
            if refreshed is None:
                raise LookupError("run not found")
        if refreshed.status in {"pending", "running"}:
            raise CancellationNotSettled(str(run_id), refreshed.status)
        return self._cancel_payload(refreshed)

    async def persist_and_publish(
        self,
        *,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        terminal: bool = False,
    ) -> StreamEvent:
        """Commit one event, then deliver that committed sequence to subscribers."""
        async with session_scope(self.session_factory) as session:
            row = await RunEventRepository(session).append(
                run_id=run_id,
                owner_user_id=owner_user_id,
                event_type=event_type,
                payload=payload,
            )
            event = StreamEvent(
                sequence=int(row.sequence),
                event_type=row.event_type,
                payload=dict(row.payload or {}),
                terminal=terminal or row.event_type in TERMINAL_EVENT_TYPES,
            )
        await self._publish(run_id, event)
        return event

    async def _execute(
        self,
        *,
        run_id: uuid.UUID,
        thread_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        graph: Any,
        graph_input: Any,
        config: dict[str, Any],
        options: dict[str, Any],
        subgraphs: bool,
    ) -> None:
        finalized = False
        outcome_chosen = False
        graph_completed = False
        paused = False
        latest: Any = None
        tenant_token = tenant_user_id.set(str(owner_user_id))

        async def choose_success() -> None:
            nonlocal finalized, outcome_chosen
            if finalized or outcome_chosen:
                return
            outcome_chosen = True
            finalized = await self._commit_terminal(
                run_id=run_id,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                status="completed",
                status_kwargs={
                    "finished_at": datetime.now(UTC),
                    "output_summary": _output_summary(latest),
                    "touch_thread": True,
                },
                event_type="end",
                event_payload={"status": "success"},
                graph_succeeded=True,
            )

        async def choose_interrupted() -> None:
            nonlocal finalized, outcome_chosen
            if finalized or outcome_chosen:
                return
            outcome_chosen = True
            finalized = await self._commit_terminal(
                run_id=run_id,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                status="interrupted",
                status_kwargs={
                    "finished_at": datetime.now(UTC),
                    "error_code": "graph_interrupt",
                    "error_message": "Run interrupted",
                    "touch_thread": True,
                },
                event_type="end",
                event_payload={"status": "interrupted"},
                graph_succeeded=False,
            )

        async def choose_cancelled() -> None:
            nonlocal finalized, outcome_chosen
            if finalized or outcome_chosen:
                return
            outcome_chosen = True
            finalized = await self._commit_terminal(
                run_id=run_id,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                status="cancelled",
                status_kwargs={
                    "finished_at": datetime.now(UTC),
                    "error_code": "cancelled",
                    "error_message": "Run cancelled",
                    "cancellation_requested": True,
                    "touch_thread": True,
                },
                event_type="end",
                event_payload={"status": "cancelled"},
                graph_succeeded=False,
            )

        async def choose_failed(exc: BaseException) -> None:
            nonlocal finalized, outcome_chosen
            if finalized or outcome_chosen:
                return
            outcome_chosen = True
            finalized = await self._commit_terminal(
                run_id=run_id,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                status="failed",
                status_kwargs={
                    "finished_at": datetime.now(UTC),
                    "error_code": type(exc).__name__,
                    "error_message": "Graph execution failed",
                    "touch_thread": True,
                },
                event_type="error",
                event_payload={
                    "error": "run_failed",
                    "message": "Graph execution failed",
                    "run_id": str(run_id),
                    "thread_id": str(thread_id),
                },
                graph_succeeded=False,
            )

        try:
            if await self._cancellation_requested(run_id) or self._shutting_down:
                await choose_cancelled()
                return
            await self.persist_and_publish(
                run_id=run_id,
                owner_user_id=owner_user_id,
                event_type="metadata",
                payload={"run_id": str(run_id), "thread_id": str(thread_id)},
            )
            if await self._cancellation_requested(run_id) or self._shutting_down:
                await choose_cancelled()
                return
            async with self._semaphore:
                written = await self._write_status(
                    run_id,
                    owner_user_id,
                    "running",
                    started_at=datetime.now(UTC),
                )
                if written != "running":
                    if written not in {"completed", "failed", "interrupted"}:
                        await choose_cancelled()
                    return
                await self._before_graph_execution(run_id)
                if await self._cancellation_requested(run_id) or self._shutting_down:
                    await choose_cancelled()
                    return
                async for chunk in graph.astream(graph_input, config, **options):
                    if await self._cancellation_requested(run_id):
                        raise asyncio.CancelledError()
                    namespace, data = _unwrap_chunk(chunk, subgraphs)
                    latest = data
                    event_type = "values"
                    if namespace:
                        event_type = "values|" + "|".join(str(part) for part in namespace)
                    await self.persist_and_publish(
                        run_id=run_id,
                        owner_user_id=owner_user_id,
                        event_type=event_type,
                        payload=_json_object(data),
                    )
                graph_completed = True
                snapshot = await graph.aget_state(config)
                paused = snapshot_interrupted(snapshot)
            if paused:
                await self.persist_and_publish(
                    run_id=run_id,
                    owner_user_id=owner_user_id,
                    event_type="values",
                    payload=_json_object(values_with_interrupts(snapshot)),
                )
                await choose_interrupted()
            else:
                await choose_success()
        except asyncio.CancelledError:
            if paused:
                await choose_interrupted()
            elif graph_completed:
                await choose_success()
            else:
                await choose_cancelled()
            raise
        except GraphInterrupt:
            paused = True
            await choose_interrupted()
        except NodeCancelledError as exc:
            if await self._cancellation_requested(run_id) or self._shutting_down:
                await choose_cancelled()
            else:
                await choose_failed(exc)
        except Exception as exc:
            if paused:
                await choose_interrupted()
            elif graph_completed:
                await choose_success()
            else:
                logger.exception("Graph run %s failed", run_id)
                await choose_failed(exc)
        finally:
            tenant_user_id.reset(tenant_token)
            try:
                if not outcome_chosen:
                    if paused:
                        await choose_interrupted()
                    elif graph_completed:
                        await choose_success()
                    else:
                        await choose_cancelled()
            except Exception:
                logger.exception("Could not finalize run %s", run_id)
            finally:
                current = asyncio.current_task()
                async with self._lock:
                    entry = self._runs.get(run_id)
                    if entry is not None and entry.task is current:
                        self._runs.pop(run_id, None)

    async def _commit_terminal(
        self,
        *,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        thread_id: uuid.UUID,
        status: str,
        status_kwargs: dict[str, Any],
        event_type: str,
        event_payload: dict[str, Any],
        graph_succeeded: bool,
    ) -> bool:
        """Retry a terminal write. Return true only after PostgreSQL confirms it."""
        delays = self._terminal_retry_delays
        last_error: Exception | None = None
        persistence_retry = False
        await self._record_reconciliation_intent(
            run_id=run_id,
            owner_user_id=owner_user_id,
            status=status,
            event_type=event_type,
            event_payload=event_payload,
            graph_succeeded=graph_succeeded,
            reason=str(status_kwargs.get("error_code") or status),
        )
        for index, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            kwargs = dict(status_kwargs)
            if index and status == "completed":
                summary = dict(kwargs.get("output_summary") or {})
                summary["persistence_reconciled"] = True
                kwargs["output_summary"] = summary
                persistence_retry = True
            try:
                written = await self._write_status(
                    run_id,
                    owner_user_id,
                    status,
                    **_with_reconciliation(
                        kwargs,
                        status=status,
                        event_type=_terminal_event_type(
                            event_type,
                            graph_succeeded=graph_succeeded,
                            persistence_retry=persistence_retry,
                        ),
                        event_payload=_terminal_event_payload(
                            event_payload,
                            run_id=run_id,
                            thread_id=thread_id,
                            graph_succeeded=graph_succeeded,
                            persistence_retry=persistence_retry,
                        ),
                        graph_succeeded=graph_succeeded,
                    ),
                )
                confirmed = await self._run_status(run_id, owner_user_id)
            except Exception as exc:
                last_error = exc
                continue
            if written != status or confirmed != status:
                last_error = RuntimeError("terminal status was not confirmed")
                continue
            published = await self._publish_terminal_event(
                run_id=run_id,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                event_type=event_type,
                event_payload=event_payload,
                graph_succeeded=graph_succeeded,
                persistence_retry=persistence_retry,
            )
            if published:
                await self._drop_pending(run_id)
                return True
            await self._remember_pending(
                _PendingTerminal(
                    run_id=run_id,
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    status=status,
                    status_kwargs=kwargs,
                    event_type=_terminal_event_type(
                        event_type,
                        graph_succeeded=graph_succeeded,
                        persistence_retry=persistence_retry,
                    ),
                    event_payload=_terminal_event_payload(
                        event_payload,
                        run_id=run_id,
                        thread_id=thread_id,
                        graph_succeeded=graph_succeeded,
                        persistence_retry=persistence_retry,
                    ),
                    graph_succeeded=graph_succeeded,
                    status_confirmed=True,
                )
            )
            self._log_persistence_failure(
                run_id,
                thread_id,
                status,
                graph_succeeded,
                last_error or RuntimeError("terminal event was not published"),
            )
            return True
        kwargs = dict(status_kwargs)
        if status == "completed":
            summary = dict(kwargs.get("output_summary") or {})
            summary["persistence_reconciled"] = True
            kwargs["output_summary"] = summary
            persistence_retry = True
        await self._remember_pending(
            _PendingTerminal(
                run_id=run_id,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                status=status,
                status_kwargs=kwargs,
                event_type=_terminal_event_type(
                    event_type,
                    graph_succeeded=graph_succeeded,
                    persistence_retry=persistence_retry,
                ),
                event_payload=_terminal_event_payload(
                    event_payload,
                    run_id=run_id,
                    thread_id=thread_id,
                    graph_succeeded=graph_succeeded,
                    persistence_retry=persistence_retry,
                ),
                graph_succeeded=graph_succeeded,
                status_confirmed=False,
            )
        )
        self._log_persistence_failure(
            run_id,
            thread_id,
            status,
            graph_succeeded,
            last_error or RuntimeError("terminal status was not confirmed"),
        )
        return False

    async def _publish_terminal_event(
        self,
        *,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        thread_id: uuid.UUID,
        event_type: str,
        event_payload: dict[str, Any],
        graph_succeeded: bool,
        persistence_retry: bool,
    ) -> bool:
        event_name = _terminal_event_type(
            event_type,
            graph_succeeded=graph_succeeded,
            persistence_retry=persistence_retry,
        )
        payload = _terminal_event_payload(
            event_payload,
            run_id=run_id,
            thread_id=thread_id,
            graph_succeeded=graph_succeeded,
            persistence_retry=persistence_retry,
        )
        try:
            event, created = await self._append_terminal_event(
                run_id=run_id,
                event_type=event_name,
                payload=payload,
            )
        except Exception:
            return False
        if created:
            await self._publish(run_id, event)
        return True

    async def reconcile_pending(self) -> int:
        """Retry queued terminal writes. Applied rows leave the queue."""
        async with self._reconcile_lock:
            async with self._lock:
                items = list(self._pending_terminals.values())
            applied = 0
            for item in items:
                async with self._lock:
                    if self._pending_terminals.get(item.run_id) is not item:
                        continue
                if not await self._apply_pending(item):
                    continue
                async with self._lock:
                    current = self._pending_terminals.get(item.run_id)
                    if current is item:
                        self._pending_terminals.pop(item.run_id, None)
                applied += 1
            return applied

    async def _apply_pending(self, item: _PendingTerminal) -> bool:
        try:
            if not item.status_confirmed:
                written = await self._write_status(
                    item.run_id,
                    item.owner_user_id,
                    item.status,
                    **item.status_kwargs,
                )
                confirmed = await self._run_status(item.run_id, item.owner_user_id)
                if written != item.status or confirmed != item.status:
                    return False
                item.status_confirmed = True
            event, created = await self._append_terminal_event(
                run_id=item.run_id,
                event_type=item.event_type,
                payload=dict(item.event_payload),
            )
            if created:
                await self._publish(item.run_id, event)
        except Exception as exc:
            self._log_persistence_failure(
                item.run_id,
                item.thread_id,
                item.status,
                item.graph_succeeded,
                exc,
            )
            return False
        return True

    async def _remember_pending(self, item: _PendingTerminal) -> None:
        async with self._lock:
            self._pending_terminals[item.run_id] = item
        self._schedule_reconcile()

    async def _drop_pending(self, run_id: uuid.UUID) -> None:
        async with self._lock:
            self._pending_terminals.pop(run_id, None)

    async def _pending_for(self, run_id: uuid.UUID) -> _PendingTerminal | None:
        async with self._lock:
            return self._pending_terminals.get(run_id)

    def _schedule_reconcile(self) -> None:
        task = self._reconcile_task
        if task is not None and not task.done():
            return
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(),
            name="run-reconciliation",
        )

    async def _reconcile_loop(self) -> None:
        delay = 0.05
        while not self._closed and not self._shutting_down:
            async with self._lock:
                pending = bool(self._pending_terminals)
            if not pending:
                return
            try:
                await self.reconcile_pending()
            except Exception:
                logger.exception("terminal reconciliation failed")
            async with self._lock:
                if not self._pending_terminals:
                    return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 1.0)

    def _log_persistence_failure(
        self,
        run_id: uuid.UUID,
        thread_id: uuid.UUID,
        status: str,
        graph_succeeded: bool,
        exc: BaseException,
    ) -> None:
        logger.error(
            "terminal_persistence_unconfirmed run_id=%s thread_id=%s "
            "target_status=%s graph_succeeded=%s error_code=persistence_failed "
            "error_type=%s",
            run_id,
            thread_id,
            status,
            graph_succeeded,
            type(exc).__name__,
        )

    async def _mark_cancelled(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        thread_id: uuid.UUID,
    ) -> bool:
        pending = await self._pending_for(run_id)
        if pending is not None and pending.graph_succeeded:
            await self.reconcile_pending()
            status = await self._run_status(run_id, owner_user_id)
            return status in TERMINAL_RUN_STATUSES if status is not None else False
        return await self._commit_terminal(
            run_id=run_id,
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            status="cancelled",
            status_kwargs={
                "finished_at": datetime.now(UTC),
                "error_code": "cancelled",
                "error_message": "Run cancelled",
                "cancellation_requested": True,
                "touch_thread": True,
            },
            event_type="end",
            event_payload={"status": "cancelled"},
            graph_succeeded=False,
        )

    async def _write_status(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        status: str,
        **kwargs: Any,
    ) -> str:
        async with session_scope(self.session_factory) as session:
            run = await RunRepository(session).set_status_for_owner(
                run_id,
                owner_user_id,
                status,
                **kwargs,
            )
            if run is None:
                raise RuntimeError("owned run disappeared before its status was saved")
            return run.status

    async def _cancellation_requested(self, run_id: uuid.UUID) -> bool:
        async with self._lock:
            entry = self._runs.get(run_id)
            if entry is not None and entry.cancel_requested:
                return True
        async with session_scope(self.session_factory) as session:
            run = await session.get(Run, run_id)
            if run is None:
                return False
            return bool(run.cancellation_requested) or run.status == "cancelled"

    async def _before_task_attach(self, run_id: uuid.UUID) -> None:
        """Pause point after the run row is visible and before the task exists."""
        return None

    async def _before_graph_execution(self, run_id: uuid.UUID) -> None:
        """Pause point immediately before the first graph invocation."""
        return None

    async def _genuinely_active(self, run_id: uuid.UUID) -> bool:
        """Return whether this process is executing or about to execute the run."""
        async with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return False
            if entry.task is None:
                return True
            return not entry.task.done()

    async def _active_run_conflict(
        self,
        thread_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        run_id: uuid.UUID | None,
    ) -> ActiveRunConflict:
        """Build a conflict that reports durable status without a second active row."""
        status: str | None = None
        intent_name: str | None = None
        graph_succeeded: bool | None = None
        visible_id = run_id
        async with session_scope(self.session_factory) as session:
            repository = RunRepository(session)
            if visible_id is None:
                visible_id = await repository.find_active_id(thread_id, owner_user_id)
            if visible_id is not None:
                run = await repository.get_for_owner(visible_id, owner_user_id)
                if run is not None:
                    status = run.status
                    intent = _intent_from_details(dict(run.error_details or {}))
                    if intent is not None:
                        intent_name = str(intent.get("intent") or "") or None
                        if "graph_succeeded" in intent:
                            graph_succeeded = bool(intent.get("graph_succeeded"))
        logger.warning(
            "admission rejected thread_id=%s active_run_id=%s status=%s "
            "reconciliation_intent=%s graph_succeeded=%s",
            thread_id,
            visible_id,
            status,
            intent_name,
            graph_succeeded,
        )
        return ActiveRunConflict(
            run_id=None if visible_id is None else str(visible_id),
            status=status,
            reconciliation_intent=intent_name,
            graph_succeeded=graph_succeeded,
        )

    async def _reserve_run(
        self,
        run_id: uuid.UUID,
        *,
        owner_user_id: uuid.UUID,
        thread_id: uuid.UUID,
        on_disconnect: str,
    ) -> None:
        """Publish the run to cancellation before its row is committed."""
        async with self._lock:
            if self._shutting_down or self._closed:
                raise RuntimeError("run manager is shutting down")
            self._runs[run_id] = _ActiveRun(
                task=None,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                on_disconnect=on_disconnect,
            )

    async def _discard_reservation(self, run_id: uuid.UUID) -> None:
        async with self._lock:
            entry = self._runs.get(run_id)
            if entry is not None and entry.task is None:
                self._runs.pop(run_id, None)

    async def _settle_active_id(self, run_id: uuid.UUID, *, fallback_reason: str) -> bool:
        """Settle one inactive row from intent or its checkpoint."""
        if await self._genuinely_active(run_id):
            return False
        async with session_scope(self.session_factory) as session:
            run = await session.get(Run, run_id)
            if run is None or run.status not in {"pending", "running"}:
                return False
            thread = await ThreadRepository(session).get_by_id(run.thread_id)
            if thread is None:
                return False
            record = ActiveRunRecord(
                id=run.id,
                thread_id=run.thread_id,
                agent_id=run.agent_id,
                owner_user_id=thread.owner_user_id,
                error_details=dict(run.error_details or {}),
                cancellation_requested=bool(run.cancellation_requested),
                status=run.status,
            )
        return await self._settle_record(record, fallback_reason=fallback_reason)

    async def _settle_record(
        self,
        record: ActiveRunRecord,
        *,
        fallback_reason: str,
    ) -> bool:
        intent: dict[str, Any] | None
        if record.cancellation_requested:
            intent = {
                "intent": "cancelled",
                "reason": "cancelled",
                "event_type": "end",
                "event_payload": {"status": "cancelled"},
                "graph_succeeded": False,
            }
        else:
            intent = _intent_from_details(record.error_details)
        checkpoint = await self._checkpoint_outcome(record.thread_id, record.agent_id)
        decision = decide_orphan_outcome(
            checkpoint=checkpoint,
            intent=intent,
            fallback_reason=fallback_reason,
        )
        await self._record_reconciliation_intent(
            run_id=record.id,
            owner_user_id=record.owner_user_id,
            status=decision.status,
            event_type=decision.event_type,
            event_payload=decision.event_payload,
            graph_succeeded=decision.graph_succeeded,
            reason=decision.error_code or decision.status,
        )
        try:
            async with session_scope(self.session_factory) as session:
                repository = RunRepository(session)
                status_kwargs: dict[str, Any] = {
                    "finished_at": datetime.now(UTC),
                    "error_details": {
                        "reconciliation": {
                            "intent": decision.status,
                            "reason": decision.error_code or decision.status,
                            "event_type": decision.event_type,
                            "event_payload": decision.event_payload,
                            "graph_succeeded": decision.graph_succeeded,
                        }
                    },
                }
                if decision.error_code is not None:
                    status_kwargs["error_code"] = decision.error_code
                    status_kwargs["error_message"] = decision.error_message
                if decision.status == "completed":
                    status_kwargs["output_summary"] = {
                        "reconciliation": checkpoint
                        if checkpoint == "completed"
                        else "durable_intent"
                    }
                if decision.status == "cancelled":
                    status_kwargs["cancellation_requested"] = True
                run = await repository.set_status_internal(
                    record.id,
                    decision.status,
                    only_active=True,
                    **status_kwargs,
                )
                if run is None or run.status != decision.status:
                    return False
                await RunEventRepository(session).append_terminal_if_absent(
                    run_id=record.id,
                    event_type=decision.event_type,
                    payload=decision.event_payload,
                )
        except Exception:
            logger.exception("Could not settle run %s", record.id)
            await self._remember_pending(
                _PendingTerminal(
                    run_id=record.id,
                    owner_user_id=record.owner_user_id,
                    thread_id=record.thread_id,
                    status=decision.status,
                    status_kwargs={
                        "finished_at": datetime.now(UTC),
                        "error_code": decision.error_code,
                        "error_message": decision.error_message,
                        "touch_thread": True,
                    },
                    event_type=decision.event_type,
                    event_payload=dict(decision.event_payload),
                    graph_succeeded=decision.graph_succeeded,
                    status_confirmed=False,
                )
            )
            return False
        return True

    async def _checkpoint_outcome(self, thread_id: uuid.UUID, agent_id: uuid.UUID) -> str:
        """Classify the latest checkpoint as completed, interrupted, or unknown."""
        graph = self.registry.graph_for_agent(agent_id)
        if graph is None:
            return "unknown"
        config = {
            "configurable": {
                "thread_id": str(thread_id),
                "checkpoint_ns": "",
            }
        }
        checkpointer = getattr(graph, "checkpointer", None)
        if checkpointer is None:
            return "unknown"
        try:
            saved = await checkpointer.aget_tuple(config)
        except Exception:
            logger.exception("Could not read the checkpoint for thread %s", thread_id)
            return "unknown"
        if saved is None:
            return "unknown"
        try:
            snapshot = await graph.aget_state(config)
        except Exception:
            logger.exception("Could not read checkpoint state for thread %s", thread_id)
            return "unknown"
        if snapshot_interrupted(snapshot):
            return "interrupted"
        if tuple(getattr(snapshot, "next", ()) or ()):
            return "unknown"
        return "completed"

    async def _append_terminal_event(
        self,
        *,
        run_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> tuple[StreamEvent, bool]:
        async with session_scope(self.session_factory) as session:
            row, created = await RunEventRepository(session).append_terminal_if_absent(
                run_id=run_id,
                event_type=event_type,
                payload=payload,
            )
            event = StreamEvent(
                sequence=int(row.sequence),
                event_type=row.event_type,
                payload=dict(row.payload or {}),
                terminal=True,
            )
        return event, created

    async def _record_reconciliation_intent(
        self,
        *,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        status: str,
        event_type: str,
        event_payload: dict[str, Any],
        graph_succeeded: bool,
        reason: str,
    ) -> None:
        """Persist terminal intent while the run is still active, when possible."""
        try:
            async with session_scope(self.session_factory) as session:
                await RunRepository(session).merge_reconciliation(
                    run_id,
                    owner_user_id,
                    {
                        "intent": status,
                        "reason": reason,
                        "event_type": event_type,
                        "event_payload": event_payload,
                        "graph_succeeded": graph_succeeded,
                    },
                )
        except Exception:
            logger.exception("Could not store reconciliation intent for run %s", run_id)

    async def _repair_one_terminal_event(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> None:
        async with session_scope(self.session_factory) as session:
            run = await RunRepository(session).get_for_owner(run_id, owner_user_id)
            if run is None or run.status not in TERMINAL_RUN_STATUSES:
                return
            snapshot = (
                run.id,
                run.thread_id,
                run.status,
                dict(run.error_details or {}),
                run.error_code,
                run.error_message,
            )
        await self._backfill_terminal_event(
            run_id=snapshot[0],
            thread_id=snapshot[1],
            status=snapshot[2],
            error_details=snapshot[3],
            error_code=snapshot[4],
            error_message=snapshot[5],
        )

    async def _backfill_terminal_event(
        self,
        *,
        run_id: uuid.UUID,
        thread_id: uuid.UUID,
        status: str,
        error_details: dict[str, Any],
        error_code: str | None,
        error_message: str | None,
    ) -> bool:
        event_type, payload = _terminal_event_for_status(
            status=status,
            error_details=error_details,
            error_code=error_code,
            error_message=error_message,
            thread_id=thread_id,
            run_id=run_id,
        )
        try:
            event, created = await self._append_terminal_event(
                run_id=run_id,
                event_type=event_type,
                payload=payload,
            )
        except Exception:
            logger.exception("Could not backfill the terminal event for run %s", run_id)
            return False
        if created:
            await self._publish(run_id, event)
        return created

    async def _thread_is_interrupted(self, graph: Any, thread_id: uuid.UUID) -> bool:
        snapshot = await graph.aget_state(
            {
                "configurable": {
                    "thread_id": str(thread_id),
                    "checkpoint_ns": "",
                }
            }
        )
        return snapshot_interrupted(snapshot)

    async def _register_task(
        self,
        *,
        view: RunView,
        owner_user_id: uuid.UUID,
        thread_id: uuid.UUID,
        on_disconnect: str,
        graph: Any,
        graph_input: Any,
        config: dict[str, Any],
        options: dict[str, Any],
        subgraphs: bool,
    ) -> None:
        """Attach the task to the reservation created before the row was visible."""
        scheduling_error: BaseException | None = None
        async with self._lock:
            if self._shutting_down or self._closed:
                scheduling_error = RuntimeError("run manager is shutting down")
            else:
                entry = self._runs.get(view.id)
                if entry is None:
                    entry = _ActiveRun(
                        task=None,
                        owner_user_id=owner_user_id,
                        thread_id=thread_id,
                        on_disconnect=on_disconnect,
                    )
                    self._runs[view.id] = entry
                if entry.cancel_requested:
                    scheduling_error = RuntimeError("cancelled before start")
                else:
                    try:
                        task = asyncio.create_task(
                            self._execute(
                                run_id=view.id,
                                thread_id=thread_id,
                                owner_user_id=owner_user_id,
                                graph=graph,
                                graph_input=graph_input,
                                config=config,
                                options=options,
                                subgraphs=subgraphs,
                            ),
                            name=f"run-{view.id}",
                        )
                    except BaseException as exc:
                        self._runs.pop(view.id, None)
                        scheduling_error = exc
                    else:
                        entry.task = task
        if scheduling_error is not None:
            if str(scheduling_error) == "cancelled before start":
                await self._mark_cancelled(view.id, owner_user_id, thread_id)
                await self._discard_reservation(view.id)
                return
            await self._rollback_unscheduled(view.id, owner_user_id, thread_id)
            raise scheduling_error

    async def _rollback_unscheduled(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        thread_id: uuid.UUID,
    ) -> None:
        payload = {
            "status": "cancelled",
            "reason": "task_scheduling_failed",
        }
        try:
            async with session_scope(self.session_factory) as session:
                await RunRepository(session).set_status_for_owner(
                    run_id,
                    owner_user_id,
                    "cancelled",
                    finished_at=datetime.now(UTC),
                    error_code="task_scheduling_failed",
                    error_message="Run task could not be scheduled",
                    error_details={
                        "reconciliation": {
                            "intent": "cancelled",
                            "reason": "task_scheduling_failed",
                            "event_type": "end",
                            "event_payload": payload,
                            "graph_succeeded": False,
                        }
                    },
                    touch_thread=True,
                )
                await RunEventRepository(session).append_terminal_if_absent(
                    run_id=run_id,
                    event_type="end",
                    payload=payload,
                )
        except Exception:
            logger.exception(
                "Could not roll back unscheduled run %s on thread %s",
                run_id,
                thread_id,
            )
            await self._remember_pending(
                _PendingTerminal(
                    run_id=run_id,
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    status="cancelled",
                    status_kwargs={
                        "finished_at": datetime.now(UTC),
                        "error_code": "task_scheduling_failed",
                        "error_message": "Run task could not be scheduled",
                        "cancellation_requested": True,
                        "touch_thread": True,
                    },
                    event_type="end",
                    event_payload=payload,
                    graph_succeeded=False,
                    status_confirmed=False,
                )
            )

    async def _task_for_cancel(self, run_id: uuid.UUID) -> asyncio.Task | None:
        """Wait until a registered task exists. Do not treat a placeholder as absent."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while True:
            async with self._lock:
                entry = self._runs.get(run_id)
                if entry is None:
                    return None
                entry.cancel_requested = True
                if entry.task is not None:
                    if entry.task.done():
                        return None
                    return entry.task
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.01)

    async def _release_stale_active(
        self,
        thread_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> None:
        """Settle a finished graph without treating metadata repair as a live run."""
        await self.reconcile_pending()
        async with session_scope(self.session_factory) as session:
            active = await RunRepository(session).find_active_id(
                thread_id,
                owner_user_id,
            )
        if active is None or await self._genuinely_active(active):
            return
        pending = await self._pending_for(active)
        if pending is not None:
            await self.reconcile_pending()
            return
        await self._settle_active_id(active, fallback_reason="stale_active_run")

    async def _thread_visible(
        self,
        thread_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        agent_id: uuid.UUID,
    ) -> bool:
        foreign = False
        async with session_scope(self.session_factory) as session:
            thread = await ThreadRepository(session).get_by_id(thread_id)
            if thread is None:
                return False
            if thread.owner_user_id != owner_user_id:
                foreign = True
            elif thread.status == "deleted" or thread.agent_id != agent_id:
                return False
            else:
                return True
        if foreign:
            await self._audit_denial(owner_user_id, "thread", str(thread_id))
        return False

    async def _audit_denial(
        self,
        actor_user_id: uuid.UUID,
        resource_type: str,
        resource_id: str,
    ) -> None:
        try:
            async with session_scope(self.session_factory) as session:
                await AuditLogRepository(session).append(
                    actor_user_id=actor_user_id,
                    action="access.denied",
                    resource_type=resource_type,
                    resource_id=resource_id,
                    metadata={"scope": resource_type},
                )
        except Exception:
            logger.exception("Could not audit a cross-tenant denial")

    async def _catch_up(
        self,
        subscription: _Subscriber,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        last: int,
    ) -> tuple[list[StreamEvent], int, bool]:
        """Read committed events after ``last`` and then rejoin live delivery."""
        produced: list[StreamEvent] = []
        cursor = last
        while True:
            _drain_queue(subscription)
            for event in await self._read_all_after(run_id, owner_user_id, cursor):
                if event.sequence > cursor:
                    produced.append(event)
                    cursor = event.sequence
            if any(event.terminal for event in produced):
                async with self._lock:
                    subscription.lagging = False
                return produced, cursor, True
            async with self._lock:
                subscription.lagging = False
            for event in await self._read_all_after(run_id, owner_user_id, cursor):
                if event.sequence > cursor:
                    produced.append(event)
                    cursor = event.sequence
            if any(event.terminal for event in produced):
                return produced, cursor, True
            async with self._lock:
                if subscription.lagging:
                    continue
            return produced, cursor, False

    async def _read_all_after(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        after_sequence: int,
    ) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        cursor = after_sequence
        while True:
            batch = await self._read_events(run_id, owner_user_id, cursor)
            if not batch:
                break
            for event in batch:
                if event.sequence > cursor:
                    events.append(event)
                    cursor = event.sequence
            if len(batch) < READ_BATCH or any(event.terminal for event in batch):
                break
        return events

    async def _iterate(
        self,
        *,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        after_sequence: int,
        cancel_on_disconnect: bool,
    ) -> AsyncIterator[StreamEvent]:
        subscription = await self._subscribe(
            run_id,
            cancel_on_disconnect=cancel_on_disconnect,
        )
        last = after_sequence
        saw_terminal = False
        try:
            while True:
                batch = await self._read_events(run_id, owner_user_id, last)
                if not batch:
                    break
                for event in batch:
                    if event.sequence <= last:
                        continue
                    yield event
                    last = event.sequence
                    saw_terminal = saw_terminal or event.terminal
                if len(batch) < READ_BATCH:
                    break

            if await self._run_status(run_id, owner_user_id) in TERMINAL_RUN_STATUSES:
                if not saw_terminal:
                    await self._repair_one_terminal_event(run_id, owner_user_id)
                for event in await self._read_events(run_id, owner_user_id, last):
                    if event.sequence > last:
                        yield event
                        last = event.sequence
                        saw_terminal = True
                return

            if subscription is None:
                subscription = await self._subscribe(
                    run_id,
                    cancel_on_disconnect=cancel_on_disconnect,
                )
            if subscription is None:
                for event in await self._read_events(run_id, owner_user_id, last):
                    if event.sequence > last:
                        yield event
                return

            while True:
                if subscription.lagging:
                    caught_up, cursor, terminal = await self._catch_up(
                        subscription,
                        run_id,
                        owner_user_id,
                        last,
                    )
                    for event in caught_up:
                        if event.sequence <= last:
                            continue
                        yield event
                        last = event.sequence
                        if event.terminal:
                            saw_terminal = True
                            return
                    last = max(last, cursor)
                    if terminal:
                        saw_terminal = True
                        return
                    continue
                try:
                    event = await asyncio.wait_for(subscription.queue.get(), timeout=1)
                except TimeoutError:
                    if subscription.lagging:
                        continue
                    if not await self._is_active(run_id):
                        if not saw_terminal:
                            await self._repair_one_terminal_event(run_id, owner_user_id)
                        for missed in await self._read_all_after(
                            run_id,
                            owner_user_id,
                            last,
                        ):
                            if missed.sequence > last:
                                yield missed
                                last = missed.sequence
                                saw_terminal = saw_terminal or missed.terminal
                        return
                    continue
                if subscription.lagging or event.sequence > last + 1:
                    subscription.lagging = True
                    continue
                if event.sequence <= last:
                    if event.terminal:
                        saw_terminal = True
                        return
                    continue
                yield event
                last = event.sequence
                if event.terminal:
                    saw_terminal = True
                    return
        finally:
            if subscription is not None:
                await self._unsubscribe(
                    run_id,
                    subscription,
                    disconnect_cancel=not saw_terminal,
                )

    async def _read_events(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        after_sequence: int,
    ) -> list[StreamEvent]:
        async with session_scope(self.session_factory) as session:
            rows = await RunEventRepository(session).replay(
                run_id=run_id,
                owner_user_id=owner_user_id,
                after_sequence=after_sequence,
                limit=READ_BATCH,
            )
            return [
                StreamEvent(
                    sequence=int(row.sequence),
                    event_type=row.event_type,
                    payload=dict(row.payload or {}),
                    terminal=row.event_type in TERMINAL_EVENT_TYPES,
                )
                for row in rows
            ]

    async def _subscribe(
        self,
        run_id: uuid.UUID,
        *,
        cancel_on_disconnect: bool,
    ) -> _Subscriber | None:
        async with self._lock:
            entry = self._runs.get(run_id)
            if entry is None or entry.task is None or entry.task.done():
                return None
            subscriber = _Subscriber(
                queue=asyncio.Queue(maxsize=self.subscriber_queue_size),
                cancel_on_disconnect=cancel_on_disconnect,
            )
            entry.subscribers.append(subscriber)
            return subscriber

    async def _unsubscribe(
        self,
        run_id: uuid.UUID,
        subscriber: _Subscriber,
        *,
        disconnect_cancel: bool,
    ) -> None:
        task: asyncio.Task | None = None
        async with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return
            if subscriber in entry.subscribers:
                entry.subscribers.remove(subscriber)
            should_cancel = disconnect_cancel and (
                subscriber.cancel_on_disconnect or entry.on_disconnect == "cancel"
            )
            task_running = entry.task is not None and not entry.task.done()
            if should_cancel and not entry.subscribers and task_running:
                entry.cancel_requested = True
                task = entry.task
        if task is not None:
            task.cancel()

    async def _publish(self, run_id: uuid.UUID, event: StreamEvent) -> None:
        async with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return
            for subscriber in list(entry.subscribers):
                if subscriber.lagging:
                    continue
                try:
                    subscriber.queue.put_nowait(event)
                except asyncio.QueueFull:
                    subscriber.lagging = True

    async def _is_active(self, run_id: uuid.UUID) -> bool:
        async with self._lock:
            entry = self._runs.get(run_id)
            return (
                entry is not None
                and entry.task is not None
                and not entry.task.done()
            )

    async def _run_status(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> str | None:
        async with session_scope(self.session_factory) as session:
            run = await RunRepository(session).get_for_owner(run_id, owner_user_id)
            if run is None:
                return None
            return run.status

    async def _visible_run(
        self,
        thread_id: uuid.UUID,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> RunView | None:
        foreign_thread = False
        foreign_run = False
        async with session_scope(self.session_factory) as session:
            threads = ThreadRepository(session)
            run = await RunRepository(session).get_for_owner(run_id, owner_user_id)
            thread = await threads.get_for_owner(thread_id, owner_user_id)
            if (
                run is not None
                and thread is not None
                and thread.status != "deleted"
                and run.thread_id == thread_id
            ):
                return RunView.from_run(run)
            raw_thread = await threads.get_by_id(thread_id)
            if (
                raw_thread is not None
                and raw_thread.owner_user_id != owner_user_id
            ):
                foreign_thread = True
            if run is None:
                raw_run = await session.get(Run, run_id)
                if raw_run is not None:
                    raw_run_thread = await threads.get_by_id(raw_run.thread_id)
                    if (
                        raw_run_thread is not None
                        and raw_run_thread.owner_user_id != owner_user_id
                    ):
                        foreign_run = True
        if foreign_thread:
            await self._audit_denial(owner_user_id, "thread", str(thread_id))
        if foreign_run:
            await self._audit_denial(owner_user_id, "run", str(run_id))
        return None

    def _cancel_payload(self, view: RunView) -> dict[str, Any]:
        assistant_id = self.registry.graph_id_for_agent(view.agent_id) or str(
            view.agent_id
        )
        updated_at = view.finished_at or view.created_at
        return {
            "run_id": str(view.id),
            "thread_id": str(view.thread_id),
            "assistant_id": assistant_id,
            "status": view.status,
            "created_at": view.created_at.isoformat(),
            "updated_at": updated_at.isoformat(),
            "cancellation_requested": view.cancellation_requested,
        }

    @asynccontextmanager
    async def _thread_guard(self, thread_id: uuid.UUID) -> AsyncIterator[None]:
        """Hold a per-thread lock and drop it when nobody is waiting."""
        async with self._lock:
            slot = self._thread_locks.get(thread_id)
            if slot is None:
                slot = _ThreadLockSlot(lock=asyncio.Lock(), refs=0)
                self._thread_locks[thread_id] = slot
            slot.refs += 1
            lock = slot.lock
        try:
            async with lock:
                yield
        finally:
            async with self._lock:
                slot.refs -= 1
                current = self._thread_locks.get(thread_id)
                if current is slot and slot.refs == 0:
                    self._thread_locks.pop(thread_id, None)


def _drain_queue(subscription: _Subscriber) -> None:
    while True:
        try:
            subscription.queue.get_nowait()
        except asyncio.QueueEmpty:
            return


def _encode_sse(event: StreamEvent) -> bytes:
    payload = json.dumps(
        jsonable_encoder(event.payload),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"
    ).encode()


def _json_object(value: Any) -> dict[str, Any]:
    encoded = jsonable_encoder(value)
    if isinstance(encoded, dict):
        return encoded
    return {"value": encoded}


def _output_summary(latest: Any) -> dict[str, Any]:
    encoded = jsonable_encoder(latest) if latest is not None else {}
    if isinstance(encoded, dict):
        return {"keys": sorted(str(key) for key in encoded)}
    return {"result_type": type(encoded).__name__}


def _unwrap_chunk(chunk: Any, subgraphs: bool) -> tuple[tuple[Any, ...], Any]:
    if (
        subgraphs
        and isinstance(chunk, tuple)
        and len(chunk) == 2
        and isinstance(chunk[0], tuple)
    ):
        return chunk[0], chunk[1]
    return (), chunk


def _terminal_event_type(
    event_type: str,
    *,
    graph_succeeded: bool,
    persistence_retry: bool,
) -> str:
    if graph_succeeded and persistence_retry:
        return "error"
    return event_type


def _terminal_event_payload(
    payload: dict[str, Any],
    *,
    run_id: uuid.UUID,
    thread_id: uuid.UUID,
    graph_succeeded: bool,
    persistence_retry: bool,
) -> dict[str, Any]:
    if graph_succeeded and persistence_retry:
        return {
            "error": "persistence_failed",
            "message": (
                "Graph execution completed but the terminal run state "
                "could not be saved"
            ),
            "run_id": str(run_id),
            "thread_id": str(thread_id),
        }
    return dict(payload)


def _interruption_message(reason: str) -> str:
    if reason == "process_restart":
        return "Run was interrupted because the server process restarted"
    if reason == "shutdown":
        return "Run was interrupted during server shutdown"
    if reason == "stale_active_run":
        return "Run was interrupted because it had no active worker"
    return "Run was interrupted because its outcome could not be determined"


@dataclass(frozen=True, slots=True)
class RestartDecision:
    """Terminal status chosen from a checkpoint or durable intent."""

    status: str
    error_code: str | None
    error_message: str
    event_type: str
    event_payload: dict[str, Any]
    graph_succeeded: bool


def decide_orphan_outcome(
    *,
    checkpoint: str,
    intent: dict[str, Any] | None,
    fallback_reason: str,
) -> RestartDecision:
    """Choose a terminal status without downgrading a completed checkpoint.

    A checkpoint that finished without an interrupt is completed even when
    run metadata was not saved. Durable intent distinguishes failed, cancelled,
    and interrupted runs when the checkpoint is not a successful completion.
    Anything else is an explicit interruption.
    """
    if checkpoint == "completed":
        return RestartDecision(
            status="completed",
            error_code=None,
            error_message="",
            event_type="end",
            event_payload={"status": "success", "reconciliation": "checkpoint"},
            graph_succeeded=True,
        )
    if intent and intent.get("intent") in {
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    }:
        status = str(intent["intent"])
        event_type = str(intent.get("event_type") or ("error" if status == "failed" else "end"))
        payload = dict(intent.get("event_payload") or {})
        if not payload:
            payload = _default_terminal_payload(status, fallback_reason)
        reason = str(intent.get("reason") or fallback_reason)
        return RestartDecision(
            status=status,
            error_code=reason,
            error_message=_message_for_status(status, reason),
            event_type=event_type,
            event_payload=payload,
            graph_succeeded=bool(intent.get("graph_succeeded")),
        )
    if checkpoint == "interrupted":
        return RestartDecision(
            status="interrupted",
            error_code="graph_interrupt",
            error_message="Run interrupted",
            event_type="end",
            event_payload={
                "status": "interrupted",
                "reason": "checkpoint_interrupt",
            },
            graph_succeeded=False,
        )
    message = _interruption_message(fallback_reason)
    return RestartDecision(
        status="interrupted",
        error_code=fallback_reason,
        error_message=message,
        event_type="end",
        event_payload={
            "status": "interrupted",
            "reason": fallback_reason,
            "message": message,
        },
        graph_succeeded=False,
    )


def _is_active_run_uniqueness(exc: IntegrityError) -> bool:
    """Return whether PostgreSQL rejected a second pending or running row."""
    return "uq_runs_one_active_per_thread" in str(getattr(exc, "orig", exc))


def _intent_from_details(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    intent = details.get("reconciliation")
    if not isinstance(intent, dict):
        return None
    return intent


def _message_for_status(status: str, reason: str) -> str:
    if status == "cancelled":
        return "Run cancelled"
    if status == "failed":
        return "Graph execution failed"
    if status == "interrupted":
        return _interruption_message(reason)
    return ""


def _default_terminal_payload(status: str, reason: str) -> dict[str, Any]:
    if status == "failed":
        return {"error": "run_failed", "message": "Graph execution failed"}
    if status == "cancelled":
        return {"status": "cancelled"}
    if status == "interrupted":
        return {
            "status": "interrupted",
            "reason": reason,
            "message": _interruption_message(reason),
        }
    return {"status": "success"}


def _terminal_event_for_status(
    *,
    status: str,
    error_details: dict[str, Any],
    error_code: str | None,
    error_message: str | None,
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
) -> tuple[str, dict[str, Any]]:
    intent = _intent_from_details(error_details)
    if intent and intent.get("event_type") and isinstance(intent.get("event_payload"), dict):
        return str(intent["event_type"]), dict(intent["event_payload"])
    if status == "failed":
        return (
            "error",
            {
                "error": "run_failed",
                "message": error_message or "Graph execution failed",
                "run_id": str(run_id),
                "thread_id": str(thread_id),
            },
        )
    if status == "cancelled":
        return "end", {"status": "cancelled"}
    if status == "interrupted":
        reason = error_code or "interrupted"
        return (
            "end",
            {
                "status": "interrupted",
                "reason": reason,
                "message": error_message or _interruption_message(reason),
            },
        )
    return "end", {"status": "success"}


def _with_reconciliation(
    status_kwargs: dict[str, Any],
    *,
    status: str,
    event_type: str,
    event_payload: dict[str, Any],
    graph_succeeded: bool,
) -> dict[str, Any]:
    """Copy status fields and attach the terminal event that must be repaired."""
    copied = dict(status_kwargs)
    details = dict(copied.get("error_details") or {})
    details["reconciliation"] = {
        "intent": status,
        "reason": copied.get("error_code") or status,
        "event_type": event_type,
        "event_payload": event_payload,
        "graph_succeeded": graph_succeeded,
    }
    copied["error_details"] = details
    return copied
