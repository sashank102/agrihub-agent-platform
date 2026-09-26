"""Stream, reconnect, and cancel routes for durable runs."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from agent_platform.api.dependencies import get_principal, require_ready
from agent_platform.api.schemas import RunStreamRequest
from agent_platform.services.accounts import AuthenticatedPrincipal
from agent_platform.services.errors import (
    ActiveRunConflict,
    CheckpointReferenceError,
    RunCursorError,
    ThreadNotInterrupted,
    UnsupportedRunOption,
)
from agent_platform.services.run_fields import parse_event_cursor

router = APIRouter(
    prefix="/threads",
    dependencies=[Depends(require_ready)],
)
_ACCEPTED_WAIT = {None, "0", "1", "true", "false", "True", "False"}


def _run_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="assistant not found")
    if isinstance(exc, LookupError):
        detail = str(exc) if str(exc) in {"thread not found", "run not found"} else "thread not found"
        return HTTPException(status_code=404, detail=detail)
    if isinstance(exc, ActiveRunConflict):
        conflict: dict[str, object] = {"message": str(exc)}
        if exc.run_id is not None:
            conflict["run_id"] = exc.run_id
        if exc.status is not None:
            conflict["status"] = exc.status
        if exc.reconciliation_intent is not None:
            conflict["reconciliation_intent"] = exc.reconciliation_intent
        if exc.graph_succeeded is not None:
            conflict["graph_succeeded"] = exc.graph_succeeded
        return HTTPException(status_code=409, detail=conflict)
    if isinstance(exc, ThreadNotInterrupted):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, UnsupportedRunOption):
        return HTTPException(status_code=422, detail=exc.detail)
    if isinstance(exc, CheckpointReferenceError):
        return HTTPException(status_code=exc.status_code, detail=exc.detail)
    if isinstance(exc, RunCursorError):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.post("/{thread_id}/runs/stream")
async def stream_run(
    thread_id: uuid.UUID,
    body: RunStreamRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(get_principal)],
) -> StreamingResponse:
    """Start a durable run and stream its committed events."""
    manager = request.app.state.run_manager
    owner_user_id = principal.user_id
    try:
        run = await manager.start_run(thread_id, body, owner_user_id)
    except Exception as exc:
        raise _run_http_error(exc) from exc

    location = f"/threads/{thread_id}/runs/{run.id}"
    cancel_on_disconnect = (body.on_disconnect or "continue") == "cancel"
    return StreamingResponse(
        manager.stream_events(
            thread_id=thread_id,
            run_id=run.id,
            owner_user_id=owner_user_id,
            after_sequence=0,
            cancel_on_disconnect=cancel_on_disconnect,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Content-Location": location,
            "X-Run-ID": str(run.id),
            "X-Thread-ID": str(thread_id),
        },
    )


@router.get("/{thread_id}/runs/{run_id}/stream")
async def join_run_stream(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(get_principal)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    last_event_id_query: Annotated[str | None, Query(alias="last_event_id")] = None,
    cancel_on_disconnect: Annotated[str | None, Query()] = None,
    stream_mode: Annotated[list[str] | None, Query()] = None,
) -> StreamingResponse:
    """Replay durable events and follow a still-active run."""
    if stream_mode is not None and any(
        mode not in {"values", "updates", "custom", "messages-tuple"}
        for mode in stream_mode
    ):
        raise HTTPException(
            status_code=422,
            detail="stream_mode only supports values",
        )
    try:
        cursor = parse_event_cursor(last_event_id, last_event_id_query)
    except RunCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    manager = request.app.state.run_manager
    owner_user_id = principal.user_id
    disconnect = cancel_on_disconnect in {"1", "true", "True"}
    location = f"/threads/{thread_id}/runs/{run_id}"
    try:
        await manager.get_owned_run(thread_id, run_id, owner_user_id)
    except Exception as exc:
        raise _run_http_error(exc) from exc

    return StreamingResponse(
        manager.stream_events(
            thread_id=thread_id,
            run_id=run_id,
            owner_user_id=owner_user_id,
            after_sequence=cursor,
            cancel_on_disconnect=disconnect,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Content-Location": location,
            "X-Run-ID": str(run_id),
            "X-Thread-ID": str(thread_id),
        },
    )


@router.post("/{thread_id}/runs/{run_id}/cancel")
async def cancel_run(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(get_principal)],
    wait: Annotated[str | None, Query()] = None,
    action: Annotated[str, Query()] = "interrupt",
) -> dict[str, object]:
    """Cancel an owned run and return its durable terminal status.

    ``wait`` is accepted for SDK compatibility. The response is returned only
    after the terminal status has been committed, including when ``wait`` is
    false. ``action=rollback`` is rejected because checkpoints are not rolled
    back.
    """
    if wait not in _ACCEPTED_WAIT:
        raise HTTPException(status_code=422, detail="wait must be 0 or 1")
    manager = request.app.state.run_manager
    owner_user_id = principal.user_id
    try:
        return await manager.cancel_run(
            thread_id,
            run_id,
            owner_user_id,
            action=action,
        )
    except Exception as exc:
        raise _run_http_error(exc) from exc
