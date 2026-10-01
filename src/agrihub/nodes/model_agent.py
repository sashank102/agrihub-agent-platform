"""Model step for trait studies. A stub until model adapters exist."""

import asyncio
from typing import Any

from langchain_core.runnables import RunnableConfig

from agrihub import events
from agrihub.nodes.intake import place_snps
from agrihub.state import SnpInput, StudyState, parse_study

STUB_SNPS = (
    {"raw": "stub_model_snp_1", "chrom": "1", "pos": 1_200_000, "score": 0.9, "method": "stub"},
    {"raw": "stub_model_snp_2", "chrom": "2", "pos": 3_400_000, "score": 0.7, "method": "stub"},
)


async def model_agent(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
    """Return placeholder ranked SNPs, placed like user SNPs, so the rest of the pipeline can run."""
    events.phase("model")
    study = parse_study(state.get("study") or {})
    placed, warnings = await asyncio.to_thread(
        place_snps, study, [SnpInput.model_validate(snp) for snp in STUB_SNPS]
    )
    note = "Placeholder SNPs: no model or precomputed results are registered yet."
    result = {
        "adapter": "stub",
        "applicable": True,
        "trait": study.trait_text,
        "snp_count": len(placed),
        "note": note,
    }
    events.phase("model", "completed", detail=note)
    return {
        "snps": [snp.model_dump(mode="json") for snp in placed],
        "model_result": result,
        "warnings": [*(state.get("warnings") or []), *(warning.model_dump(exclude_none=True) for warning in warnings)],
    }
