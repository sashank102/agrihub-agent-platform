"""Harvest keys every fact to a gene, streams progress per step, and keeps the brief small."""

import asyncio
import uuid
from typing import Any

import pytest
from agrihub_fixtures import FixtureBundle
from langgraph.checkpoint.memory import InMemorySaver

from agrihub.evidence_store import EvidenceStore
from agrihub.graph import build_study_graph
from agrihub.nodes.harvest import BRIEF_TOKEN_BUDGET, STEPS, brief_tokens, fit_brief
from agrihub_data.bundle import open_bundle

pytestmark = pytest.mark.usefixtures("fake_llm")

STUDY = {
    "mode": "snps",
    "species": "soybean",
    "assembly": "Wm82.a2.v1",
    "trait_text": "plant height",
    "snps": [
        {"raw": "S5_2899164", "chrom": "5", "pos": 2_899_164},
        {"raw": "S18_9263941", "chrom": "18", "pos": 9_263_941},
        {"raw": "S19_45150000", "chrom": "Gm19", "pos": 45_150_000},
    ],
}


@pytest.fixture
def harvested(fixture_env: FixtureBundle) -> dict[str, Any]:
    run_id = uuid.uuid4().hex

    async def scenario() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        graph = build_study_graph(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": run_id, "run_id": run_id}}
        received = [
            part["data"]
            async for part in graph.astream({"study": STUDY}, config, stream_mode=["custom"], subgraphs=True, version="v2")
        ]
        return received, dict((await graph.aget_state(config)).values)

    received, values = asyncio.run(scenario())
    store = EvidenceStore.for_run(run_id)
    yield {"events": received, "values": values, "evidence": store.query()}
    store.close()


def test_every_evidence_item_is_keyed_to_a_gene_on_the_study_assembly(harvested: dict[str, Any]):
    evidence = harvested["evidence"]
    gene_ids = sorted({item.gene_id for item in evidence})
    rows = open_bundle("soybean").rows_raw(
        f"SELECT gene_id FROM genes WHERE assembly = 'Wm82.a2.v1' AND gene_id IN ({', '.join('?' for _ in gene_ids)})",
        gene_ids,
    )
    assert {str(row[0]) for row in rows} == set(gene_ids)
    association = [item for item in evidence if item.category == "association"]
    assert association and not any(item.gene_id.startswith("L") for item in evidence)
    wrky = {(item.source_db, item.value["distance_to_core"]) for item in association if item.gene_id == "Glyma.18G092200"}
    assert ("GWAS Atlas", 0) in wrky and ("LIS/SoyBase GWAS", 12_391) in wrky
    assert {item.category for item in evidence} >= {"positional", "functional_annotation", "ortholog", "association", "known_gene"}


def test_progress_events_reach_the_total_for_every_step(harvested: dict[str, Any]):
    total = len(harvested["values"]["candidates"])
    progress: dict[str, list[tuple[int, int]]] = {}
    for event in harvested["events"]:
        if event["type"] == "evidence.progress":
            progress.setdefault(event["data"]["category"], []).append((event["data"]["done"], event["data"]["total"]))
    assert set(progress) == {step.category for step in STEPS if step.domain in {None, "variant_location"}}
    for steps in progress.values():
        assert [done for done, _ in steps] == sorted(done for done, _ in steps)
        assert steps[-1] == (total, total)


def test_the_triage_brief_is_under_budget_and_names_known_genes_and_gaps(harvested: dict[str, Any]):
    brief = harvested["values"]["triage_brief"]
    assert brief["token_estimate"] == brief_tokens(brief) <= BRIEF_TOKEN_BUDGET
    by_locus = {entry["locus_id"]: entry for entry in brief["loci"]}
    assert set(brief["top_genes"]) == set(by_locus) == {"L1", "L2", "L3"}
    dt1 = by_locus["L3"]
    assert dt1["top"][0]["gene_id"] == "Glyma.19G194300" and dt1["top"][0]["tier"] == "T1"
    assert dt1["known_genes"] == [{"gene_id": "Glyma.19G194300", "symbols": ["GmDT1", "GmTFL1b"], "trait_match": "ontology"}]
    assert "interval QTLs" in by_locus["L2"]["context"]["qtl"]
    assert "expression" not in brief["domains"]["available"]
    assert "build the extended tier" in brief["domains"]["unavailable"]["expression"]
    assert "known_gene" in by_locus["L1"]["no_coverage"] or by_locus["L1"]["known_genes"]
    assert brief["matrix_ref"].startswith("O")


def test_fit_brief_trims_until_the_brief_fits():
    entry = {
        "locus_id": "L1",
        "top": [{"gene_id": f"g{index}", "why": ["x" * 200] * 3, "flags": ["f" * 100]} for index in range(5)],
        "positional_only": {"count": 200, "gene_ids": [f"p{index}" for index in range(8)]},
        "known_genes": [{"gene_id": f"k{index}"} for index in range(6)],
        "context": {"qtl": "q" * 300, "gwas": "g" * 300},
    }
    brief = {"loci": [{**entry, "locus_id": f"L{index}"} for index in range(30)]}
    assert brief_tokens(brief) > 1_500
    fitted = fit_brief(brief, budget=1_500)
    assert fitted["token_estimate"] <= 1_500
    assert all(len(locus["top"]) >= 1 for locus in fitted["loci"])
