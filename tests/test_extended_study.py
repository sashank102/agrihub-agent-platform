"""The study graph with the scripted model on core-only and heavy fixture bundles."""

import asyncio
import uuid
from typing import Any

import pytest
from agrihub_fixtures import FixtureBundle
from langgraph.checkpoint.memory import InMemorySaver

from agrihub.evidence_store import EvidenceStore
from agrihub.graph import build_study_graph
from agrihub.study_preview import preview_study
from agrihub_data import availability, external

PLINK2 = external.plink2_path()

STUDY = {
    "mode": "snps",
    "species": "soybean",
    "assembly": "Wm82.a2.v1",
    "trait_text": "plant height",
    "snps": [
        {"raw": "S5_2899164", "chrom": "5", "pos": 2_899_164},
        {"raw": "S18_9263941", "chrom": "18", "pos": 9_263_941},
    ],
}


def _run(study: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    run_id = uuid.uuid4().hex
    received: list[dict[str, Any]] = []

    async def scenario() -> dict[str, Any]:
        graph = build_study_graph(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": run_id, "run_id": run_id, "max_rounds": 1}, "max_concurrency": 1}
        async for part in graph.astream({"study": study or STUDY}, config, stream_mode=["custom"], subgraphs=True, version="v2"):
            received.append(part["data"])
        return dict((await graph.aget_state(config)).values)

    return received, asyncio.run(scenario()), run_id


def _tools_by_lane(received: list[dict[str, Any]]) -> dict[str, set[str]]:
    lanes: dict[str, set[str]] = {}
    for event in received:
        if event["type"] == "tool.started":
            lanes.setdefault(event["agent"]["id"].rsplit("-", 1)[-1], set()).add(event["data"]["name"])
    return lanes


@pytest.fixture
def no_heavy_binaries(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_PLINK2", "/nonexistent/plink2")
    monkeypatch.setenv("AGRIHUB_VEP_IMAGE", "agrihub-test/no-such-vep:0")
    monkeypatch.delenv("AGRIHUB_VEP", raising=False)
    monkeypatch.delenv("AGRIHUB_SNPEFF", raising=False)
    external.reset_probes()
    availability.clear_cache()
    yield
    external.reset_probes()
    availability.clear_cache()


LD_STUDY = {**STUDY, "window": {"mode": "ld", "flank_bp": 250_000, "r2": 0.2}}


def test_ld_mode_falls_back_to_fixed_windows_without_plink2(heavy_env: FixtureBundle, no_heavy_binaries: None):
    preview = preview_study(LD_STUDY)
    codes = [warning["code"] for warning in preview["warnings"]]
    assert "ld_unavailable" in codes and codes.count("ld_unavailable") == 3
    assert {locus["window_method"] for locus in preview["preview"]["loci"]} == {"fixed"}


@pytest.mark.skipif(PLINK2 is None, reason="PLINK2 is not installed (scripts/setup_heavy_tools.sh)")
def test_ld_mode_builds_ld_windows_and_stores_gene_r2(heavy_env: FixtureBundle, fake_llm: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_PLINK2", str(PLINK2))
    availability.clear_cache()
    preview = preview_study(LD_STUDY)
    by_snp = {locus["lead_snp"]: locus for locus in preview["preview"]["loci"]}
    assert by_snp["S18_9263941"]["window_method"] == "ld"
    assert (by_snp["S18_9263941"]["start"], by_snp["S18_9263941"]["end"]) == (9_200_000, 9_500_000)
    assert by_snp["S5_2899164"]["window_method"] == "fixed"
    assert any(warning["code"] == "ld_proxy" for warning in preview["warnings"])
    _, values, run_id = _run(LD_STUDY)
    items = EvidenceStore.for_run(run_id).query(categories=["positional"])
    linked = {item.gene_id: item.value for item in items if item.subtype.startswith("ld_r2:")}
    assert linked["Glyma.18G092200"]["r2"] == 1.0 and linked["Glyma.18G092000"]["via"] == "ss2"
    assert any(item.subtype.startswith("ld_window:") and item.gene_id == "S18_9263941" for item in items)
    ranked = {row["gene_id"]: row for row in values["ranking"]}
    assert ranked["Glyma.18G092000"]["category_points"]["A"] >= 20.0


def test_core_only_bundle_declines_expression_network(fixture_env: FixtureBundle, fake_llm: str):
    received, values, _ = _run()
    decision = next(event["data"] for event in received if event["type"] == "orchestrator.decision")
    assert "expression_network" not in {item["specialist"] for item in decision["dispatched"]}
    reason = next(item["reason"] for item in decision["rejected"] if item["specialist"] == "expression_network")
    assert "no expression, samples rows" in reason and "build the extended tier" in reason
    candidate = values["ranking"][0]
    assert candidate["category_points"]["E"] == 0 and candidate["category_points"]["F"] == 0


def test_heavy_bundle_runs_every_specialist_and_reports_missing_binaries(heavy_env: FixtureBundle, fake_llm: str, no_heavy_binaries: None):
    received, values, run_id = _run()
    assert values["run_status"] == "completed" and values["report"]
    decision = next(event["data"] for event in received if event["type"] == "orchestrator.decision")
    dispatched = {item["specialist"]: item for item in decision["dispatched"]}
    assert "expression_network" in dispatched and dispatched["expression_network"]["rationale"]
    lanes = _tools_by_lane(received)
    assert {"trait_relevant_tissues", "tissue_specificity", "expression_profile", "seed_propagation", "coexpression_neighbors", "get_regulation", "get_pathways"} <= lanes["expression_network"]
    assert {"annotate_variants", "snp_in_tfbs_or_cns", "homeologs", "gene_haplotypes"} <= lanes["locus_variant"]
    assert "ld_with_lead" not in lanes["locus_variant"]
    completed = {event["agent"]["id"].rsplit("-", 1)[-1]: event["data"] for event in received if event["type"] == "agent.completed"}
    assert all(item["status"] == "completed" for item in completed.values())
    gaps = completed["locus_variant"]["summary"]
    assert "Not available in this build" in gaps and "LD with the lead SNP" in gaps and "variant consequences" in gaps
    assert "Not available in this build" not in completed["expression_network"]["summary"]

    store = EvidenceStore.for_run(run_id)
    findings = store.findings()
    assert findings and any(finding.agent_id.endswith("expression_network") for finding in findings)
    for finding in findings:
        known, unknown = store.resolve(finding.evidence_ids)
        assert unknown == [] and len(known) == len(finding.evidence_ids)
    by_gene = {row["gene_id"]: row for row in values["ranking"]}
    wrky = by_gene["Glyma.18G092200"]["category_points"]
    assert wrky["E"] > 0 and wrky["F"] > 0 and wrky["A"] > 0
    explanations = store.get_output(values["scoring"]["explanations_ref"]) or {}
    explained = {row["gene_id"]: row for row in explanations["rows"]}["Glyma.18G092200"]["categories"]
    assert explained["E"]["available"] and explained["F"]["available"] and explained["E"]["evidence_ids"]
