"""Token budgets for agent conversations.

Tool wrappers already return at most 40 rows. On top of that an agent sees
each tool result clipped to ``tool_output_chars``, and only its last
``history_tool_results`` results verbatim: older results are replaced by a
one-line digest (the result's header, its evidence aliases and its
``output_ref``) so the full rows stay retrievable with ``get_evidence``.
Message order and tool-call ids are never changed, which keeps the history
valid for every provider's tool-calling format.
"""

import re
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage

DIGEST_MARK = "[older result compacted]"
MAX_REJECTED_CALLS = 2
"""Consecutive provider-rejected requests (HTTP 400, e.g. malformed tool arguments) an agent may retry."""
_OUTPUT_REF = re.compile(r"output_ref: (O\d+)")


def clip_tool_output(content: str, limit: int) -> str:
    """Return ``content`` cut to ``limit`` characters with a note saying where the rest is."""
    if len(content) <= limit:
        return content
    ref = _OUTPUT_REF.search(content)
    tail = f" Full result: output_ref {ref.group(1)}." if ref else ""
    return content[: limit - 120].rstrip() + f"\n… [clipped at {limit} characters.{tail}]"


def digest(message: ToolMessage) -> str:
    """Return a one-line stand-in for an old tool result."""
    text = str(message.content)
    if text.startswith(DIGEST_MARK):
        return text
    header = text.strip().splitlines()[0][:160] if text.strip() else "(empty result)"
    artifact: Any = message.artifact if isinstance(message.artifact, dict) else {}
    aliases = artifact.get("aliases") or []
    found = _OUTPUT_REF.search(text)
    ref = artifact.get("output_ref") or (found.group(1) if found else None)
    parts = [DIGEST_MARK, header]
    if aliases:
        parts.append(f"evidence {_ranges(aliases)}")
    if ref:
        parts.append(f"output_ref {ref}")
    return " | ".join(parts)


def compact_history(messages: Sequence[BaseMessage], keep_tool_results: int) -> list[BaseMessage]:
    """Return the messages with every tool result but the last ``keep_tool_results`` digested."""
    tool_positions = [index for index, message in enumerate(messages) if isinstance(message, ToolMessage)]
    old = set(tool_positions[: max(0, len(tool_positions) - keep_tool_results)])
    compacted: list[BaseMessage] = []
    for index, message in enumerate(messages):
        if index in old and isinstance(message, ToolMessage):
            compacted.append(message.model_copy(update={"content": digest(message)}))
        else:
            compacted.append(message)
    return compacted


def estimate_tokens(messages: Sequence[BaseMessage]) -> int:
    """Estimate prompt size at four characters per token."""
    return sum(len(str(message.content)) for message in messages) // 4


def _ranges(aliases: list[str]) -> str:
    numbers = sorted({int(alias[1:]) for alias in aliases if alias[1:].isdigit()})
    parts: list[str] = []
    index = 0
    while index < len(numbers):
        end = index
        while end + 1 < len(numbers) and numbers[end + 1] == numbers[end] + 1:
            end += 1
        parts.append(f"E{numbers[index]}" if end == index else f"E{numbers[index]}..E{numbers[end]}")
        index = end + 1
    return ",".join(parts)


def is_rejected_request(exc: BaseException) -> bool:
    """Return whether the provider refused the request itself (HTTP 400), as opposed to failing or rate limiting."""
    return getattr(exc, "status_code", None) == 400


def rejection_feedback(exc: BaseException) -> str:
    """Tell the model its last reply was refused and why, so it can correct its tool call."""
    return (
        "Your last reply was rejected by the model provider before any tool ran: "
        f"{str(exc)[:400]}. Call one tool with arguments that match its schema exactly."
    )


def budget_reminder(step: int, max_steps: int) -> str | None:
    """Return a reminder for the last two steps of a lane's budget."""
    left = max_steps - step
    if left > 2:
        return None
    if left <= 1:
        return "This is your last tool round: record findings for the evidence you already have, then call specialist_done in the same reply."
    return f"{left} tool rounds left: record your findings now with record_finding, then call specialist_done."
