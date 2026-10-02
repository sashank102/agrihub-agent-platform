"""Join the specialist lanes, build the delta brief, and route to a follow-up decision or to ranking.

The delta brief lists, for the round just finished, the genes that gained
supporting findings, genes with conflicting findings, dispatched genes that
got no finding, failed specialists and the genes literature already covered.
With rounds left (``round < max_rounds``) the orchestrator reads it and
decides on a follow-up; otherwise the run finishes here.
"""

import asyncio
from collections import defaultdict
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from agrihub import events
from agrihub.configuration import StudyConfiguration
from agrihub.state import Finding, StudyState
from agrihub.tools.store_tools import store_for_config

SUMMARY_CHARS = 200


async def collect(state: StudyState, config: RunnableConfig) -> Command:
    """Merge this round's findings and decide where the run goes next."""
    settings = StudyConfiguration.from_runnable_config(config)
    round_number = int(state.get("round") or 0)
    results = [result for result in state.get("specialist_results") or [] if result.get("round") == round_number]
    findings = await asyncio.to_thread(_round_findings, config, [str(result["agent_id"]) for result in results])
    delta = delta_brief(round_number, results, findings, list(state.get("specialist_results") or []))
    if round_number < settings.max_rounds:
        return Command(update={"delta_brief": delta}, goto="orchestrator")
    total = state.get("specialist_results") or []
    events.phase(
        "specialists",
        "completed",
        detail=f"{len(total)} specialist runs returned {sum(len(result.get('finding_ids') or []) for result in total)} findings",
    )
    events.decision(
        "finish",
        f"{len(findings)} findings from {len(results)} specialists in round {round_number}; "
        f"the study allows {settings.max_rounds} round{'s' if settings.max_rounds != 1 else ''}, so research ends here.",
        round=round_number,
    )
    return Command(update={"delta_brief": delta, "run_status": "ranking"}, goto="rank_verify")


def delta_brief(
    round_number: int,
    results: list[dict[str, Any]],
    findings: list[Finding],
    all_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize what one round changed, compactly enough for the orchestrator's next decision."""
    names = {str(result["agent_id"]): str(result["specialist"]) for result in all_results}
    support: dict[str, list[Finding]] = defaultdict(list)
    conflict: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        if finding.stance == "supports":
            support[finding.target].append(finding)
        elif finding.stance == "conflicts":
            conflict[finding.target].append(finding)
    dispatched = list(dict.fromkeys(gene for result in results for gene in result.get("focus_gene_ids") or []))
    with_findings = {finding.target for finding in findings}
    covered = sorted(
        {
            gene
            for result in all_results
            if result.get("specialist") == "literature" and result.get("status") == "completed"
            for gene in result.get("focus_gene_ids") or []
        }
    )
    return {
        "round": round_number,
        "specialists": [
            {
                "agent_id": result["agent_id"],
                "specialist": result["specialist"],
                "status": result.get("status"),
                "findings": len(result.get("finding_ids") or []),
                "summary": str(result.get("summary") or "")[:SUMMARY_CHARS],
            }
            for result in results
        ],
        "gained_support": [
            {
                "gene_id": gene,
                "findings": [str(item.finding_id) for item in items],
                "by": sorted({names.get(item.agent_id, item.agent_id) for item in items}),
                "strongest": max((item.strength for item in items), key=["weak", "moderate", "strong"].index),
            }
            for gene, items in support.items()
        ],
        "conflicts": [
            {
                "gene_id": gene,
                "conflicting": [str(item.finding_id) for item in items],
                "supporting": [str(item.finding_id) for item in support.get(gene, [])],
            }
            for gene, items in conflict.items()
        ],
        "no_findings": [gene for gene in dispatched if gene not in with_findings],
        "failed": [result["specialist"] for result in results if result.get("status") == "failed"],
        "literature_covered": covered,
    }


def _round_findings(config: RunnableConfig, agent_ids: list[str]) -> list[Finding]:
    if not agent_ids:
        return []
    store = store_for_config(config)
    wanted = set(agent_ids)
    return [finding for finding in store.findings() if finding.agent_id in wanted]
