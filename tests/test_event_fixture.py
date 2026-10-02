"""The poster study's events must keep the shape pinned by the frontend's golden fixture.

The fixture is recorded with the scripted fake LLM. When this fails on
purpose, re-record with
``uv run python scripts/record_study_events.py --fake-llm`` and update the
frontend types and reducer tests in the same change.
"""

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from agent_platform.core.settings import get_data_paths
from agrihub import events
from agrihub_data.bundle import close_bundles

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "frontend" / "src" / "lib" / "__fixtures__" / "poster-run.ndjson"
BUNDLE = get_data_paths().data_dir / "soybean" / "bundle.duckdb"

pytestmark = [
    pytest.mark.usefixtures("fake_llm"),
    pytest.mark.bundle,
    pytest.mark.skipif(not BUNDLE.exists(), reason=f"no soybean bundle at {BUNDLE}"),
]


def _recorder() -> Any:
    spec = importlib.util.spec_from_file_location("record_study_events", PROJECT_ROOT / "scripts" / "record_study_events.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _shape(received: list[dict[str, Any]]) -> dict[str, set[Any]]:
    """Event types with their data key sets, plus the keys of list-of-object fields."""
    envelopes: set[tuple[str, ...]] = set()
    data_keys: set[tuple[str, tuple[str, ...]]] = set()
    item_keys: set[tuple[str, str, tuple[str, ...]]] = set()
    agents: set[tuple[str, str]] = set()
    for event in received:
        envelopes.add(tuple(sorted(event)))
        data = event["data"]
        data_keys.add((event["type"], tuple(sorted(data))))
        agents.add((event["type"], event["agent"]["kind"]))
        for key, value in data.items():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        item_keys.add((event["type"], key, tuple(sorted(item))))
    return {"envelopes": envelopes, "data_keys": data_keys, "item_keys": item_keys, "agents": agents}


@pytest.fixture
def run_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path))
    yield
    close_bundles()


def _record(recorder: Any) -> list[dict[str, Any]]:
    return recorder.normalize(asyncio.run(recorder.record_events(fake_llm=True)), fixed_clock=True)


def test_poster_events_match_the_golden_fixture(run_env: None):
    recorder = _recorder()
    golden = recorder.read_ndjson(FIXTURE)
    current = _record(recorder)

    assert all(event["schema"] == events.SCHEMA for event in golden)
    assert {event["type"] for event in current} == {event["type"] for event in golden}
    expected, actual = _shape(golden), _shape(current)
    for part in expected:
        assert actual[part] == expected[part], part
    assert sorted(event["type"] for event in current) == sorted(event["type"] for event in golden)
    assert [event["event_id"] for event in golden] == [f"evt-{index:04d}" for index in range(1, len(golden) + 1)]


def test_fake_llm_recordings_are_identical_after_normalization(run_env: None):
    recorder = _recorder()
    first, second = _record(recorder), _record(recorder)
    assert first == second
    decisions = [event["data"] for event in first if event["type"] == "orchestrator.decision"]
    assert [decision["kind"] for decision in decisions] == ["dispatch", "followup", "finish"]
    dispatched = decisions[0]["dispatched"]
    assert len(dispatched) >= 3
    assert len({tuple(sorted(item["focus_gene_ids"])) for item in dispatched}) == len(dispatched)
    assert all(item["rationale"] and item["instructions"] for item in dispatched)
    assert first == recorder.read_ndjson(FIXTURE)
