"""The agrihub_study graph walks every phase on the fixture bundle and fans out five lanes."""

import asyncio
import uuid
from typing import Any

import pytest
from agrihub_fixtures import FixtureBundle
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from agrihub import events
from agrihub.evidence_store import EvidenceStore
from agrihub.graph import build_study_graph
from agrihub.nodes.intake import StudyInputError
from agrihub.state import SPECIALISTS, Report

SNP_STUDY = {
    "mode": "snps",
    "species": "soybean",
    "assembly": "Wm82.a2.v1",
    "trait_text": "plant height",
    "snps": [
        {"raw": "S5_2899164", "chrom": "5", "pos": 2_899_164},
        {"raw": "S18_9263941", "chrom": "18", "pos": 9_263_941},
        {"raw": "S18_9263941", "chrom": "18", "pos": 9_263_941},
        {"raw": "S18_9300000", "chrom": "18", "pos": 9_300_000},
        {"raw": "BARC_123", "marker_id": "BARC_123"},
    ],
}
TRAIT_STUDY = {
    "mode": "trait",
    "species": "soybean",
    "assembly": "Wm82.a2.v1",
    "trait_text": "plant height",
}
SNP_PHASES = ["intake", "loci", "harvest", "planning", "specialists", "ranking", "reporting"]


@pytest.fixture(autouse=True)
def bundle_env(fixture_env: FixtureBundle) -> FixtureBundle:
    return fixture_env


def _run(
    study: dict[str, Any],
    sink: Any = None,
    received: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    run_id = uuid.uuid4().hex
    events_seen: list[dict[str, Any]] = [] if received is None else received

    async def scenario() -> dict[str, Any]:
        graph = build_study_graph(checkpointer=InMemorySaver(), artifact_sink=sink)
        config = {"configurable": {"thread_id": "thread-1", "run_id": run_id}}
        async for part in graph.astream(
            {"study": study},
            config,
            stream_mode=["values", "custom"],
            subgraphs=True,
            version="v2",
        ):
            if part["type"] == "custom":
                assert part["data"]["schema"] == events.SCHEMA
                events_seen.append(part["data"])
        state = await graph.aget_state(config)
        return dict(state.values)

    values = asyncio.run(scenario())
    return events_seen, values, run_id


def _started_phases(received: list[dict[str, Any]]) -> list[str]:
    return [
        event["data"]["phase"]
        for event in received
        if event["type"] == "run.phase" and event["data"]["status"] == "started"
    ]


def test_snp_study_emits_phases_in_order_and_closes_each():
    received, values, _ = _run(SNP_STUDY)
    assert _started_phases(received) == SNP_PHASES
    phase_events = [
        (event["data"]["phase"], event["data"]["status"])
        for event in received
        if event["type"] == "run.phase"
    ]
    assert phase_events == [
        (name, status) for name in SNP_PHASES for status in ("started", "completed")
    ]
    assert [snp["raw"] for snp in values["snps"]] == ["S5_2899164", "S18_9263941", "S18_9300000"]
    assert {(warning["code"], warning.get("snp")) for warning in values["warnings"]} == {
        ("duplicate", "S18_9263941"),
        ("unresolved_marker", "BARC_123"),
    }
    intake_done = next(
        event["data"]
        for event in received
        if event["type"] == "run.phase" and (event["data"]["phase"], event["data"]["status"]) == ("intake", "completed")
    )
    assert len(intake_done["warnings"]) == 2
    assert [locus["locus_id"] for locus in values["loci"]] == ["L1", "L2"]
    merged = values["loci"][1]
    assert merged["chrom"] == "Gm18" and merged["merged_from"] == ["S18_9263941", "S18_9300000"]
    assert {candidate["gene_id"] for candidate in values["candidates"]} >= {"Glyma.05G032200", "Glyma.18G092200"}


def test_five_specialists_get_distinct_lanes_that_all_complete():
    received, values, run_id = _run(SNP_STUDY)
    started = [event for event in received if event["type"] == "agent.started"]
    completed = [event for event in received if event["type"] == "agent.completed"]
    started_ids = [event["agent"]["id"] for event in started]

    assert len(started) == 5
    assert len(set(started_ids)) == 5
    assert sorted(event["agent"]["id"] for event in completed) == sorted(started_ids)
    for event in started:
        assert event["agent"]["kind"] == "specialist"
        assert event["agent"]["parent_id"] == events.ORCHESTRATOR.id
        assert event["cause"] == {"type": "send", "tool_call_id": event["agent"]["id"]}
        assert event["ns"][0].startswith("specialist:")

    dispatch = next(
        event
        for event in received
        if event["type"] == "orchestrator.decision" and event["data"]["kind"] == "dispatch"
    )
    assert [item["agent_id"] for item in dispatch["data"]["dispatched"]] == [
        item["agent_id"] for item in values["dispatches"]
    ]
    assert sorted(item["specialist"] for item in values["dispatches"]) == sorted(SPECIALISTS)
    assert set(started_ids) == {item["agent_id"] for item in values["dispatches"]}

    lane_of_ns = {event["ns"][0]: event["agent"]["id"] for event in started}
    for event in received:
        if event["type"].startswith(("agent.", "tool.")):
            assert lane_of_ns[event["ns"][0]] == event["agent"]["id"]

    store = EvidenceStore.for_run(run_id)
    lane = {item["specialist"]: item["agent_id"] for item in values["dispatches"]}
    recorded = {finding.agent_id for finding in store.findings()}
    assert {lane["locus_variant"], lane["function_orthology"]} <= recorded <= set(started_ids)
    assert lane["expression_network"] not in recorded
    assert sorted(values["findings"]) == sorted(
        str(finding.finding_id) for finding in store.findings()
    )
    store.close()


def test_trait_study_routes_through_the_model_agent():
    received, values, _ = _run(TRAIT_STUDY)
    assert _started_phases(received) == ["intake", "model", *SNP_PHASES[1:]]
    assert values["model_result"]["adapter"] == "stub"
    assert len(values["snps"]) == values["model_result"]["snp_count"]
    assert values["report"]["mode"] == "trait"


def test_final_state_has_a_report_and_artifacts_follow_the_ledger():
    stored: list[dict[str, Any]] = []

    async def sink(config: dict[str, Any], **artifact: Any) -> str:
        assert config["configurable"]["run_id"]
        stored.append(artifact)
        return f"artifact-{len(stored)}"

    received, values, run_id = _run(SNP_STUDY, sink=sink)
    report = Report.model_validate(values["report"])
    assert report.candidates and report.loci
    assert report.finding_count == len(values["findings"])
    assert isinstance(values["messages"][-1], AIMessage)
    assert values["messages"][-1].content == report.markdown
    assert values["run_status"] == "completed"

    assert [item["kind"] for item in stored] == ["report", "evidence_snapshot"]
    snapshot = stored[1]["content"]
    assert len(snapshot["evidence"]) == report.evidence_count
    cited = {evidence_id for candidate in report.candidates for evidence_id in candidate.evidence_ids}
    assert cited <= {item["evidence_id"] for item in snapshot["evidence"]}

    artifacts = [event for event in received if event["type"] == "artifact.created"]
    assert [event["data"]["kind"] for event in artifacts] == [
        "loci_table",
        "candidates_table",
        "candidates_table",
        "evidence_snapshot",
        "report",
    ]
    assert artifacts[-1]["data"]["artifact_id"] == "artifact-1"
    assert received[-1]["type"] == "run.phase" and received[-1]["data"]["phase"] == "reporting"


def test_disabled_specialists_are_rejected_in_the_decision():
    study = {**SNP_STUDY, "specialists_enabled": ["literature", "qtl_gwas"]}
    received, values, _ = _run(study)
    assert sorted(item["specialist"] for item in values["dispatches"]) == ["literature", "qtl_gwas"]
    assert len([event for event in received if event["type"] == "agent.started"]) == 2
    dispatch = next(event for event in received if event["type"] == "orchestrator.decision")
    assert sorted(item["specialist"] for item in dispatch["data"]["rejected"]) == [
        "expression_network",
        "function_orthology",
        "locus_variant",
    ]


def test_invalid_study_is_rejected_at_intake():
    with pytest.raises(ValidationError):
        _run({**SNP_STUDY, "snps": []})
    with pytest.raises(ValidationError):
        _run({**SNP_STUDY, "mode": "genome"})


def test_a_study_with_no_placeable_snp_fails_intake_with_a_readable_event():
    received: list[dict[str, Any]] = []
    study = {
        **SNP_STUDY,
        "snps": [
            {"raw": "S99_1", "chrom": "99", "pos": 1},
            {"raw": "S18_99999999", "chrom": "Gm18", "pos": 99_999_999},
        ],
    }
    with pytest.raises(StudyInputError, match="none of the 2 SNPs could be placed"):
        _run(study, received=received)
    failed = [event["data"] for event in received if event["type"] == "run.phase" and event["data"]["status"] == "failed"]
    assert [event["phase"] for event in failed] == ["intake"]
    assert {warning["code"] for warning in failed[0]["warnings"]} == {"unknown_chromosome", "out_of_bounds"}
    assert "beyond the end of Gm18" in failed[0]["detail"]
    with pytest.raises(StudyInputError, match="not a registered soybean assembly"):
        _run({**SNP_STUDY, "assembly": "Wm82.a9"})
