"""Run-scoped evidence ledger, its agent tools, and checkpoint size."""

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from agrihub_fixtures import FixtureBundle
from langgraph.checkpoint.memory import InMemorySaver

from agrihub.evidence_store import (
    DATABASE_NAME,
    EvidenceStore,
    UnknownEvidenceError,
    close_run,
    evidence_id_for,
)
from agrihub.graph import build_study_graph
from agrihub.state import EvidenceItem, Finding, OrthologRef
from agrihub.tools.store_tools import get_evidence, record_finding

pytestmark = pytest.mark.usefixtures("fake_llm")

CATEGORIES = ("positional", "ortholog", "expression", "literature")


@pytest.fixture(autouse=True)
def run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def store(run_dir: Path):
    opened = EvidenceStore.for_run("run-a")
    yield opened
    opened.close()


def _item(gene_id: str, category: str = "ortholog", subtype: str = "best_hit", **extra: Any) -> EvidenceItem:
    return EvidenceItem(
        gene_id=gene_id,
        category=category,
        subtype=subtype,
        value=extra.pop("value", "AT1G01010"),
        source_db=extra.pop("source_db", "plaza"),
        db_version="5.0",
        source_record=extra.pop("source_record", f"{gene_id}:{subtype}"),
        **extra,
    )


def _config(run_id: str = "run-a", agent_id: str = "call_lane") -> dict[str, Any]:
    return {
        "configurable": {"run_id": run_id},
        "metadata": {"agrihub_agent_id": agent_id},
    }


def test_store_lives_in_the_run_directory(store: EvidenceStore, run_dir: Path):
    assert store.path == run_dir / "run-a" / DATABASE_NAME
    assert store.path.exists()
    assert EvidenceStore.for_run("run-a") is store
    with pytest.raises(ValueError):
        EvidenceStore("../escape")


def test_close_run_releases_the_cached_handle_without_opening_one(store: EvidenceStore, run_dir: Path):
    store.put_items([_item("g1")])
    assert close_run("run-a") is True
    with pytest.raises(RuntimeError, match="closed"):
        store.count()
    assert close_run("run-a") is False and close_run("never-opened") is False
    assert not (run_dir / "never-opened").exists()
    assert close_run("../escape") is False
    reopened = EvidenceStore.for_run("run-a")
    assert reopened is not store and reopened.count() == 1
    reopened.close()


def test_duplicate_evidence_collapses_to_one_id_and_alias(store: EvidenceStore):
    first = _item("Glyma.18G092200")
    expected = hashlib.sha1(
        b"plaza|Glyma.18G092200:best_hit|Glyma.18G092200|best_hit"
    ).hexdigest()[:12]
    assert evidence_id_for(first) == expected

    saved = store.put_items([first, _item("Glyma.18G092200", value="changed")])
    assert [item.evidence_id for item in saved] == [expected, expected]
    assert [item.alias for item in saved] == ["E1", "E1"]
    again = store.put_items([_item("Glyma.18G092200"), _item("Glyma.18G092200", subtype="other")])
    assert [item.alias for item in again] == ["E1", "E2"]
    assert store.count() == 2
    assert store.get(["E1"])[0].value == "AT1G01010"


def test_query_by_gene_and_category_and_get_by_id_or_alias(store: EvidenceStore):
    saved = store.put_items(
        _item(gene, category, subtype=category)
        for gene in ("g1", "g2", "g3")
        for category in CATEGORIES
    )
    assert store.count() == 12
    assert {item.gene_id for item in store.query(gene_ids=["g2"])} == {"g2"}
    assert [item.category for item in store.query(["g1", "g3"], ["expression"])] == [
        "expression",
        "expression",
    ]
    assert len(store.query(categories=["literature"], limit=2)) == 2
    assert store.query(gene_ids=[]) == []
    assert store.counts_by_category() == {category: 3 for category in CATEGORIES}

    ids = [saved[5].evidence_id, "E1", "missing", "E12"]
    assert [item.alias for item in store.get(ids)] == ["E6", "E1", "E12"]
    assert store.resolve(["E1", str(saved[0].evidence_id), "E99"]) == (
        [str(saved[0].evidence_id)],
        ["E99"],
    )


def test_finding_with_unknown_evidence_is_rejected(store: EvidenceStore):
    store.put_items([_item("g1")])
    with pytest.raises(UnknownEvidenceError) as caught:
        store.put_finding(
            Finding(
                agent_id="call_lane",
                target="g1",
                claim="Ortholog of a height gene.",
                stance="supports",
                strength="moderate",
                evidence_ids=["E1", "E404", "deadbeef0000"],
            )
        )
    assert caught.value.unknown == ["E404", "deadbeef0000"]
    assert "E404" in str(caught.value) and "evidence tools" in str(caught.value)
    assert store.findings() == []

    saved = store.put_finding(
        Finding(
            agent_id="call_lane",
            target="g1",
            claim="Ortholog of a height gene.",
            stance="supports",
            strength="moderate",
            evidence_ids=["E1"],
        )
    )
    assert saved.finding_id == "F1"
    assert saved.evidence_ids == [evidence_id_for(_item("g1"))]
    assert store.findings(agent_id="call_lane") == [saved]


def test_tools_read_the_run_store_and_reject_unknown_ids(store: EvidenceStore):
    store.put_items([_item("g1"), _item("L1", category="positional", subtype="span")])

    fetched = get_evidence.invoke({"ids": ["E1", "E9"]}, _config())
    assert [item["alias"] for item in fetched["evidence"]] == ["E1"]
    assert fetched["not_found"] == ["E9"]

    rejected = record_finding.invoke(
        {
            "target": "g1",
            "claim": "Supported by an unknown record.",
            "stance": "supports",
            "strength": "strong",
            "evidence_ids": ["E1", "E9"],
        },
        _config(),
    )
    assert isinstance(rejected, str)
    assert rejected.startswith("Finding rejected. Unknown evidence ids: E9.")
    assert store.findings() == []

    recorded = record_finding.invoke(
        {
            "target": "L1",
            "claim": "The locus spans one candidate.",
            "stance": "neutral",
            "strength": "weak",
            "evidence_ids": ["E2"],
        },
        _config(agent_id="call_other"),
    )
    assert recorded["status"] == "recorded"
    finding = store.findings()[0]
    assert (finding.agent_id, finding.target_type) == ("call_other", "locus")


def test_snapshot_export_round_trip(store: EvidenceStore, run_dir: Path):
    store.put_items(_item(f"g{index}", category, subtype=category) for index in range(3) for category in CATEGORIES)
    store.put_finding(
        Finding(
            agent_id="call_lane",
            target="g0",
            claim="Two lines of evidence.",
            stance="supports",
            strength="moderate",
            evidence_ids=["E1", "E2"],
        )
    )
    path = store.export()
    assert path == run_dir / "run-a" / "evidence_snapshot.json"
    exported = json.loads(path.read_text(encoding="utf-8"))
    assert exported == store.snapshot()

    restored = EvidenceStore.restore(exported, run_id="run-b")
    try:
        copy = restored.snapshot()
        assert copy["run_id"] == "run-b"
        assert {**copy, "run_id": "run-a"} == exported
        assert restored.get(["E3"]) == store.get(["E3"])
    finally:
        restored.close()


class _RecordingConnection:
    """Delegate to a DuckDB connection and keep every statement it runs."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.statements: list[str] = []

    def execute(self, statement: str, *args: Any) -> Any:
        self.statements.append(" ".join(statement.split()))
        return self.connection.execute(statement, *args)

    def close(self) -> None:
        self.connection.close()


@pytest.mark.parametrize("batch", [3, 500])
def test_put_items_finds_duplicates_without_reading_every_stored_id(store: EvidenceStore, batch: int):
    store.put_items(_item(f"old{index}") for index in range(2_000))
    recorder = _RecordingConnection(store._connection)
    store._connection = recorder  # type: ignore[assignment]

    mixed = [_item(f"old{index}") for index in range(batch)] + [_item(f"new{index}") for index in range(batch)]
    saved = store.put_items(mixed)

    assert [item.alias for item in saved[:batch]] == [f"E{index + 1}" for index in range(batch)]
    assert [item.alias for item in saved[batch:]] == [f"E{2_001 + index}" for index in range(batch)]
    reads = [statement for statement in recorder.statements if "FROM evidence" in statement]
    assert store.count() == 2_000 + batch
    assert reads
    assert all(
        "WHERE evidence_id IN" in statement or "SEMI JOIN" in statement for statement in reads
    ), reads
    assert not any("max(ordinal)" in statement for statement in recorder.statements)


def test_via_ortholog_is_structured_and_round_trips(store: EvidenceStore):
    ortholog = OrthologRef(
        species="arabidopsis",
        gene_id="AT1G80840",
        relation="one2one",
        n_methods=3,
        confidence="high",
    )
    saved = store.put_items([_item("Glyma.18G092200", via_ortholog=ortholog)])[0]
    fetched = store.get([str(saved.alias)])[0]
    assert fetched.via_ortholog == ortholog
    assert fetched.model_dump(mode="json")["via_ortholog"]["n_methods"] == 3
    with pytest.raises(ValueError):
        _item("g1", via_ortholog="AT1G80840")


def test_outputs_keep_full_tool_results(store: EvidenceStore):
    first = store.put_output("genes_in_window", {"rows": [{"gene_id": "g1"}], "total": 1})
    second = store.put_output("qtl_overlap", {"rows": []})
    assert (first, second) == ("O1", "O2")
    assert store.get_output("O1") == {"tool": "genes_in_window", "rows": [{"gene_id": "g1"}], "total": 1}
    assert store.get_output("O9") is None


def test_store_tools_run_off_the_event_loop(store: EvidenceStore):
    store.put_items([_item("g1")])

    async def scenario() -> tuple[dict[str, Any], set[str]]:
        threads: set[str] = set()
        original = store.get

        def recording_get(ids: Any) -> Any:
            import threading

            threads.add(threading.current_thread().name)
            return original(ids)

        store.get = recording_get  # type: ignore[method-assign]
        try:
            return await get_evidence.ainvoke({"ids": ["E1"]}, _config()), threads
        finally:
            del store.get

    fetched, threads = asyncio.run(scenario())
    assert [item["alias"] for item in fetched["evidence"]] == ["E1"]
    assert threads and "MainThread" not in threads


def test_checkpoint_stays_flat_with_a_thousand_evidence_items(fixture_env: FixtureBundle):
    study = {
        "mode": "snps",
        "species": "soybean",
        "assembly": "Wm82.a2.v1",
        "trait_text": "plant height",
        "snps": [{"raw": "S5_2899164", "chrom": "5", "pos": 2_899_164}],
    }

    def checkpoint_bytes(extra_items: int) -> tuple[int, int]:
        run_id = uuid.uuid4().hex
        if extra_items:
            bulk = EvidenceStore.for_run(run_id)
            bulk.put_items(
                _item(f"bulk{index}", CATEGORIES[index % 4], quote="q" * 200)
                for index in range(extra_items)
            )
            assert bulk.count() == extra_items

        async def scenario() -> tuple[int, int]:
            saver = InMemorySaver()
            graph = build_study_graph(checkpointer=saver)
            config = {"configurable": {"thread_id": run_id, "run_id": run_id}}
            await graph.ainvoke({"study": study}, config)
            saved = saver.get_tuple(config)
            assert saved is not None
            _, blob = saver.serde.dumps_typed(saved.checkpoint["channel_values"])
            evidence = (await graph.aget_state(config)).values["report"]["evidence_count"]
            return len(blob), evidence

        return asyncio.run(scenario())

    baseline, baseline_evidence = checkpoint_bytes(0)
    loaded, loaded_evidence = checkpoint_bytes(1_000)
    assert loaded_evidence == baseline_evidence + 1_000
    assert baseline < 32_000
    assert abs(loaded - baseline) < 256
