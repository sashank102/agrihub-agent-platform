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
            "threads": ["create", "search", "get", "state", "history"],
            "runs": ["stream", "join_stream", "cancel"],
            "stream_modes": ["values"],
            "resumable_streams": True,
            "multitask_strategies": ["reject"],
            "on_disconnect": ["continue", "cancel"],
            "durability": ["sync", "async", "exit"],
            "workers": 1,
        },
        "unsupported": [
            "webhook",
            "after_seconds",
            "feedback_keys",
            "multitask strategies other than reject",
            "if_not_exists create",
            "on_completion other than keep",
            "stream modes other than values",
            "cancel action rollback",
            "automatic restart after process loss",
            "multi-worker execution",
        ],
    }
