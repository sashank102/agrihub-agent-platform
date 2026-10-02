"""End-to-end deterministic studies on the real soybean core bundle, without language models."""

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agent_platform.core.settings import get_data_paths
from agrihub.evidence_store import EvidenceStore
from agrihub.graph import build_study_graph
from agrihub_data.bundle import close_bundles, open_bundle

BUNDLE = get_data_paths().data_dir / "soybean" / "bundle.duckdb"
POSTER = {
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

pytestmark = [
    pytest.mark.usefixtures("fake_llm"),
    pytest.mark.bundle,
    pytest.mark.skipif(not BUNDLE.exists(), reason=f"no soybean bundle at {BUNDLE}"),
]


@pytest.fixture(autouse=True)
def run_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path))
    yield
    close_bundles()


def _study(study: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], EvidenceStore, float]:
    run_id = uuid.uuid4().hex

    async def scenario() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        graph = build_study_graph(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": run_id, "run_id": run_id}}
        received = [
            part["data"]
            async for part in graph.astream({"study": study}, config, stream_mode=["custom"], subgraphs=True, version="v2")
        ]
        return received, dict((await graph.aget_state(config)).values)

    started = time.monotonic()
    received, values = asyncio.run(scenario())
    return received, values, EvidenceStore.for_run(run_id), time.monotonic() - started


def test_poster_study_ranks_real_loci_with_explanations_in_under_30_seconds():
    received, values, store, elapsed = _study(POSTER)
    assert elapsed < 30, elapsed
    report = values["report"]
    loci = {locus["lead_snp"]: locus for locus in report["loci"]}
    assert {snp: locus["n_genes"] for snp, locus in loci.items()} == {
        "S5_2899164": 50,
        "S18_9263941": 31,
        "S18_51620945": 42,
    }
    wrky_locus = loci["S18_9263941"]["locus_id"]
    candidates = {ref["gene_id"]: ref for ref in values["candidates"]}
    assert candidates["Glyma.18G092200"]["locus_id"] == wrky_locus
    assert candidates["Glyma.18G092200"]["distance_bp"] == 0
    ranked = {item["gene_id"]: item for item in report["candidates"]}
    wrky_ranked = ranked["Glyma.18G092200"]
    assert wrky_ranked["chrom"] == "Gm18" and wrky_ranked["start"] <= 9_263_941 <= wrky_ranked["end"]
    assert wrky_ranked["distance_bp"] == 0 and wrky_ranked["overlaps_snp"] is True
    assert wrky_ranked["lead_snp"] == wrky_ranked["nearest_snp"] == "S18_9263941"
    assert "WRKY" in wrky_ranked["defline"].upper()
    assert all(item["chrom"] and item["distance_bp"] is not None for item in report["candidates"])
    explanations = store.get_output(report["provenance"]["score_explanations_ref"])
    wrky = next(row for row in explanations["rows"] if row["gene_id"] == "Glyma.18G092200")
    assert wrky["locus_id"] == wrky_locus and wrky["categories"]["A"]["points"] == 20
    assert wrky["categories"]["A"]["evidence_ids"] and wrky["categories"]["E"]["available"] is False
    assert {candidate["locus_id"] for candidate in report["candidates"]} == {locus["locus_id"] for locus in report["loci"]}
    assert values["triage_brief"]["token_estimate"] <= 4_000
    progress = [event["data"] for event in received if event["type"] == "evidence.progress"]
    assert all(event["done"] == event["total"] == 123 for event in progress if event["done"] == event["total"])
    assert {event["agent"]["id"] for event in received if event["type"] == "agent.usage"}
    assert {event["data"]["model"] for event in received if event["type"] == "agent.usage"} == {"agrihub-fake:poster"}
    assert any("Known-gene coverage is thin" in item for item in report["limitations"])
    store.close()


@pytest.mark.parametrize(
    ("trait", "gene_id", "offset"),
    [
        ("plant height", "Glyma.19G194300", 50_000),
        ("plant height", "Glyma.19G194300", 150_000),
        ("flowering time", "Glyma.06G207800", 100_000),
        ("flowering time", "Glyma.06G207800", -150_000),
    ],
)
def test_snps_near_a_known_trait_gene_rank_it_in_the_top_three(trait: str, gene_id: str, offset: int):
    gene = open_bundle("soybean").rows(
        'SELECT chrom, start, "end" FROM genes WHERE assembly = ? AND gene_id = ?', ["Wm82.a2.v1", gene_id]
    )[0]
    pos = int(gene["end"]) + offset if offset > 0 else int(gene["start"]) + offset
    study = {**POSTER, "trait_text": trait, "snps": [{"raw": f"syn{offset}", "chrom": gene["chrom"], "pos": pos}]}
    _, values, store, _ = _study(study)
    ranked = sorted(values["report"]["candidates"], key=lambda item: item["rank_in_locus"])
    top = {item["gene_id"]: item for item in ranked if item["rank_in_locus"] <= 3}
    assert gene_id in top, [(item["gene_id"], item["score"]) for item in ranked]
    assert top[gene_id]["tier"] == "T1"
    store.close()
