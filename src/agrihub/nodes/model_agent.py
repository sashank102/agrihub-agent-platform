"""Model step for trait studies. A stub until model adapters exist."""

from typing import Any

from langchain_core.runnables import RunnableConfig

from agrihub import events
from agrihub.state import StudyState

STUB_SNPS = (
    {"raw": "stub_model_snp_1", "chrom": "1", "pos": 1_200_000, "score": 0.9, "method": "stub"},
    {"raw": "stub_model_snp_2", "chrom": "2", "pos": 3_400_000, "score": 0.7, "method": "stub"},
)


async def model_agent(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
    """Return placeholder ranked SNPs so the rest of the pipeline can run."""
    events.phase("model")
    study = state.get("study") or {}
    snps = [dict(snp) for snp in STUB_SNPS]
    result = {
        "adapter": "stub",
        "applicable": True,
        "trait": study.get("trait_text"),
        "snp_count": len(snps),
        "note": "Placeholder SNPs: no model or precomputed results are registered yet.",
    }
    events.phase("model", "completed", detail=result["note"])
    return {"snps": snps, "model_result": result}
