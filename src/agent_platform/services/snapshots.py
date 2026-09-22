"""Convert LangGraph snapshots into Agent Protocol state documents."""

import uuid
from datetime import datetime
from typing import Any

from fastapi.encoders import jsonable_encoder

from agent_platform.api.schemas import CheckpointResponse, ThreadStateResponse


def agent_protocol_status(
    *,
    has_active_run: bool,
    latest_run_status: str | None,
    checkpoint_interrupted: bool,
) -> str:
    """Derive Agent Protocol thread status from durable run and checkpoint state."""
    if has_active_run:
        return "busy"
    if latest_run_status == "interrupted" or checkpoint_interrupted:
        return "interrupted"
    if latest_run_status == "failed":
        return "error"
    return "idle"


def snapshot_interrupted(snapshot: Any) -> bool:
    """Return whether a snapshot is waiting on a LangGraph interrupt."""
    if getattr(snapshot, "interrupts", ()):
        return True
    return any(
        getattr(task, "interrupts", ())
        for task in (getattr(snapshot, "tasks", ()) or ())
    )


def snapshot_to_state(
    thread_id: uuid.UUID,
    snapshot: Any,
) -> ThreadStateResponse:
    """Map one LangGraph snapshot to the SDK thread-state shape."""
    config = getattr(snapshot, "config", None) or {}
    parent = getattr(snapshot, "parent_config", None)
    created_at = getattr(snapshot, "created_at", None)
    if isinstance(created_at, datetime):
        created_at = created_at.isoformat()
    elif created_at is not None:
        created_at = str(created_at)
    return ThreadStateResponse(
        values=jsonable_encoder(getattr(snapshot, "values", {}) or {}),
        next=[str(node) for node in (getattr(snapshot, "next", ()) or ())],
        checkpoint=checkpoint_response(thread_id, config),
        metadata=jsonable_encoder(getattr(snapshot, "metadata", None)),
        created_at=created_at,
        parent_checkpoint=(
            checkpoint_response(thread_id, parent) if parent is not None else None
        ),
        tasks=[task_payload(thread_id, task) for task in (getattr(snapshot, "tasks", ()) or ())],
    )


def interrupt_map(snapshot: Any) -> dict[str, list[dict[str, Any]]]:
    """Group interrupt payloads by task id for a thread response."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for task in getattr(snapshot, "tasks", ()) or ():
        interrupts = _interrupt_payloads(getattr(task, "interrupts", ()) or ())
        if interrupts:
            grouped[str(getattr(task, "id", ""))] = interrupts
    if grouped:
        return grouped
    root = _interrupt_payloads(getattr(snapshot, "interrupts", ()) or ())
    if root:
        return {"__interrupt__": root}
    return {}


def values_with_interrupts(snapshot: Any) -> Any:
    """Attach ``__interrupt__`` to values when the snapshot is paused."""
    values = jsonable_encoder(getattr(snapshot, "values", {}) or {})
    interrupts = _interrupt_payloads(getattr(snapshot, "interrupts", ()) or ())
    if not interrupts:
        for task in getattr(snapshot, "tasks", ()) or ():
            interrupts.extend(
                _interrupt_payloads(getattr(task, "interrupts", ()) or ())
            )
    if not interrupts:
        return values
    if not isinstance(values, dict):
        values = {"value": values}
    values["__interrupt__"] = interrupts
    return values


def checkpoint_response(
    thread_id: uuid.UUID,
    config: dict[str, Any] | None,
) -> CheckpointResponse:
    """Build a checkpoint identity pinned to the platform thread."""
    configurable = dict((config or {}).get("configurable") or {})
    checkpoint_map = configurable.get("checkpoint_map")
    if not isinstance(checkpoint_map, dict):
        checkpoint_map = None
    checkpoint_id = configurable.get("checkpoint_id")
    return CheckpointResponse(
        thread_id=str(thread_id),
        checkpoint_ns=str(configurable.get("checkpoint_ns") or ""),
        checkpoint_id=None if checkpoint_id is None else str(checkpoint_id),
        checkpoint_map=checkpoint_map,
    )


def task_payload(thread_id: uuid.UUID, task: Any) -> dict[str, Any]:
    """Serialize one pending or finished graph task."""
    error = getattr(task, "error", None)
    state = getattr(task, "state", None)
    checkpoint = None
    if isinstance(state, dict):
        checkpoint = checkpoint_response(thread_id, state).model_dump()
    return {
        "id": str(getattr(task, "id", "")),
        "name": str(getattr(task, "name", "")),
        "error": None if error is None else type(error).__name__,
        "interrupts": _interrupt_payloads(getattr(task, "interrupts", ()) or ()),
        "checkpoint": checkpoint,
        "state": None,
    }


def _interrupt_payloads(interrupts: Any) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for interrupt in interrupts:
        payloads.append(
            {
                "id": getattr(interrupt, "id", None),
                "value": jsonable_encoder(getattr(interrupt, "value", None)),
            }
        )
    return payloads
