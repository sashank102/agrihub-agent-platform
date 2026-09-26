"""Deterministic graph used by contract and browser tests.

Messages select the behavior:

- ``fail`` raises a predictable error
- ``interrupt`` pauses with an agent-inbox payload
- ``slow`` waits before echoing, long enough to cancel or reconnect
- ``hold`` waits until ``release_hold`` is called
- any other text is echoed
- list content is summarized without logging file bytes
"""

import asyncio
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

_HOLD = asyncio.Event()


def release_hold() -> None:
    """Unblock a run that received the ``hold`` message."""
    _HOLD.set()


def reset_hold() -> None:
    """Arm the hold gate so the next ``hold`` message waits."""
    _HOLD.clear()


def _message_text(content: Any) -> tuple[str, list[str]]:
    """Return visible text and block kinds from a human message."""
    if isinstance(content, str):
        return content, ["text"]
    if not isinstance(content, list):
        return str(content), ["text"]
    texts: list[str] = []
    kinds: list[str] = []
    for block in content:
        if isinstance(block, dict):
            kind = str(block.get("type") or "block")
            kinds.append(kind)
            if kind == "text":
                texts.append(str(block.get("text") or ""))
        else:
            kinds.append("text")
            texts.append(str(block))
    return "\n".join(part for part in texts if part), kinds


def build_fixture_graph(*, checkpointer: Any, store: Any) -> Any:
    """Compile the deterministic graph with the caller's persistence."""

    async def respond(state: MessagesState) -> dict[str, Any]:
        text, kinds = _message_text(state["messages"][-1].content)
        if any(kind in {"image", "file"} for kind in kinds):
            return {"messages": [AIMessage(content="blocks:" + ",".join(kinds))]}
        if text == "fail":
            raise RuntimeError("deterministic failure")
        if text == "slow":
            await asyncio.sleep(1.5)
            return {"messages": [AIMessage(content="Echo: slow")]}
        if text == "hold":
            await _HOLD.wait()
            return {"messages": [AIMessage(content="Echo: hold")]}
        if text.startswith("interrupt"):
            decision = interrupt(
                {
                    "action_requests": [
                        {
                            "name": "publish",
                            "args": {"text": "draft"},
                            "description": "Review the draft",
                        }
                    ],
                    "review_configs": [
                        {
                            "action_name": "publish",
                            "allowed_decisions": ["approve", "edit", "reject"],
                        }
                    ],
                }
            )
            kind = "approve"
            body = "draft"
            if isinstance(decision, dict):
                first = (decision.get("decisions") or [{}])[0]
                kind = first.get("type", "approve")
                if kind == "edit":
                    body = (
                        first.get("edited_action", {}).get("args", {}).get("text", body)
                    )
                elif kind == "reject":
                    body = first.get("message") or "rejected"
            return {"messages": [AIMessage(content=f"decision:{kind}:{body}")]}
        return {"messages": [AIMessage(content=f"Echo: {text}")]}

    builder = StateGraph(MessagesState)
    builder.add_node("respond", respond)
    builder.add_edge(START, "respond")
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=checkpointer, store=store)
