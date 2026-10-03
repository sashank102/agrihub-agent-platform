"""Follow-up Q&A reads the finished study and does not change it."""

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from agrihub.evidence_store import EvidenceStore, evidence_id_for
from agrihub.nodes.followup import followup_qa, open_store
from agrihub.state import EvidenceItem

pytestmark = pytest.mark.usefixtures("fake_llm")


def _item() -> EvidenceItem:
    item = EvidenceItem(
        gene_id="Glyma.18G092200",
        category="positional",
        subtype="in_window",
        value={"dist_to_snp": 0},
        source_db="LIS",
        db_version="1",
        source_record="Gm18",
        quote="The lead SNP overlaps the gene.",
        alias="E1",
    )
    return item.model_copy(update={"evidence_id": evidence_id_for(item), "alias": "E1"})


def _state(evidence_id: str, snapshot: dict | None = None) -> dict:
    candidate = {
        "rank": 1,
        "gene_id": "Glyma.18G092200",
        "locus_id": "L1",
        "score": 20,
        "tier": "T4",
        "shortlist": True,
        "evidence_ids": [evidence_id],
        "category_points": {"A": 20},
        "verifier_status": "unverified",
    }
    return {
        "followup": "why is Glyma.18G092200 a candidate?",
        "study": {"species": "soybean", "trait_text": "plant height"},
        "ranking": [candidate],
        "report": {
            "title": "plant height",
            "species": "soybean",
            "assembly": "Wm82.a2.v1",
            "trait": "plant height",
            "mode": "snps",
            "candidates": [candidate],
            "candidates_full": [candidate],
            "markdown": "Glyma.18G092200 overlaps the lead SNP.",
        },
        "evidence_snapshot": snapshot,
    }


def test_followup_does_not_change_the_study(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path))
    store = EvidenceStore.for_run("qa-run")
    saved = store.put_items([_item()])[0]
    state = _state(str(saved.evidence_id))
    before_study = dict(state["study"])
    before_report = dict(state["report"])
    before = store.snapshot()

    async def ask():
        return await followup_qa(state, {"configurable": {"run_id": "qa-run", "thread_id": "thread"}})

    result = asyncio.run(ask())
    assert state["study"] == before_study
    assert state["report"] == before_report
    assert "study" not in result and "report" not in result and "ranking" not in result
    assert result["followup"] == ""
    text = result["messages"][-1].content
    assert "[E1]" in text
    again = EvidenceStore.for_run("qa-run")
    assert again.snapshot()["evidence"] == before["evidence"]
    assert again.findings() == []
    again.close()
    store.close()


def test_followup_rehydrates_a_missing_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path))
    origin = EvidenceStore.for_run("origin")
    origin.put_items([_item()])
    snapshot = origin.snapshot()
    origin.close()
    state = _state(snapshot["evidence"][0]["evidence_id"], snapshot)
    restored = asyncio.run(open_store({"configurable": {"run_id": "restored-run"}}, state))
    try:
        assert restored.count() == 1
        assert restored.get(["E1"])[0].gene_id == "Glyma.18G092200"
    finally:
        restored.close()


def _study_store(run_id: str) -> tuple[str, dict]:
    store = EvidenceStore.for_run(run_id)
    saved = store.put_items([_item()])[0]
    snapshot = store.snapshot()
    store.export()
    store.close()
    return str(saved.evidence_id), snapshot


def test_a_followup_run_opens_the_study_runs_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path))
    evidence_id, _ = _study_store("study-run")
    state = {**_state(evidence_id), "study_run_id": "study-run", "evidence_snapshot_id": "a1"}

    async def never(*args, **kwargs):
        raise AssertionError("the study run's database exists; the artifact must not be read")

    opened = asyncio.run(open_store({"configurable": {"run_id": "followup-run"}}, state, never))
    try:
        assert opened.directory == tmp_path / "study-run"
        assert opened.get(["E1"])[0].evidence_id == evidence_id
    finally:
        opened.close()
    assert not (tmp_path / "followup-run").exists()


def test_a_deleted_study_run_dir_is_restored_from_the_artifact_then_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path))
    evidence_id, snapshot = _study_store("study-run")
    shutil.rmtree(tmp_path / "study-run")
    state = {**_state(evidence_id), "study_run_id": "study-run", "evidence_snapshot_id": "a1"}
    calls: list[dict] = []

    async def reader(config, *, kind, artifact_id=None):
        calls.append({"kind": kind, "artifact_id": artifact_id})
        return snapshot

    restored = asyncio.run(open_store({"configurable": {"run_id": "followup-run"}}, state, reader))
    try:
        assert calls == [{"kind": "evidence_snapshot", "artifact_id": "a1"}]
        assert restored.directory == tmp_path / "study-run"
        assert restored.get(["E1"])[0].evidence_id == evidence_id
    finally:
        restored.close()

    os.remove(tmp_path / "study-run" / "evidence.duckdb")
    (tmp_path / "study-run" / "evidence_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    async def empty(*args, **kwargs):
        return None

    from_file = asyncio.run(open_store({"configurable": {"run_id": "followup-run"}}, state, empty))
    try:
        assert from_file.get(["E1"])[0].evidence_id == evidence_id
    finally:
        from_file.close()
