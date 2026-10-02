"""Record the poster study's ``agrihub.run-event/v1`` events as a golden NDJSON fixture.

Runs the poster study (S5_2899164, S18_9263941, S18_51620945; plant height;
soybean Wm82.a2.v1) in-process on the built soybean bundle, without
PostgreSQL, and writes one custom-stream event per line to
``frontend/src/lib/__fixtures__/poster-run.ndjson``. The frontend reducer tests
replay that file, and ``tests/test_event_fixture.py`` fails when the events
the graph emits drift from it.

With ``--fake-llm`` every agent uses the scripted ``agrihub-fake:poster``
model and the run executes one task at a time (``max_concurrency=1``), so
evidence aliases, finding ids and event order are reproducible; the fixture
is recorded this way. Without it the configured ``MODEL`` runs the agents.

Run-specific values are normalized so re-recording only changes what the
pipeline changed:

- ``event_id`` becomes ``evt-0001``, ``evt-0002``, ... in emission order;
- specialist agent ids become ``call_<specialist>`` (``call_<specialist>_r2``
  in a follow-up round), including inside tool-call ids;
- checkpoint namespace parts ``<node>:<uuid>`` become ``<node>:<n>``, numbered
  by first appearance;
- ``ts`` is rebased to ``2026-01-01T00:00:00+00:00``. Real runs keep the
  measured offsets and ``duration_ms``. ``--fake-llm`` runs interleave the
  specialist lanes of each round round-robin (as a parallel run would show
  them), place events 100 ms apart, and derive ``duration_ms`` from that clock.

Usage::

    uv run python scripts/record_study_events.py [--fake-llm] [--output PATH]
"""

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "frontend" / "src" / "lib" / "__fixtures__" / "poster-run.ndjson"
POSTER_STUDY: dict[str, Any] = {
    "mode": "snps",
    "species": "soybean",
    "assembly": "Wm82.a2.v1",
    "trait_text": "plant height",
    "snps": [
        {"raw": "S5_2899164", "chrom": "5", "pos": 2_899_164},
        {"raw": "S18_9263941", "chrom": "18", "pos": 9_263_941},
        {"raw": "S18_51620945", "chrom": "18", "pos": 51_620_945},
    ],
}
FAKE_MODEL = "agrihub-fake:poster"
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)
TICK = timedelta(milliseconds=100)
_NAMESPACE = re.compile(r"^(?P<node>[^:]+):[0-9a-f-]{36}$")


async def record_events(study: dict[str, Any] | None = None, *, fake_llm: bool = False) -> list[dict[str, Any]]:
    """Run a study in-process and return its custom-stream events as emitted."""
    from langgraph.checkpoint.memory import InMemorySaver

    from agrihub.graph import build_study_graph

    run_id = uuid.uuid4().hex
    graph = build_study_graph(checkpointer=InMemorySaver())
    configurable: dict[str, Any] = {"thread_id": run_id, "run_id": run_id}
    config: dict[str, Any] = {"configurable": configurable}
    if fake_llm:
        configurable.update(orchestrator_model=FAKE_MODEL, specialist_model=FAKE_MODEL)
        config["max_concurrency"] = 1
    return [
        part["data"]
        async for part in graph.astream(
            {"study": study or POSTER_STUDY},
            config,
            stream_mode=["custom"],
            subgraphs=True,
            version="v2",
        )
    ]


def normalize(events: list[dict[str, Any]], *, fixed_clock: bool = False) -> list[dict[str, Any]]:
    """Replace run-specific ids and timestamps as described in the module docstring."""
    agents: dict[str, str] = {}
    for event in events:
        if event["type"] == "orchestrator.decision":
            round_number = int(event["data"].get("round") or 1)
            for item in event["data"].get("dispatched") or []:
                suffix = "" if round_number == 1 else f"_r{round_number}"
                agents.setdefault(str(item["agent_id"]), f"call_{item['specialist']}{suffix}")
    pattern = re.compile("|".join(re.escape(agent) for agent in sorted(agents, key=len, reverse=True))) if agents else None
    ordered = interleave_lanes(events) if fixed_clock else list(events)
    namespaces: dict[str, str] = {}
    first = datetime.fromisoformat(ordered[0]["ts"]) if ordered else BASE_TS
    normalized = []
    for index, event in enumerate(ordered, start=1):
        text = json.dumps(event)
        if pattern is not None:
            text = pattern.sub(lambda match: agents[match.group(0)], text)
        item = json.loads(text)
        item["event_id"] = f"evt-{index:04d}"
        if fixed_clock:
            item["ts"] = (BASE_TS + TICK * (index - 1)).isoformat()
        else:
            item["ts"] = (BASE_TS + (datetime.fromisoformat(event["ts"]) - first)).isoformat()
        item["ns"] = [_namespace(part, namespaces) for part in item.get("ns") or []]
        normalized.append(item)
    if fixed_clock:
        started = {event["agent"]["id"]: event["ts"] for event in normalized if event["type"] == "agent.started"}
        for event in normalized:
            if event["type"] == "agent.completed" and event["agent"]["id"] in started:
                elapsed = datetime.fromisoformat(event["ts"]) - datetime.fromisoformat(started[event["agent"]["id"]])
                event["data"]["duration_ms"] = int(elapsed.total_seconds() * 1000)
    return normalized


def interleave_lanes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge each run of consecutive specialist events round-robin by lane, keeping each lane's order."""
    result: list[dict[str, Any]] = []
    block: list[dict[str, Any]] = []

    def flush() -> None:
        lanes: dict[str, list[dict[str, Any]]] = {}
        for event in block:
            lanes.setdefault(event["agent"]["id"], []).append(event)
        queues = list(lanes.values())
        while any(queues):
            for queue in queues:
                if queue:
                    result.append(queue.pop(0))
        block.clear()

    for event in events:
        if event["agent"]["kind"] == "specialist":
            block.append(event)
            continue
        flush()
        result.append(event)
    flush()
    return result


def write_ndjson(events: list[dict[str, Any]], path: Path) -> None:
    """Write one compact JSON event per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_ndjson(path: Path) -> list[dict[str, Any]]:
    """Read an NDJSON fixture written by :func:`write_ndjson`."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _namespace(part: str, seen: dict[str, str]) -> str:
    match = _NAMESPACE.match(part)
    if match is None:
        return part
    if part not in seen:
        seen[part] = f"{match.group('node')}:{len(seen) + 1}"
    return seen[part]


def main() -> int:
    """Record the poster study and write the fixture."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--fake-llm", action="store_true", help=f"use the scripted {FAKE_MODEL} model and run serially")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="agrihub-record-") as run_dir:
        os.environ["AGRIHUB_RUN_DIR"] = run_dir
        events = normalize(asyncio.run(record_events(fake_llm=args.fake_llm)), fixed_clock=args.fake_llm)
    write_ndjson(events, args.output)
    sys.stdout.write(f"wrote {len(events)} events to {args.output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
