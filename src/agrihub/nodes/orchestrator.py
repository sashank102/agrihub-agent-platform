"""Plan the research round and dispatch specialists with ``Send``.

The dispatch id is the specialist's ``agent_id`` and the UI lane key. Once the
orchestrator is an LLM, it is the ``dispatch_specialists`` tool-call id.
"""

import uuid

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, Send

from agrihub import events
from agrihub.state import SPECIALISTS, SpecialistTask, StudyState

SPECIALIST_LABELS = {
    "locus_variant": "Locus and variant specialist",
    "qtl_gwas": "QTL and GWAS specialist",
    "function_orthology": "Function and orthology specialist",
    "expression_network": "Expression and network specialist",
    "literature": "Literature specialist",
}
SPECIALIST_INSTRUCTIONS = {
    "locus_variant": "Validate each locus and check linked variants in the focus genes.",
    "qtl_gwas": "Check overlapping QTLs, prior GWAS hits, and known trait genes.",
    "function_orthology": "Assess domains, GO relevance, and characterized orthologs.",
    "expression_network": "Check expression in trait tissues and network proximity.",
    "literature": "Find publications that link the focus genes to the trait.",
}


async def orchestrator(state: StudyState, config: RunnableConfig) -> Command:
    """Emit the plan and one dispatch per enabled specialist."""
    events.phase("planning")
    study = state.get("study") or {}
    brief = state.get("triage_brief") or {}
    top_genes: dict[str, list[str]] = brief.get("top_genes") or {}
    focus_gene_ids = [gene_id for genes in top_genes.values() for gene_id in genes]
    focus_loci = list(top_genes)
    requested = study.get("specialists_enabled") or SPECIALISTS
    enabled = [name for name in SPECIALISTS if name in requested]
    round_number = int(state.get("round") or 0) + 1

    events.plan(
        f"Review {len(focus_gene_ids)} triaged genes across {len(focus_loci)} loci.",
        [f"{SPECIALIST_LABELS[name]}: {SPECIALIST_INSTRUCTIONS[name]}" for name in enabled]
        + ["Collect findings", "Rank and verify candidates", "Write the report"],
    )
    tasks: list[SpecialistTask] = [
        {
            "agent_id": f"call_{uuid.uuid4().hex[:24]}",
            "specialist": name,
            "round": round_number,
            "focus_gene_ids": focus_gene_ids,
            "focus_loci": focus_loci,
            "instructions": SPECIALIST_INSTRUCTIONS[name],
            "rationale": f"Default roster: {SPECIALIST_LABELS[name]} covers the top genes.",
            "study": {
                "species": study.get("species"),
                "assembly": study.get("assembly"),
                "trait_text": study.get("trait_text"),
            },
        }
        for name in enabled
    ]
    dispatches = [
        {
            "agent_id": task["agent_id"],
            "specialist": task["specialist"],
            "round": round_number,
            "focus_gene_ids": task["focus_gene_ids"],
            "focus_loci": task["focus_loci"],
            "rationale": task["rationale"],
        }
        for task in tasks
    ]
    events.decision(
        "dispatch",
        f"Dispatch {len(tasks)} specialists on the top genes of every locus.",
        dispatched=[
            {
                key: dispatch[key]
                for key in ("agent_id", "specialist", "focus_gene_ids", "focus_loci")
            }
            for dispatch in dispatches
        ],
        rejected=[
            {"specialist": name, "reason": "disabled for this study"}
            for name in SPECIALISTS
            if name not in enabled
        ],
    )
    events.phase("planning", "completed", detail=f"{len(tasks)} specialists dispatched")
    events.phase("specialists")
    return Command(
        update={"dispatches": dispatches, "round": round_number},
        goto=[Send("specialist", dict(task)) for task in tasks],
    )
