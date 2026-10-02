"""The orchestrator and specialist agents with scripted fake models on the fixture bundle.

Each scenario registers a script for ``agrihub-fake:<name>`` that plays the
orchestrator and/or the specialists through ordinary tool calls, then runs
the whole study graph in-process.
"""

import asyncio
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import pytest
from agrihub_fixtures import FixtureBundle
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agrihub import budget, fake_llm
from agrihub.evidence_store import EvidenceStore
from agrihub.graph import build_study_graph
from agrihub.nodes import orchestrator as orchestrator_node
from agrihub.nodes.specialist import SPECS
from agrihub.prompts import PromptContext, render
from agrihub.prompts.orchestrator import ORCHESTRATOR
from agrihub.state import SPECIALISTS

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
Script = Callable[[list[BaseMessage], tuple[str, ...]], AIMessage]


@pytest.fixture(autouse=True)
def bundle_env(fixture_env: FixtureBundle, fake_llm: str) -> FixtureBundle:
    return fixture_env


@pytest.fixture
def script() -> Iterator[Callable[[Script], str]]:
    names: list[str] = []

    def register(function: Script) -> str:
        name = f"test-{uuid.uuid4().hex[:8]}"
        fake_llm.register_script(name, function)
        names.append(name)
        return f"{fake_llm.PREFIX}{name}"

    yield register
    for name in names:
        fake_llm.unregister_script(name)


def _run(model: str | None = None, **configurable: Any) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    run_id = uuid.uuid4().hex
    received: list[dict[str, Any]] = []
    settings = dict(configurable)
    if model:
        settings.update(orchestrator_model=model, specialist_model=model)

    async def scenario() -> dict[str, Any]:
        graph = build_study_graph(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": run_id, "run_id": run_id, **settings}}
        async for part in graph.astream({"study": STUDY}, config, stream_mode=["custom"], subgraphs=True, version="v2"):
            received.append(part["data"])
        return dict((await graph.aget_state(config)).values)

    values = asyncio.run(scenario())
    return received, values, run_id


def _of(received: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in received if event["type"] == kind]


def _candidates(messages: list[BaseMessage]) -> list[str]:
    text = fake_llm.human_text(messages)
    return [gene for line in text.splitlines() if line.startswith("L") and ": " in line for gene in line.split(": ", 1)[1].split(" (")[0].split(", ")]


def _orchestrator_or_poster(orchestrate: Callable[[list[BaseMessage]], AIMessage]) -> Script:
    def play(messages: list[BaseMessage], tools: tuple[str, ...]) -> AIMessage:
        if "dispatch_specialists" in tools:
            return orchestrate(messages)
        return fake_llm.poster_script(messages, tools)

    return play


def test_orchestrator_dispatches_differentiated_lanes_with_rationales():
    received, values, run_id = _run(max_rounds=1)
    decision = _of(received, "orchestrator.decision")[0]["data"]
    dispatched = decision["dispatched"]
    assert decision["kind"] == "dispatch" and decision["round"] == 1
    assert len(dispatched) >= 3
    focus = {(tuple(sorted(item["focus_gene_ids"])), tuple(item["focus_loci"])) for item in dispatched}
    assert len(focus) == len(dispatched)
    assert len({item["instructions"] for item in dispatched}) == len({item["rationale"] for item in dispatched}) == len(dispatched)
    assert all(item["agent_id"].startswith("call_orch_r1_s2-") for item in dispatched)
    started = {
        event["agent"]["id"]: event
        for event in _of(received, "agent.started")
        if event["agent"]["kind"] == "specialist"
    }
    assert set(started) == {item["agent_id"] for item in dispatched}
    for item in dispatched:
        focus = started[item["agent_id"]]["data"]["focus"]
        assert focus["gene_ids"] == item["focus_gene_ids"] and focus["rationale"] == item["rationale"]
    plan = _of(received, "orchestrator.plan")[0]["data"]
    assert len(plan["steps"]) == len(dispatched) + 3
    usage = [event for event in _of(received, "agent.usage") if event["agent"]["kind"] == "orchestrator"]
    assert usage and usage[-1]["data"]["input_tokens"] > 0
    assert values["orchestrator_usage"][0]["calls"] == 2
    tools = [event["data"]["tool_call_id"] for event in _of(received, "tool.started")]
    assert len(tools) == len(set(tools)) and all(tool.startswith("call_orch_r1_s2-") for tool in tools)
    store = EvidenceStore.for_run(run_id)
    for finding in store.findings():
        assert store.resolve(finding.evidence_ids)[1] == []
    store.close()


def test_unknown_gene_ids_in_a_dispatch_are_rejected(script: Callable[[Script], str]):
    def orchestrate(messages: list[BaseMessage]) -> AIMessage:
        genes = _candidates(messages)
        return fake_llm.reply(
            fake_llm.call(
                "dispatch_specialists",
                {
                    "summary": "Check genetics and literature.",
                    "dispatches": [
                        {"specialist": "qtl_gwas", "focus_gene_ids": genes[:2], "focus_loci": ["L1"], "instructions": "Check QTLs.", "rationale": "Top genes."},
                        {"specialist": "literature", "focus_gene_ids": [genes[0], "Glyma.99G999999"], "instructions": "Search.", "rationale": "Named genes."},
                        {"specialist": "locus_variant", "focus_gene_ids": [], "focus_loci": ["L9"], "instructions": "Validate.", "rationale": "Loci."},
                    ],
                },
                "call_bad",
            )
        )

    received, values, _ = _run(script(_orchestrator_or_poster(orchestrate)), max_rounds=1)
    decision = _of(received, "orchestrator.decision")[0]["data"]
    assert [item["specialist"] for item in decision["dispatched"]] == ["qtl_gwas"]
    rejected = {item["specialist"]: item["reason"] for item in decision["rejected"]}
    assert rejected["literature"] == "unknown gene ids: Glyma.99G999999"
    assert rejected["locus_variant"] == "unknown loci: L9"
    assert [event["agent"]["id"] for event in _of(received, "agent.started") if event["agent"]["kind"] == "specialist"] == ["call_bad-qtl_gwas"]
    assert values["run_status"] == "completed"


def test_a_dispatch_with_only_invalid_entries_lets_the_orchestrator_retry(script: Callable[[Script], str]):
    def orchestrate(messages: list[BaseMessage]) -> AIMessage:
        step = fake_llm.steps_taken(messages)
        genes = ["Glyma.99G999999"] if step == 0 else _candidates(messages)[:1]
        entry = {"specialist": "function_orthology", "focus_gene_ids": genes, "instructions": "Check.", "rationale": "Why."}
        return fake_llm.reply(fake_llm.call("dispatch_specialists", {"summary": "Try.", "dispatches": [entry]}, f"call_try{step}"))

    received, _, _ = _run(script(_orchestrator_or_poster(orchestrate)), max_rounds=1)
    decision = _of(received, "orchestrator.decision")[0]["data"]
    assert [item["agent_id"] for item in decision["dispatched"]] == ["call_try1-function_orthology"]


def test_a_specialist_that_never_finishes_closes_when_its_budget_runs_out(script: Callable[[Script], str]):
    def play(messages: list[BaseMessage], tools: tuple[str, ...]) -> AIMessage:
        if "dispatch_specialists" in tools:
            genes = _candidates(messages)
            entry = {"specialist": "function_orthology", "focus_gene_ids": genes[:1], "instructions": "Check.", "rationale": "Top gene."}
            return fake_llm.reply(fake_llm.call("dispatch_specialists", {"summary": "One lane.", "dispatches": [entry]}, "call_one"))
        step = fake_llm.steps_taken(messages)
        return fake_llm.reply(fake_llm.call("think", {"reflection": "keep looking"}, f"think-{step}"))

    received, values, _ = _run(script(play), max_rounds=1, max_specialist_steps=2)
    steps = [event["data"]["step"] for event in _of(received, "agent.step") if event["agent"]["kind"] == "specialist"]
    completed = [event for event in _of(received, "agent.completed") if event["agent"]["kind"] == "specialist"]
    assert steps == [1, 2]
    assert completed[0]["data"]["status"] == "completed"
    assert budget.budget_reminder(0, 2) and budget.budget_reminder(1, 2).startswith("This is your last tool round")
    assert budget.budget_reminder(0, 8) is None
    assert completed[0]["data"]["summary"].startswith("Step budget of 2 used up")
    assert values["specialist_results"][0]["steps"] == 2
    assert values["run_status"] == "completed" and values["report"]


def test_one_failing_specialist_does_not_fail_the_run(script: Callable[[Script], str]):
    def play(messages: list[BaseMessage], tools: tuple[str, ...]) -> AIMessage:
        if "specialist_done" in tools and fake_llm.assignment(messages).get("specialist") == "qtl_gwas":
            raise RuntimeError("provider exploded")
        return fake_llm.poster_script(messages, tools)

    received, values, _ = _run(script(play), max_rounds=1)
    completed = {event["agent"]["id"]: event["data"] for event in _of(received, "agent.completed")}
    failed = [data for data in completed.values() if data["status"] == "failed"]
    assert len(failed) == 1 and "RuntimeError: provider exploded" in failed[0]["summary"]
    assert len(completed) >= 3 and sum(1 for data in completed.values() if data["status"] == "completed") == len(completed) - 1
    results = {result["specialist"]: result for result in values["specialist_results"]}
    assert results["qtl_gwas"]["status"] == "failed"
    assert values["run_status"] == "completed"
    assert _of(received, "run.phase")[-1]["data"] == {"phase": "reporting", "status": "completed"}


def test_a_rejected_tool_call_is_fed_back_once_instead_of_failing(script: Callable[[Script], str]):
    class Rejected(Exception):
        status_code = 400

    seen: list[int] = []

    def play(messages: list[BaseMessage], tools: tuple[str, ...]) -> AIMessage:
        if "dispatch_specialists" in tools and not any("rejected by the model provider" in str(message.content) for message in messages):
            seen.append(1)
            raise Rejected("tool call validation failed: parameters did not match schema")
        return fake_llm.poster_script(messages, tools)

    received, _, _ = _run(script(play), max_rounds=1)
    decision = _of(received, "orchestrator.decision")[0]["data"]
    assert seen == [1]
    assert not decision["rationale"].startswith("Fallback plan") and len(decision["dispatched"]) >= 3


def test_a_failing_orchestrator_model_falls_back_to_the_default_plan(script: Callable[[Script], str]):
    def play(messages: list[BaseMessage], tools: tuple[str, ...]) -> AIMessage:
        if "dispatch_specialists" in tools:
            raise RuntimeError("rate limited")
        return fake_llm.poster_script(messages, tools)

    received, values, _ = _run(script(play), max_rounds=1)
    decision = _of(received, "orchestrator.decision")[0]["data"]
    assert decision["rationale"].startswith("Fallback plan (orchestrator model failed: RuntimeError: rate limited)")
    assert len(decision["dispatched"]) >= 3
    assert values["run_status"] == "completed"


def test_follow_up_rounds_happen_only_when_allowed(script: Callable[[Script], str]):
    def orchestrate(messages: list[BaseMessage]) -> AIMessage:
        text = fake_llm.human_text(messages)
        if "Delta brief (JSON):" in text:
            delta = fake_llm.json_after(text, "Delta brief (JSON):") or {}
            genes = [item["gene_id"] for item in delta.get("gained_support") or []] or _candidates(messages)[:1]
            entry = {"specialist": "function_orthology", "focus_gene_ids": genes[:1], "instructions": "Narrow check.", "rationale": "Gained support."}
            return fake_llm.reply(fake_llm.call("dispatch_specialists", {"summary": "Follow up.", "dispatches": [entry]}, "call_follow"))
        genes = _candidates(messages)
        entry = {"specialist": "qtl_gwas", "focus_gene_ids": genes[:3], "focus_loci": ["L1", "L2"], "instructions": "Check.", "rationale": "Top genes."}
        return fake_llm.reply(fake_llm.call("dispatch_specialists", {"summary": "Genetics first.", "dispatches": [entry]}, "call_first"))

    model = script(_orchestrator_or_poster(orchestrate))
    single, single_values, _ = _run(model, max_rounds=1)
    assert [event["data"]["kind"] for event in _of(single, "orchestrator.decision")] == ["dispatch", "finish"]
    assert len(single_values["orchestrator_usage"]) == 1
    assert "delta_brief" in single_values and single_values["round"] == 1

    double, double_values, _ = _run(model, max_rounds=2)
    decisions = [event["data"] for event in _of(double, "orchestrator.decision")]
    assert [(item["kind"], item["round"]) for item in decisions] == [("dispatch", 1), ("followup", 2), ("finish", 2)]
    assert [item["agent_id"] for item in decisions[1]["dispatched"]] == ["call_follow-function_orthology"]
    started = [event for event in _of(double, "agent.started") if event["agent"]["kind"] == "specialist"]
    assert [event["data"]["focus"]["round"] for event in started] == [1, 2]
    assert started[1]["agent"]["label"].endswith("round 2")
    assert double_values["round"] == 2 and len(double_values["orchestrator_usage"]) == 2
    phases = [(event["data"]["phase"], event["data"]["status"]) for event in _of(double, "run.phase")]
    assert phases.count(("specialists", "started")) == 1 and phases.count(("specialists", "completed")) == 1


def test_the_orchestrator_may_finish_without_a_follow_up(script: Callable[[Script], str]):
    def orchestrate(messages: list[BaseMessage]) -> AIMessage:
        if "Delta brief (JSON):" in fake_llm.human_text(messages):
            return fake_llm.reply(fake_llm.call("finish_research", {"reason": "Round 1 covered every locus."}, "call_end"))
        return orchestrator_or_default(messages)

    def orchestrator_or_default(messages: list[BaseMessage]) -> AIMessage:
        return fake_llm.poster_script(messages, ("dispatch_specialists",))

    received, values, _ = _run(script(_orchestrator_or_poster(orchestrate)), max_rounds=2)
    decisions = [event["data"] for event in _of(received, "orchestrator.decision")]
    assert [(item["kind"], item["round"]) for item in decisions] == [("dispatch", 1), ("finish", 1)]
    assert decisions[-1]["rationale"] == "Round 1 covered every locus."
    assert ("specialists", "completed") in [(event["data"]["phase"], event["data"]["status"]) for event in _of(received, "run.phase")]
    assert values["run_status"] == "completed"


def test_validate_trims_focus_lists_and_reports_disabled_specialists():
    view = orchestrator_node.View(
        study={},
        brief={},
        candidates={f"Glyma.01G{index:06d}": {"gene_id": f"Glyma.01G{index:06d}", "locus_id": "L1"} for index in range(20)},
        loci={"L1": {}},
        enabled=["literature", "qtl_gwas"],
        round=1,
        max_rounds=2,
        max_genes=5,
        delta=None,
        previous=[],
        matrix=[],
        profile=None,
    )
    genes = [f"glyma.01g{index:06d}" for index in range(8)]
    accepted, rejected, notes = orchestrator_node.validate(
        view,
        [
            {"specialist": "literature", "focus_gene_ids": genes, "instructions": "i", "rationale": "r"},
            {"specialist": "literature", "focus_gene_ids": genes[:1], "instructions": "i", "rationale": "r"},
            {"specialist": "locus_variant", "focus_loci": ["L1"], "instructions": "i", "rationale": "r"},
            {"specialist": "qtl_gwas", "instructions": "i", "rationale": "r"},
        ],
    )
    assert accepted[0]["focus_gene_ids"] == [f"Glyma.01G{index:06d}" for index in range(5)]
    assert notes == ["literature: kept the first 5 of 8 genes"]
    assert rejected == [
        {"specialist": "literature", "reason": "already dispatched in this call"},
        {"specialist": "locus_variant", "reason": "disabled for this study"},
        {"specialist": "qtl_gwas", "reason": "no focus genes or loci"},
    ]


def test_history_compaction_keeps_recent_results_and_valid_tool_pairs():
    messages: list[BaseMessage] = [HumanMessage(content="assignment")]
    for index in range(4):
        call_id = f"c{index}"
        messages.append(AIMessage(content="", tool_calls=[{"name": "gene_annotation", "args": {}, "id": call_id, "type": "tool_call"}]))
        messages.append(
            ToolMessage(
                content=f"gene_annotation {index} of 4\n"
                + "\n".join(f"E{index * 3 + 1}..E{index * 3 + 3} Glyma.01G00000{index} row {row}" for row in range(10))
                + f"\ntruncated: false; output_ref: O{index + 1}",
                tool_call_id=call_id,
                name="gene_annotation",
                artifact={"aliases": [f"E{index * 3 + n}" for n in (1, 2, 3)], "output_ref": f"O{index + 1}"},
            )
        )
    compacted = budget.compact_history(messages, keep_tool_results=2)
    tool_messages = [message for message in compacted if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in tool_messages] == ["c0", "c1", "c2", "c3"]
    assert tool_messages[0].content == f"{budget.DIGEST_MARK} | gene_annotation 0 of 4 | evidence E1..E3 | output_ref O1"
    assert tool_messages[3].content == messages[-1].content
    assert budget.compact_history(compacted, 2) == compacted
    assert budget.estimate_tokens(compacted) < budget.estimate_tokens(messages)
    clipped = budget.clip_tool_output("x" * 1_000 + "\noutput_ref: O7", 500)
    assert len(clipped) <= 500 and clipped.endswith("Full result: output_ref O7.]")


def test_prompts_carry_guardrails_study_values_tools_and_gaps():
    context = PromptContext(
        species="soybean",
        assembly="Wm82.a2.v1",
        trait="plant height",
        max_steps=6,
        profile_key="plant_height",
        profile_keywords=("dwarf", "internode length"),
        focus_gene_ids=("Glyma.18G092200",),
        focus_loci=("L2",),
    )
    for name, spec in SPECS.items():
        text = render(spec.prompt, context, tools=tuple(tool.name for tool in spec.tools))
        assert 'trait "plant height"' in text and "soybean (Wm82.a2.v1)" in text
        assert "at most 6 tool rounds" in text and "Glyma.18G092200" in text and "dwarf, internode length" in text
        assert "Every claim cites evidence" in text and "Absence of evidence is not negative evidence" in text
        assert set(spec.prompt.tools) == {tool.name for tool in spec.tools}, name
        assert "{" not in text and "}" not in text
        assert "## Unavailable domains in this build" not in text
        gaps = replace(context, unavailable=("co-expression neighbours (ATTED-II): no edges rows",))
        assert "- co-expression neighbours (ATTED-II): no edges rows" in render(spec.prompt, gaps)
    orchestrator = render(
        ORCHESTRATOR,
        PromptContext(
            species="soybean",
            assembly="Wm82.a2.v1",
            trait="plant height",
            max_steps=6,
            extra={"max_focus_genes": "12", "round": "1", "max_rounds": "2", "round_rule": "One follow-up allowed."},
        ),
    )
    assert "at most 12 genes per specialist" in orchestrator and "round 1 of at most 2" in orchestrator
    assert set(SPECS) == set(SPECIALISTS)


def test_the_poster_script_is_deterministic_for_the_same_conversation():
    payload = {"agent_id": "lane", "specialist": "qtl_gwas", "focus_gene_ids": ["Glyma.18G092200"], "focus_loci": [], "trait": "plant height", "species": "soybean"}
    messages: list[BaseMessage] = [HumanMessage(content="Assignment JSON: " + json.dumps(payload))]
    tools = tuple(tool.name for tool in SPECS["qtl_gwas"].tools)
    first = fake_llm.scripted_model("poster").bind_tools(SPECS["qtl_gwas"].tools).invoke(messages)
    second = fake_llm.poster_script(messages, tools)
    assert [call["name"] for call in first.tool_calls] == ["qtl_overlap", "gwas_catalog_overlap"]
    assert first.tool_calls == second.tool_calls
    assert first.usage_metadata and first.usage_metadata["input_tokens"] > 0
    slow = fake_llm.scripted_model("poster@0.05")
    assert (slow.script_name, slow.delay) == ("poster", 0.05)
