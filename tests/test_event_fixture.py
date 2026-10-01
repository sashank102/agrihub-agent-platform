"""The poster study's events must keep the shape pinned by the frontend's golden fixture.

When this fails on purpose, re-record with
``uv run python scripts/record_study_events.py`` and update the frontend types
and reducer tests in the same change.
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


def test_poster_events_match_the_golden_fixture(run_env: None):
    recorder = _recorder()
    golden = recorder.read_ndjson(FIXTURE)
    current = recorder.normalize(asyncio.run(recorder.record_events()))

    assert all(event["schema"] == events.SCHEMA for event in golden)
    assert {event["type"] for event in current} == {event["type"] for event in golden}
    expected, actual = _shape(golden), _shape(current)
    for part in expected:
        assert actual[part] == expected[part], part
    assert sorted(event["type"] for event in current) == sorted(event["type"] for event in golden)
    assert [event["event_id"] for event in golden] == [f"evt-{index:04d}" for index in range(1, len(golden) + 1)]
