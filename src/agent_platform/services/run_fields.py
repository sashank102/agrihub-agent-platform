"""Honest handling of Agent Protocol run-stream fields."""

import uuid
from typing import Any

from langgraph.types import Command

from agent_platform.api.schemas import RunStreamRequest
from agent_platform.services.errors import (
    CheckpointReferenceError,
    RunCursorError,
    ThreadNotInterrupted,
    UnsupportedRunOption,
)
from agent_platform.services.redaction import strip_client_secrets
from agent_platform.services.tenant_context import TenantIdentity

_IDENTITY_KEYS = ("user_id", "owner_user_id", "owner_id", "run_id")

SUPPORTED_STREAM_MODES = frozenset({"values"})
# The React SDK always requests ``updates`` beside ``values``. It also registers
# ``custom`` for onCustomEvent and ``messages-tuple`` when message helpers are
# read during render. Those extra modes are accepted and not emitted. The run
# still streams ``values``, which carry the message state.
ACCEPTED_STREAM_MODES = frozenset(
    {"values", "updates", "custom", "messages-tuple"}
)
IMPLEMENTED_DURABILITY = frozenset({"sync", "async", "exit"})


def validate_run_stream_request(request: RunStreamRequest) -> None:
    """Accept implemented fields and reject unsupported ones with a reason."""
    if request.webhook is not None:
        raise UnsupportedRunOption("webhook delivery is not supported")
    if request.after_seconds is not None:
        raise UnsupportedRunOption("delayed or scheduled runs are not supported")
    if request.feedback_keys is not None:
        raise UnsupportedRunOption("feedback keys are not supported")
    if request.if_not_exists not in {None, "reject"}:
        raise UnsupportedRunOption(
            "if_not_exists only supports reject; thread creation on run is not supported"
        )
    if request.on_completion not in {None, "keep"}:
        raise UnsupportedRunOption(
            "on_completion only supports keep; automatic thread deletion is not supported"
        )
    if request.multitask_strategy not in {None, "reject"}:
        raise UnsupportedRunOption(
            "multitask_strategy only supports reject; enqueue, interrupt, and rollback are not supported"
        )
    if request.on_disconnect not in {None, "continue", "cancel"}:
        raise UnsupportedRunOption(
            "on_disconnect only supports continue and cancel"
        )
    if request.durability not in {None, *IMPLEMENTED_DURABILITY}:
        raise UnsupportedRunOption(
            "durability only supports sync, async, and exit"
        )
    if request.checkpoint_during is not None and request.durability is not None:
        raise UnsupportedRunOption(
            "checkpoint_during cannot be combined with durability"
        )
    modes = _stream_modes(request.stream_mode)
    if not modes or any(mode not in ACCEPTED_STREAM_MODES for mode in modes):
        unknown = ", ".join(mode for mode in modes if mode not in ACCEPTED_STREAM_MODES)
        raise UnsupportedRunOption(
            "stream_mode only supports values; other stream modes are not supported"
            + (f": {unknown}" if unknown else "")
        )
    if "values" not in modes:
        raise UnsupportedRunOption(
            "stream_mode only supports values; other stream modes are not supported"
        )
    _validate_command(request.command)
    _validate_interrupt_nodes(request.interrupt_before, "interrupt_before")
    _validate_interrupt_nodes(request.interrupt_after, "interrupt_after")


def parse_event_cursor(header: str | None, query: str | None) -> int:
    """Resolve ``Last-Event-ID`` or the ``last_event_id`` query parameter."""
    raw = header if header not in {None, ""} else query
    if raw in {None, ""}:
        return 0
    try:
        cursor = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise RunCursorError() from exc
    return max(cursor, 0)


def graph_input_for(
    request: RunStreamRequest,
    *,
    interrupted: bool,
) -> Any:
    """Build the LangGraph input or command for one accepted request."""
    command = _command(request.command, interrupted=interrupted)
    if command is not None:
        return command
    if request.input is not None:
        return request.input
    return None


def disconnect_policy(request: RunStreamRequest) -> str:
    """Return whether a disconnect continues the run or cancels it."""
    return request.on_disconnect or "continue"


def run_configuration(request: RunStreamRequest) -> dict[str, Any]:
    """Persist the client config and metadata without treating them as authority."""
    stored = strip_client_secrets(dict(request.config or {}))
    stored.pop("configurable", None)
    configurable = strip_client_secrets(dict((request.config or {}).get("configurable") or {}))
    for key in _IDENTITY_KEYS:
        configurable.pop(key, None)
    if configurable:
        stored["configurable"] = configurable
    metadata = strip_client_secrets(dict(request.metadata or {}))
    for key in _IDENTITY_KEYS:
        metadata.pop(key, None)
    stored["metadata"] = metadata
    if request.command is not None:
        stored["command"] = request.command
    if request.checkpoint is not None:
        stored["checkpoint"] = request.checkpoint
    return stored


def execution_config(
    thread_id: uuid.UUID,
    request: RunStreamRequest,
    identity: TenantIdentity | None = None,
) -> dict[str, Any]:
    """Build a graph config whose thread id cannot be redirected by the client."""
    config = strip_client_secrets(dict(request.config or {}))
    configurable = strip_client_secrets(dict(config.get("configurable") or {}))
    for key in _IDENTITY_KEYS:
        configurable.pop(key, None)
    checkpoint = dict(request.checkpoint or {})
    configurable["thread_id"] = str(thread_id)
    configurable["checkpoint_ns"] = str(
        checkpoint.get("checkpoint_ns")
        or configurable.get("checkpoint_ns")
        or ""
    )
    checkpoint_id = checkpoint.get("checkpoint_id", configurable.get("checkpoint_id"))
    if checkpoint_id:
        configurable["checkpoint_id"] = str(checkpoint_id)
    else:
        configurable.pop("checkpoint_id", None)
    checkpoint_map = checkpoint.get("checkpoint_map", configurable.get("checkpoint_map"))
    if isinstance(checkpoint_map, dict):
        configurable["checkpoint_map"] = checkpoint_map
    else:
        configurable.pop("checkpoint_map", None)
    if identity is not None:
        configurable["user_id"] = identity.user_id
        configurable["thread_id"] = identity.thread_id
        configurable["run_id"] = identity.run_id
    config["configurable"] = configurable
    metadata = strip_client_secrets(dict(config.get("metadata") or {}))
    metadata.update(strip_client_secrets(dict(request.metadata or {})))
    for key in _IDENTITY_KEYS:
        metadata.pop(key, None)
    if identity is not None:
        metadata.update(identity.as_dict())
        config["run_id"] = identity.run_id
    config["metadata"] = metadata
    return config


def graph_context(
    request: RunStreamRequest,
    identity: TenantIdentity,
) -> dict[str, Any]:
    """Build LangGraph runtime context that the client cannot retarget."""
    context = strip_client_secrets(dict(request.context or {}))
    for key in (*_IDENTITY_KEYS, "thread_id"):
        context.pop(key, None)
    context.update(identity.as_dict())
    return context


def checkpoint_reference(
    thread_id: uuid.UUID,
    request: RunStreamRequest,
) -> dict[str, Any]:
    """Collect the checkpoint identity the client asked to execute from."""
    checkpoint = dict(request.checkpoint or {})
    configurable = dict((request.config or {}).get("configurable") or {})
    reference = {
        "thread_id": checkpoint.get("thread_id", configurable.get("thread_id")),
        "checkpoint_ns": checkpoint.get(
            "checkpoint_ns",
            configurable.get("checkpoint_ns"),
        ),
        "checkpoint_id": checkpoint.get(
            "checkpoint_id",
            configurable.get("checkpoint_id"),
        ),
        "checkpoint_map": checkpoint.get(
            "checkpoint_map",
            configurable.get("checkpoint_map"),
        ),
    }
    supplied_thread = reference["thread_id"]
    if supplied_thread is not None and str(supplied_thread) != str(thread_id):
        raise CheckpointReferenceError(
            "checkpoint belongs to a different thread",
            422,
        )
    return reference


async def ensure_checkpoint_belongs_to_thread(
    graph: Any,
    thread_id: uuid.UUID,
    reference: dict[str, Any],
) -> None:
    """Reject checkpoint ids that are not stored for this thread."""
    checkpoint_id = reference.get("checkpoint_id")
    if not checkpoint_id:
        return
    config = {
        "configurable": {
            "thread_id": str(thread_id),
            "checkpoint_ns": str(reference.get("checkpoint_ns") or ""),
            "checkpoint_id": str(checkpoint_id),
        }
    }
    checkpointer = getattr(graph, "checkpointer", None)
    if checkpointer is None:
        raise CheckpointReferenceError("checkpoint storage is unavailable", 503)
    saved = await checkpointer.aget_tuple(config)
    if saved is None:
        raise CheckpointReferenceError("checkpoint was not found on this thread", 404)
    saved_thread = ((saved.config or {}).get("configurable") or {}).get("thread_id")
    if saved_thread is not None and str(saved_thread) != str(thread_id):
        raise CheckpointReferenceError(
            "checkpoint belongs to a different thread",
            422,
        )


def astream_options(request: RunStreamRequest) -> dict[str, Any]:
    """Return keyword arguments implemented by the in-process graph stream."""
    options: dict[str, Any] = {
        "stream_mode": "values",
        "subgraphs": bool(request.stream_subgraphs),
        "durability": request.durability or "sync",
    }
    if request.interrupt_before is not None:
        options["interrupt_before"] = _interrupt_argument(request.interrupt_before)
    if request.interrupt_after is not None:
        options["interrupt_after"] = _interrupt_argument(request.interrupt_after)
    if request.checkpoint_during is not None and request.durability is None:
        options.pop("durability", None)
        options["checkpoint_during"] = request.checkpoint_during
    return options


def _stream_modes(value: str | list[str] | None) -> list[str]:
    if value is None:
        return ["values"]
    if isinstance(value, str):
        return [value]
    return list(value)


def _validate_command(command: dict[str, Any] | None) -> None:
    if not command:
        return
    unknown = set(command) - {"resume", "goto", "update", "graph"}
    if unknown:
        raise UnsupportedRunOption(
            "command contains unsupported fields: " + ", ".join(sorted(unknown))
        )
    if command.get("graph") not in {None, ""}:
        raise UnsupportedRunOption(
            "command.graph is not supported; only the current graph can be resumed"
        )


def _validate_interrupt_nodes(value: str | list[str] | None, field: str) -> None:
    if value is None or value == "*":
        return
    nodes = [value] if isinstance(value, str) else list(value)
    if any(not isinstance(node, str) or not node for node in nodes):
        raise UnsupportedRunOption(f"{field} must be a node name, a list of names, or '*'")


def _interrupt_argument(value: str | list[str]) -> str | list[str]:
    if value == "*":
        return "*"
    if isinstance(value, str):
        return [value]
    return list(value)


def _command(command: dict[str, Any] | None, *, interrupted: bool) -> Command | None:
    if not command:
        return None
    resume = command.get("resume")
    goto = command.get("goto")
    update = command.get("update")
    has_resume = resume is not None
    has_goto = goto not in (None, "", [], ())
    has_update = update is not None
    if not any((has_resume, has_goto, has_update)):
        return None
    if has_resume and not interrupted:
        raise ThreadNotInterrupted()
    arguments: dict[str, Any] = {}
    if has_resume:
        arguments["resume"] = resume
    if has_goto:
        arguments["goto"] = goto
    if has_update:
        arguments["update"] = update
    return Command(**arguments)
