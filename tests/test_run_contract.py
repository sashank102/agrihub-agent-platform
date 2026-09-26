"""Pure checks for thread status and accepted run fields."""

import pytest
from langgraph.types import Command

from agent_platform.api.schemas import RunStreamRequest
from agent_platform.services.errors import (
    RunCursorError,
    ThreadNotInterrupted,
    UnsupportedRunOption,
)
from agent_platform.services.run_fields import (
    graph_input_for,
    parse_event_cursor,
    validate_run_stream_request,
)
from agent_platform.services.snapshots import agent_protocol_status


def _request(**overrides) -> RunStreamRequest:
    payload = {"assistant_id": "agrihub", "input": {"messages": []}}
    payload.update(overrides)
    return RunStreamRequest.model_validate(payload)


def test_protocol_status_follows_active_failed_and_interrupted_runs():
    assert agent_protocol_status(
        has_active_run=True,
        latest_run_status="failed",
        checkpoint_interrupted=True,
    ) == "busy"
    assert agent_protocol_status(
        has_active_run=False,
        latest_run_status="interrupted",
        checkpoint_interrupted=False,
    ) == "interrupted"
    assert agent_protocol_status(
        has_active_run=False,
        latest_run_status="completed",
        checkpoint_interrupted=True,
    ) == "interrupted"
    assert agent_protocol_status(
        has_active_run=False,
        latest_run_status="failed",
        checkpoint_interrupted=False,
    ) == "error"
    assert agent_protocol_status(
        has_active_run=False,
        latest_run_status="completed",
        checkpoint_interrupted=False,
    ) == "idle"
    assert agent_protocol_status(
        has_active_run=False,
        latest_run_status=None,
        checkpoint_interrupted=False,
    ) == "idle"


def test_unsupported_run_fields_are_rejected():
    for field, value in (
        ("webhook", "https://example.test/hook"),
        ("after_seconds", 5),
        ("feedback_keys", ["quality"]),
        ("if_not_exists", "create"),
        ("on_completion", "delete"),
        ("multitask_strategy", "enqueue"),
        ("on_disconnect", "abandon"),
        ("stream_mode", ["messages"]),
        ("durability", "memory"),
    ):
        with pytest.raises(UnsupportedRunOption):
            validate_run_stream_request(_request(**{field: value}))

    command = _request(command={"graph": "parent"})
    with pytest.raises(UnsupportedRunOption):
        validate_run_stream_request(command)


def test_implemented_and_explicitly_ignored_fields_are_accepted():
    request = _request(
        stream_mode=["values", "updates"],
        stream_subgraphs=True,
        stream_resumable=False,
        metadata={"source": "test"},
        durability="sync",
        multitask_strategy="reject",
        on_disconnect="continue",
        on_completion="keep",
        if_not_exists="reject",
        interrupt_before="*",
        interrupt_after=["respond"],
        context={"locale": "en"},
    )
    validate_run_stream_request(request)

    omitted = _request()
    validate_run_stream_request(omitted)
    resumed = graph_input_for(
        _request(command={"resume": {"decisions": [{"type": "approve"}]}}),
        interrupted=True,
    )
    assert isinstance(resumed, Command)
    with pytest.raises(ThreadNotInterrupted):
        graph_input_for(
            _request(command={"resume": {"decisions": [{"type": "reject"}]}}),
            interrupted=False,
        )


def test_event_cursor_accepts_the_header_or_query_form():
    assert parse_event_cursor("4", "1") == 4
    assert parse_event_cursor(None, "3") == 3
    assert parse_event_cursor(None, "-1") == 0
    assert parse_event_cursor(None, None) == 0
    with pytest.raises(RunCursorError):
        parse_event_cursor("next", None)
