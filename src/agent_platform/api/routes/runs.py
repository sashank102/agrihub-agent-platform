"""Thin streamed-run route."""

import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from agent_platform.api.schemas import RunStreamRequest
from agent_platform.services.run_service import RunService

router = APIRouter(prefix="/threads")


@router.post("/{thread_id}/runs/stream")
async def stream_run(
    thread_id: uuid.UUID,
    body: RunStreamRequest,
    request: Request,
) -> StreamingResponse:
    """Create a durable run and stream metadata plus values events."""
    service = RunService(
        session_factory=request.app.state.session_factory,
        registry=request.app.state.graph_registry,
        semaphore=request.app.state.run_semaphore,
        owner_user_id=request.app.state.settings.DEVELOPMENT_USER_ID,
    )
    try:
        run, registered = await service.create(thread_id, body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="assistant not found") from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="thread not found") from exc

    location = f"/threads/{thread_id}/runs/{run.id}"
    return StreamingResponse(
        service.stream(run, registered, body),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Content-Location": location,
            "X-Run-ID": str(run.id),
            "X-Thread-ID": str(thread_id),
        },
    )
