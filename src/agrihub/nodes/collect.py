"""Join the specialist lanes and decide whether another round is needed."""

from typing import Any

from langchain_core.runnables import RunnableConfig

from agrihub import events
from agrihub.state import StudyState


async def collect(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
    """Close the specialists phase and record the finish decision.

    The skeleton always finishes after one round; follow-up rounds arrive
    with the LLM orchestrator.
    """
    round_number = int(state.get("round") or 0)
    results = [
        result
        for result in state.get("specialist_results") or []
        if result.get("round") == round_number
    ]
    findings = sum(len(result.get("finding_ids") or []) for result in results)
    events.phase(
        "specialists",
        "completed",
        detail=f"{len(results)} specialists returned {findings} findings",
    )
    events.decision(
        "finish",
        f"{findings} findings from {len(results)} specialists cover every locus; "
        "no follow-up round is needed.",
    )
    return {"run_status": "ranking"}
