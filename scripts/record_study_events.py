"""Record the poster study's ``agrihub.run-event/v1`` events as a golden NDJSON fixture.

Runs the poster study (S5_2899164, S18_9263941, S18_51620945; plant height;
soybean Wm82.a2.v1) in-process on the built soybean bundle, without
PostgreSQL or language models, and writes one custom-stream event per line to
``frontend/src/lib/__fixtures__/poster-run.ndjson``. The frontend reducer tests
replay that file, and ``tests/test_event_fixture.py`` fails when the events
the graph emits drift from it.

Run-specific values are normalized so re-recording only changes what the
pipeline changed:

- ``event_id`` becomes ``evt-0001``, ``evt-0002``, ... in emission order;
- ``ts`` is rebased to ``2026-01-01T00:00:00+00:00`` keeping the real offsets
  between events, so durations and ordering stay realistic;
- specialist agent ids ``call_<hex>`` become ``call_<specialist>``, including
  inside tool-call ids and dispatch payloads;
- checkpoint namespace parts ``<node>:<uuid>`` become ``<node>:<n>``, numbered
  by first appearance.

``duration_ms`` and the timing offsets are left as measured.

Usage::

    uv run python scripts/record_study_events.py [--output PATH]
"""

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import UTC, datetime
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
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)
_AGENT_ID = re.compile(r"call_[0-9a-f]{24}")
_NAMESPACE = re.compile(r"^(?P<node>[^:]+):[0-9a-f-]{36}$")


async def record_events(study: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run a study in-process and return its custom-stream events as emitted."""
    from langgraph.checkpoint.memory import InMemorySaver

    from agrihub.graph import build_study_graph

    run_id = uuid.uuid4().hex
    graph = build_study_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": run_id, "run_id": run_id}}
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


def normalize(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace run-specific ids and timestamps as described in the module docstring."""
    agents: dict[str, str] = {}
    for event in events:
        if event["type"] == "orchestrator.decision":
            for item in event["data"].get("dispatched") or []:
                agents.setdefault(str(item["agent_id"]), f"call_{item['specialist']}")
    namespaces: dict[str, str] = {}
    first = datetime.fromisoformat(events[0]["ts"]) if events else BASE_TS
    normalized = []
    for index, event in enumerate(events, start=1):
        text = _AGENT_ID.sub(lambda match: agents.get(match.group(0), match.group(0)), json.dumps(event))
        item = json.loads(text)
        item["event_id"] = f"evt-{index:04d}"
        item["ts"] = (BASE_TS + (datetime.fromisoformat(event["ts"]) - first)).isoformat()
        item["ns"] = [_namespace(part, namespaces) for part in item.get("ns") or []]
        normalized.append(item)
    return normalized


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
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="agrihub-record-") as run_dir:
        os.environ["AGRIHUB_RUN_DIR"] = run_dir
        events = normalize(asyncio.run(record_events()))
    write_ndjson(events, args.output)
    sys.stdout.write(f"wrote {len(events)} events to {args.output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
