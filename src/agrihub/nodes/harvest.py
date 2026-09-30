"""Batch evidence harvest over every candidate gene. A stub source for now."""

from typing import Any

from langchain_core.runnables import RunnableConfig

from agrihub import events
from agrihub.configuration import run_id_from_config
from agrihub.evidence_store import EvidenceStore
from agrihub.nodes.locus_builder import STUB_SOURCE, STUB_VERSION
from agrihub.state import EvidenceItem, StudyState

# The nearest gene of each locus also gets these stub categories so every
# specialist has something to review.
NEAREST_GENE_CATEGORIES = (
    ("association", "stub_gwas_hit"),
    ("ortholog", "stub_arabidopsis_ortholog"),
    ("expression", "stub_tissue_expression"),
    ("literature", "stub_publication"),
)


async def harvest(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
    """Store evidence for every candidate and build the triage brief."""
    events.phase("harvest")
    study = state.get("study") or {}
    store = EvidenceStore.for_run(run_id_from_config(config))
    events.source_discovered(
        source_id=STUB_SOURCE,
        name="AgriHub stub source",
        version=STUB_VERSION,
        license="internal test data",
    )
    candidates = list(state.get("candidates") or [])
    by_locus: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_locus.setdefault(str(candidate["locus_id"]), []).append(candidate)
    for genes in by_locus.values():
        genes.sort(key=lambda gene: int(gene["distance_bp"]))

    done = 0
    for genes in by_locus.values():
        items = [_annotation(gene) for gene in genes]
        items.extend(_nearest_gene_items(genes[0]))
        store.put_items(items)
        done += len(genes)
        events.evidence_progress(
            store.counts_by_category(),
            done=done,
            total=len(candidates),
        )

    top_k = int(study.get("top_k_per_locus") or 5)
    top_genes = {
        locus_id: [str(gene["gene_id"]) for gene in genes[:top_k]]
        for locus_id, genes in by_locus.items()
    }
    counts = store.counts_by_category()
    triage_brief = {
        "loci": len(by_locus),
        "genes": len(candidates),
        "evidence_items": sum(counts.values()),
        "evidence_by_category": counts,
        "top_genes": top_genes,
        "coverage_gaps": ["network", "regulation", "variant", "known_gene"],
    }
    events.artifact_created(
        "candidates_table",
        title="Candidate genes",
        rows=[
            {"gene_id": gene_id, "locus_id": locus_id, "rank_in_locus": rank}
            for locus_id, gene_ids in top_genes.items()
            for rank, gene_id in enumerate(gene_ids, start=1)
        ],
    )
    events.phase(
        "harvest",
        "completed",
        detail=f"{triage_brief['evidence_items']} evidence items for {len(candidates)} genes",
    )
    return {"triage_brief": triage_brief}


def _annotation(gene: dict[str, Any]) -> EvidenceItem:
    return EvidenceItem(
        gene_id=str(gene["gene_id"]),
        category="functional_annotation",
        subtype="stub_defline",
        value="Stub gene model (no data bundle loaded)",
        source_db=STUB_SOURCE,
        db_version=STUB_VERSION,
        source_record=f"annotation:{gene['gene_id']}",
    )


def _nearest_gene_items(gene: dict[str, Any]) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            gene_id=str(gene["gene_id"]),
            category=category,
            subtype=subtype,
            value=f"{subtype} placeholder",
            source_db=STUB_SOURCE,
            db_version=STUB_VERSION,
            source_record=f"{subtype}:{gene['gene_id']}",
        )
        for category, subtype in NEAREST_GENE_CATEGORIES
    ]
