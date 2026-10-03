"""Trait mode: the model registry, its adapters and the model agent through the study graph."""

import asyncio
import csv
import uuid
from pathlib import Path
from typing import Any

import pytest
from agrihub_fixtures import FixtureBundle
from langgraph.checkpoint.memory import InMemorySaver

from agent_platform.core.settings import get_data_paths
from agrihub.graph import build_study_graph
from agrihub.state import TraitStudy
from agrihub.trait_models.adapters import (
    CatalogTopHitsAdapter,
    PlannedAdapter,
    PrecomputedResultsAdapter,
    adapter_for,
    evaluate_models,
)
from agrihub.trait_models.registry import load_model_registry
from agrihub_data.bundle import close_bundles

pytestmark = pytest.mark.usefixtures("fake_llm")
PRECOMPUTED = "sister_team_kinship_gnnexplainer"
HEADER = ["rs", "chrom", "pos", "maf", "score", "rank", "method", "graph", "trait_full", "trait", "year"]
REAL_RESULTS = Path(__file__).resolve().parents[2] / "Results"
REAL_BUNDLE = get_data_paths().data_dir / "soybean" / "bundle.duckdb"


@pytest.fixture
def results_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a sister-team PH_2019 file whose Lee.gnm2 positions the fixture anchors can lift."""
    root = tmp_path / "results"
    folder = root / "2_Sep" / "Lee"
    folder.mkdir(parents=True)
    rows = [
        ["S18_10263941", "18", "10263941", "0.41", "0.0199", "1", "gnnexplainer", "kinship", "PH_2019", "PH", "2019"],
        ["S18_52620945", "18", "52620945", "0.44", "0.0197", "2", "gnnexplainer", "kinship", "PH_2019", "PH", "2019"],
    ]
    with (folder / "top50_kinshipgraph_gnnexplainer_PH_2019_lee.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(rows)
    monkeypatch.setenv("AGRIHUB_MODEL_RESULTS_DIR", str(root))
    return root


def _study(trait: str, **extra: Any) -> TraitStudy:
    return TraitStudy(mode="trait", species="soybean", assembly="Wm82.a2.v1", trait_text=trait, **extra)


def _run(study: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    run_id = uuid.uuid4().hex

    async def scenario() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        graph = build_study_graph(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": run_id, "run_id": run_id}}
        received = []
        try:
            async for part in graph.astream({"study": study}, config, stream_mode=["custom"], subgraphs=True, version="v2"):
                received.append(part["data"])
        except Exception as exc:  # noqa: BLE001
            received.append({"type": "error", "message": str(exc)})
        return received, dict((await graph.aget_state(config)).values)

    try:
        return asyncio.run(scenario())
    finally:
        close_bundles()


def test_the_registry_keeps_score_types_and_marks_planned_runners():
    registry = load_model_registry()
    by_id = {model.id: model for model in registry.models}
    assert by_id[PRECOMPUTED].score_type == "gnnexplainer" and by_id[PRECOMPUTED].assemblies == ["Lee.gnm2"]
    assert by_id["gwas_atlas_top_hits"].label == "previously published associations — not a new analysis"
    assert {model.id for model in registry.models if model.status == "planned"} == {"gapit_blink", "gapit_farmcpu", "gapit_mlm", "sister_team_gnn_live"}
    planned = adapter_for(by_id["gapit_blink"])
    assert isinstance(planned, PlannedAdapter)
    verdict = planned.applicable(_study("plant height"))
    assert not verdict.ok and "genotype_vcf, phenotype_table" in verdict.reasons[0]
    with pytest.raises(NotImplementedError):
        planned.run(_study("plant height"))


def test_precomputed_results_match_the_trait_code_or_name_and_read_lee_positions(results_dir: Path):
    adapter = adapter_for(load_model_registry().get(PRECOMPUTED))
    assert isinstance(adapter, PrecomputedResultsAdapter)
    by_name = adapter.applicable(_study("plant height"))
    assert by_name.ok and by_name.datasets == ["PH_2019"] and by_name.trait == "PH"
    assert adapter.applicable(_study("PH")).ok
    missing = adapter.applicable(_study("grain yield"))
    assert not missing.ok and "no GY result files" in missing.reasons[0]
    other = adapter.applicable(_study("seed oil content"))
    assert not other.ok and "it covers PH (plant height)" in other.reasons[0]
    on_a4 = adapter.applicable(_study("plant height").model_copy(update={"assembly": "Wm82.a4.v1"}))
    assert not on_a4.ok and "lifted to Wm82.a2.v1), not the study assembly Wm82.a4.v1" in on_a4.reasons[0]
    snps = adapter.run(_study("plant height"), "PH_2019")
    assert [(snp.marker, snp.chrom, snp.pos, snp.assembly, snp.score_type, snp.rank) for snp in snps] == [
        ("S18_10263941", "18", 10_263_941, "Lee.gnm2", "gnnexplainer", 1),
        ("S18_52620945", "18", 52_620_945, "Lee.gnm2", "gnnexplainer", 2),
    ]
    assert snps[0].as_input().model_dump(include={"score_type", "model_id", "p_value"}) == {
        "score_type": "gnnexplainer",
        "model_id": PRECOMPUTED,
        "p_value": None,
    }


def test_catalog_hits_are_labelled_published_and_keep_p_values(fixture_env: FixtureBundle, results_dir: Path):
    adapter = adapter_for(load_model_registry().get("gwas_atlas_top_hits"))
    assert isinstance(adapter, CatalogTopHitsAdapter)
    assert adapter.applicable(_study("plant height")).ok
    [hit] = [snp for snp in adapter.run(_study("plant height")) if snp.chrom == "Gm18"]
    assert (hit.pos, hit.score_type, hit.p_value) == (9_263_500, "p_value", 1e-10)
    assert not adapter.applicable(_study("nodule colour")).ok
    candidates = {item.model_id: item for item in evaluate_models(_study("nodule colour"))}
    assert not any(item.applicability.ok for item in candidates.values())


def test_a_trait_study_runs_the_precomputed_results_through_the_pipeline(fixture_env: FixtureBundle, results_dir: Path):
    received, values = _run(
        {
            "mode": "trait",
            "species": "soybean",
            "assembly": "Wm82.a2.v1",
            "trait_text": "plant height",
            "model_preferences": [f"{PRECOMPUTED}:PH_2019"],
        }
    )
    assert values["run_status"] == "completed", received[-1]
    decision = next(event["data"] for event in received if event["type"] == "orchestrator.decision" and event["data"]["kind"] == "select_model")
    assert [(item["model_id"], item["dataset"], item["score_type"]) for item in decision["selected"]] == [(PRECOMPUTED, "PH_2019", "gnnexplainer")]
    assert "gnnexplainer scores are kept" in decision["rationale"]
    assert {row["specialist"] for row in decision["rejected"]} >= {"gapit_blink", "sister_team_gnn_live"}
    model_events = [event for event in received if event.get("agent", {}).get("kind") == "model"]
    assert {event["type"] for event in model_events} >= {"agent.started", "agent.completed", "orchestrator.decision"}
    [snp] = values["snps"]
    assert (snp["chrom"], snp["pos"], snp["score_type"], snp["model_id"]) == ("Gm18", 9_263_941, "gnnexplainer", PRECOMPUTED)
    assert snp["lifted_from"]["assembly"] == "Lee.gnm2" and snp["lifted_from"]["pos"] == 10_263_941
    run = values["model_result"]["runs"][0]
    assert (run["proposed"], run["placed"], run["lifted_to"]) == (2, 1, "Wm82.a2.v1")
    assert values["model_result"]["score_types"] == ["gnnexplainer"]
    report = values["report"]
    assert report["mode"] == "trait" and report["loci"][0]["lead_snp"] == "S18_10263941"
    assert "## Model step" in report["markdown"] and "Scores of different types are not compared or combined." in report["markdown"]
    assert any(warning["code"] == "unlifted" for warning in report["warnings"])


def test_a_pick_that_places_no_snps_falls_back_to_the_next_applicable_model(fixture_env: FixtureBundle, results_dir: Path):
    unliftable = results_dir / "2_Sep" / "Lee" / "top50_kinshipgraph_gnnexplainer_PH_2020_lee.csv"
    unliftable.write_text(",".join(HEADER) + "\nS18_52620945,18,52620945,0.4,0.02,1,gnnexplainer,kinship,PH_2020,PH,2020\n", encoding="utf-8")
    received, values = _run(
        {"mode": "trait", "species": "soybean", "assembly": "Wm82.a2.v1", "trait_text": "plant height", "model_preferences": [f"{PRECOMPUTED}:PH_2020"]}
    )
    assert values["run_status"] == "completed", received[-1]
    assert [(run["model_id"], run["placed"]) for run in values["model_result"]["runs"]] == [(PRECOMPUTED, 0), ("gwas_atlas_top_hits", 1)]
    assert "placed no SNPs, so GWAS Atlas top hits for the trait was used next" in values["model_result"]["rationale"]
    assert values["model_result"]["score_types"] == ["gnnexplainer", "p_value"]


def test_without_an_applicable_model_the_model_phase_fails_with_reasons(fixture_env: FixtureBundle, results_dir: Path):
    received, values = _run({"mode": "trait", "species": "soybean", "assembly": "Wm82.a2.v1", "trait_text": "nodule colour"})
    failed = next(event["data"] for event in received if event.get("type") == "run.phase" and event["data"]["status"] == "failed")
    assert failed["phase"] == "model"
    assert failed["detail"].startswith("No applicable model is registered for nodule colour in soybean: ")
    decision = next(event["data"] for event in received if event.get("type") == "orchestrator.decision")
    assert decision["kind"] == "select_model" and decision["selected"] == []
    assert values.get("report") is None


@pytest.mark.bundle
@pytest.mark.skipif(not REAL_BUNDLE.exists() or not (REAL_RESULTS / "2_Sep" / "Lee").is_dir(), reason="needs the soybean bundle and the sister-team CSVs")
def test_the_real_ph_2019_results_lift_and_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("AGRIHUB_MODEL_RESULTS_DIR", str(REAL_RESULTS))
    received, values = _run(
        {"mode": "trait", "species": "soybean", "assembly": "Wm82.a2.v1", "trait_text": "plant height", "model_preferences": [f"{PRECOMPUTED}:PH_2019"]}
    )
    assert values["run_status"] == "completed", received[-1]
    run = values["model_result"]["runs"][0]
    assert run["dataset"] == "PH_2019" and run["proposed"] == 50 and run["placed"] >= 45
    assert all(snp["lifted_from"]["assembly"] == "Lee.gnm2" for snp in values["snps"])
    assert values["report"]["loci"] and values["report"]["candidates"]
