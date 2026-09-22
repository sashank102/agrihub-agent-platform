"""In-process durable run execution, replay, and cancellation.

Consistency boundary: this manager keeps task handles and subscriber queues
in one process. PostgreSQL is the replay log. A second Uvicorn worker cannot
observe those queues. Startup marks leftover pending and running rows
interrupted and does not resume model or tool calls automatically.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi.encoders import jsonable_encoder
from langgraph.errors import GraphInterrupt, NodeCancelledError

from agent_platform.api.schemas import RunStreamRequest
from agent_platform.db.models.run import TERMINAL_RUN_STATUSES
from agent_platform.db.repositories import (
    RunEventRepository,
    RunRepository,
    ThreadRepository,
)
from agent_platform.db.session import AsyncSessionFactory, session_scope
from agent_platform.services.errors import ActiveRunConflict, UnsupportedRunOption
from agent_platform.services.graph_registry import GraphRegistry
from agent_platform.services.run_fields import (
    astream_options,
    checkpoint_reference,
    disconnect_policy,
    ensure_checkpoint_belongs_to_thread,
    execution_config,
    graph_input_for,
    run_configuration,
    validate_run_stream_request,
)
from agent_platform.services.snapshots import (
    snapshot_interrupted,
    values_with_interrupts,
)

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
    """A bounded queue for one SSE consumer."""

    queue: asyncio.Queue
    dropped: bool = False
    cancel_on_disconnect: bool = False


@dataclass
class _ActiveRun:
    """Process-local state for one executing run."""

    task: asyncio.Task
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
    ) -> None:
        """Create an empty task registry and the process-wide run semaphore."""
        self.session_factory = session_factory
        self.registry = registry
        self.subscriber_queue_size = subscriber_queue_size
        self._semaphore = asyncio.Semaphore(max_concurrent_runs)
        self._lock = asyncio.Lock()
        self._thread_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._runs: dict[uuid.UUID, _ActiveRun] = {}
        self._shutting_down = False
        self._closed = False

    async def reconcile_orphaned_runs(self, *, reason: str) -> int:
        """Mark leftover pending and running rows interrupted.

        ``process_restart`` is used at startup. ``shutdown`` covers rows that
        were still active after registered tasks were cancelled. Neither path
        invokes the graph.
        """
        async with session_scope(self.session_factory) as session:
            run_ids = await RunRepository(session).list_active_ids()
        changed = 0
        message = _interruption_message(reason)
        for run_id in run_ids:
            async with session_scope(self.session_factory) as session:
                run = await RunRepository(session).set_status_internal(
                    run_id,
                    "interrupted",
                    finished_at=datetime.now(UTC),
                    error_code=reason,
                    error_message=message,
                    only_active=True,
                )
                if run is None or run.status != "interrupted" or run.error_code != reason:
                    continue
                await RunEventRepository(session).append_for_system(
                    run_id=run_id,
                    event_type="end",
                    payload={
                        "status": "interrupted",
                        "reason": reason,
                        "message": message,
                    },
                )
            changed += 1
        return changed

    async def shutdown(self) -> None:
        """Cancel registered tasks and interrupt any rows that remain active."""
        if self._closed:
            return
        self._shutting_down = True
        async with self._lock:
            tasks = [
                entry.task
                for entry in self._runs.values()
                if not entry.task.done()
            ]
            for entry in self._runs.values():
                entry.cancel_requested = True
                if not entry.task.done():
                    entry.task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Run task failed while shutting down")
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

        async with session_scope(self.session_factory) as session:
            thread = await ThreadRepository(session).get_for_owner(
                thread_id,
                owner_user_id,
            )
            if thread is None or thread.status == "deleted":
                raise LookupError("thread not found")
            if thread.agent_id != registered.agent_id:
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
        config = execution_config(thread_id, request)
        options = astream_options(request)

        thread_lock = await self._lock_for(thread_id)
        async with thread_lock:
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
                if active is not None:
                    raise ActiveRunConflict()
                run, _created = await RunRepository(session).create_idempotent(
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    agent_id=registered.agent_id,
                    input=request.input,
                    configuration=run_configuration(request),
                )
                view = RunView.from_run(run)

            task = asyncio.create_task(
                self._execute(
                    run_id=view.id,
                    thread_id=thread_id,
                    owner_user_id=owner_user_id,
                    graph=registered.graph,
                    graph_input=graph_input,
                    config=config,
                    options=options,
                    subgraphs=bool(request.stream_subgraphs),
                ),
                name=f"run-{view.id}",
            )
            async with self._lock:
                self._runs[view.id] = _ActiveRun(
                    task=task,
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    on_disconnect=disconnect_policy(request),
                )
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
        async with self._lock:
            entry = self._runs.get(run_id)
            if entry is not None:
                entry.cancel_requested = True
            task = None if entry is None or entry.task.done() else entry.task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Run %s failed while cancelling", run_id)
        else:
            await self._mark_cancelled(run_id, owner_user_id, thread_id)

        refreshed = await self._visible_run(thread_id, run_id, owner_user_id)
        return self._cancel_payload(refreshed or current)

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
        graph_completed = False
        paused = False
        latest: Any = None

        async def finalize_cancelled() -> None:
            nonlocal finalized
            if finalized:
                return
            await self._mark_cancelled(run_id, owner_user_id, thread_id)
            finalized = True

        async def finalize_failed(exc: BaseException) -> None:
            nonlocal finalized
            if finalized:
                return
            await self._mark_failed(run_id, owner_user_id, thread_id, exc)
            finalized = True

        async def finalize_success() -> None:
            nonlocal finalized
            if finalized:
                return
            await self._complete_or_reconcile(
                run_id,
                owner_user_id,
                thread_id,
                latest,
            )
            finalized = True

        try:
            await self.persist_and_publish(
                run_id=run_id,
                owner_user_id=owner_user_id,
                event_type="metadata",
                payload={"run_id": str(run_id), "thread_id": str(thread_id)},
            )
            async with self._semaphore:
                await self._write_status(
                    run_id,
                    owner_user_id,
                    "running",
                    started_at=datetime.now(UTC),
                )
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
                await self._mark_interrupted(run_id, owner_user_id, thread_id)
                finalized = True
            else:
                await finalize_success()
        except asyncio.CancelledError:
            if paused or graph_completed:
                if paused:
                    await self._mark_interrupted(run_id, owner_user_id, thread_id)
                    finalized = True
                else:
                    await finalize_success()
            else:
                await finalize_cancelled()
            raise
        except GraphInterrupt:
            paused = True
            if not finalized:
                await self._mark_interrupted(run_id, owner_user_id, thread_id)
                finalized = True
        except NodeCancelledError as exc:
            if await self._cancellation_requested(run_id) or self._shutting_down:
                await finalize_cancelled()
            else:
                await finalize_failed(exc)
        except Exception as exc:
            if paused and not finalized:
                await self._mark_interrupted(run_id, owner_user_id, thread_id)
                finalized = True
            elif graph_completed:
                await finalize_success()
            else:
                logger.exception("Graph run %s failed", run_id)
                await finalize_failed(exc)
        finally:
            try:
                if not finalized:
                    if paused:
                        await self._mark_interrupted(run_id, owner_user_id, thread_id)
                    elif graph_completed:
                        await finalize_success()
                    else:
                        await finalize_cancelled()
            except Exception:
                logger.exception("Could not finalize run %s", run_id)
            finally:
                async with self._lock:
                    self._runs.pop(run_id, None)

    async def _complete_or_reconcile(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        thread_id: uuid.UUID,
        latest: Any,
    ) -> None:
        summary = _output_summary(latest)
        try:
            status = await self._write_status(
                run_id,
                owner_user_id,
                "completed",
                finished_at=datetime.now(UTC),
                output_summary=summary,
                touch_thread=True,
            )
        except Exception:
            logger.exception(
                "Run %s completed but terminal metadata persistence failed",
                run_id,
            )
            summary = {**summary, "persistence_reconciled": True}
            try:
                await self._write_status(
                    run_id,
                    owner_user_id,
                    "completed",
                    finished_at=datetime.now(UTC),
                    output_summary=summary,
                    touch_thread=True,
                )
            except Exception:
                logger.exception(
                    "Run %s completed but terminal metadata reconciliation failed",
                    run_id,
                )
            await self._emit_persistence_error(run_id, owner_user_id, thread_id)
            return
        if status != "completed":
            return
        await self.persist_and_publish(
            run_id=run_id,
            owner_user_id=owner_user_id,
            event_type="end",
            payload={"status": "success"},
            terminal=True,
        )

    async def _mark_interrupted(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        thread_id: uuid.UUID,
    ) -> None:
        """Persist an interrupted run without treating it as a graph failure."""
        try:
            status = await self._write_status(
                run_id,
                owner_user_id,
                "interrupted",
                finished_at=datetime.now(UTC),
                error_code="graph_interrupt",
                error_message="Run interrupted",
                touch_thread=True,
            )
        except Exception:
            logger.exception(
                "Could not persist interruption for run %s on thread %s",
                run_id,
                thread_id,
            )
            return
        if status != "interrupted":
            return
        try:
            await self.persist_and_publish(
                run_id=run_id,
                owner_user_id=owner_user_id,
                event_type="end",
                payload={"status": "interrupted"},
                terminal=True,
            )
        except Exception:
            logger.exception("Could not publish interruption for run %s", run_id)

    async def _mark_cancelled(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        thread_id: uuid.UUID,
    ) -> None:
        try:
            status = await self._write_status(
                run_id,
                owner_user_id,
                "cancelled",
                finished_at=datetime.now(UTC),
                error_code="cancelled",
                error_message="Run cancelled",
                cancellation_requested=True,
                touch_thread=True,
            )
        except Exception:
            logger.exception("Could not persist cancellation for run %s", run_id)
            return
        if status != "cancelled":
            return
        try:
            await self.persist_and_publish(
                run_id=run_id,
                owner_user_id=owner_user_id,
                event_type="end",
                payload={"status": "cancelled"},
                terminal=True,
            )
        except Exception:
            logger.exception("Could not publish cancellation for run %s", run_id)

    async def _mark_failed(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        thread_id: uuid.UUID,
        exc: BaseException,
    ) -> None:
        try:
            status = await self._write_status(
                run_id,
                owner_user_id,
                "failed",
                finished_at=datetime.now(UTC),
                error_code=type(exc).__name__,
                error_message="Graph execution failed",
                touch_thread=True,
            )
        except Exception:
            logger.exception("Could not persist failure for run %s", run_id)
            return
        if status != "failed":
            return
        try:
            await self.persist_and_publish(
                run_id=run_id,
                owner_user_id=owner_user_id,
                event_type="error",
                payload={
                    "error": "run_failed",
                    "message": "Graph execution failed",
                    "run_id": str(run_id),
                    "thread_id": str(thread_id),
                },
                terminal=True,
            )
        except Exception:
            logger.exception("Could not publish failure for run %s", run_id)

    async def _emit_persistence_error(
        self,
        run_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        thread_id: uuid.UUID,
    ) -> None:
        try:
            await self.persist_and_publish(
                run_id=run_id,
                owner_user_id=owner_user_id,
                event_type="error",
                payload={
                    "error": "persistence_failed",
                    "message": (
                        "Graph execution completed but the terminal run state "
                        "could not be saved"
                    ),
                    "run_id": str(run_id),
                    "thread_id": str(thread_id),
                },
                terminal=True,
            )
        except Exception:
            logger.exception(
                "Could not publish persistence failure for run %s",
                run_id,
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
            return entry is not None and entry.cancel_requested

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
                if subscription.dropped and subscription.queue.empty():
                    return
                try:
                    event = await asyncio.wait_for(subscription.queue.get(), timeout=1)
                except TimeoutError:
                    if not await self._is_active(run_id):
                        for missed in await self._read_events(
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
                if event.sequence <= last:
                    if event.terminal:
                        saw_terminal = True
                        return
                    continue
                if event.sequence > last + 1:
                    for missed in await self._read_events(
                        run_id,
                        owner_user_id,
                        last,
                    ):
                        if missed.sequence > last:
                            yield missed
                            last = missed.sequence
                            if missed.terminal:
                                saw_terminal = True
                                return
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
            if entry is None or entry.task.done():
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
            if should_cancel and not entry.subscribers and not entry.task.done():
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
                if subscriber.dropped:
                    continue
                try:
                    subscriber.queue.put_nowait(event)
                except asyncio.QueueFull:
                    subscriber.dropped = True

    async def _is_active(self, run_id: uuid.UUID) -> bool:
        async with self._lock:
            entry = self._runs.get(run_id)
            return entry is not None and not entry.task.done()

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
        async with session_scope(self.session_factory) as session:
            run = await RunRepository(session).get_for_owner(run_id, owner_user_id)
            thread = await ThreadRepository(session).get_for_owner(
                thread_id,
                owner_user_id,
            )
            if (
                run is None
                or thread is None
                or thread.status == "deleted"
                or run.thread_id != thread_id
            ):
                return None
            return RunView.from_run(run)

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

    async def _lock_for(self, thread_id: uuid.UUID) -> asyncio.Lock:
        async with self._lock:
            return self._thread_locks.setdefault(thread_id, asyncio.Lock())


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


def _interruption_message(reason: str) -> str:
    if reason == "process_restart":
        return "Run was interrupted because the server process restarted"
    return "Run was interrupted during server shutdown"
