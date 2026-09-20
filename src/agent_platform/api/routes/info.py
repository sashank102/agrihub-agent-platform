"""Minimal server information used by local LangGraph SDK clients."""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/info")
async def info(request: Request) -> dict[str, object]:
    """Describe only the Agent Protocol features implemented here."""
    settings = request.app.state.settings
    return {
        "version": "0.1.0",
        "graph_id": settings.DEVELOPMENT_GRAPH_ID,
        "authentication": False,
        "capabilities": {
            "threads": ["create", "search", "get", "state"],
            "runs": ["stream"],
            "stream_modes": ["values"],
            "resumable_streams": False,
        },
    }
