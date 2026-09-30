"""Turn SNPs into merged loci and candidate genes.

Windows and merging are real; the genes inside each window are stubs until
the species data bundles exist. Full gene records go to the evidence store as
positional evidence; state keeps ``{gene_id, locus_id, distance_bp}`` refs.
"""

from typing import Any

from langchain_core.runnables import RunnableConfig

from agrihub import events
from agrihub.configuration import run_id_from_config
from agrihub.evidence_store import EvidenceStore
from agrihub.state import (
    CandidateGene,
    EvidenceItem,
    Locus,
    SnpInput,
    StudyState,
    Window,
)

STUB_GENES_PER_LOCUS = 4
STUB_SOURCE = "agrihub-stub"
STUB_VERSION = "plan-1"


async def locus_builder(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
    """Build fixed windows, merge overlaps, and list candidate genes per locus."""
    events.phase("loci")
    study = state.get("study") or {}
    window = Window.model_validate(study.get("window") or {})
    snps = [SnpInput.model_validate(raw) for raw in state.get("snps") or []]
    positioned = [snp for snp in snps if snp.chrom is not None and snp.pos is not None]
    loci, leads = build_loci(positioned, window, str(study.get("assembly") or ""))
    genes = [gene for locus in loci for gene in _stub_genes(locus, leads[locus.locus_id])]
    EvidenceStore.for_run(run_id_from_config(config)).put_items(
        _positional_evidence(gene) for gene in genes
    )
    events.artifact_created(
        "loci_table",
        title="Loci",
        rows=[locus.model_dump(mode="json") for locus in loci],
    )
    unresolved = len(snps) - len(positioned)
    detail = f"{len(loci)} loci, {len(genes)} candidate genes"
    if unresolved:
        detail += f"; {unresolved} marker ids need lookup"
    events.phase("loci", "completed", detail=detail)
    return {
        "loci": [locus.model_dump(mode="json") for locus in loci],
        "candidates": [
            {
                "gene_id": gene.gene_id,
                "locus_id": gene.locus_id,
                "distance_bp": gene.distance_bp,
            }
            for gene in genes
        ],
    }


def build_loci(
    snps: list[SnpInput],
    window: Window,
    assembly: str,
) -> tuple[list[Locus], dict[str, int]]:
    """Merge overlapping ``pos ± flank_bp`` windows per chromosome.

    Returns the loci and the lead SNP position of each locus.
    """
    intervals = sorted(
        (
            (
                str(snp.chrom),
                max(0, int(snp.pos or 0) - window.flank_bp),
                int(snp.pos or 0) + window.flank_bp,
                snp,
            )
            for snp in snps
        ),
        key=lambda interval: (interval[0], interval[1]),
    )
    groups: list[list[tuple[str, int, int, SnpInput]]] = []
    for interval in intervals:
        if (
            groups
            and groups[-1][0][0] == interval[0]
            and interval[1] <= max(member[2] for member in groups[-1])
        ):
            groups[-1].append(interval)
        else:
            groups.append([interval])
    loci: list[Locus] = []
    leads: dict[str, int] = {}
    for index, group in enumerate(groups, start=1):
        members = [member[3] for member in group]
        lead = max(members, key=_strength)
        locus_id = f"L{index}"
        loci.append(
            Locus(
                locus_id=locus_id,
                lead_snp=lead.raw,
                supporting_snps=[snp.raw for snp in members if snp is not lead],
                chrom=group[0][0],
                start=min(member[1] for member in group),
                end=max(member[2] for member in group),
                assembly=assembly,
                window_method=window.mode,
                merged_from=[snp.raw for snp in members] if len(members) > 1 else [],
            )
        )
        leads[locus_id] = int(lead.pos or 0)
    return loci, leads


def _strength(snp: SnpInput) -> float:
    if snp.p_value is not None:
        return 1.0 - snp.p_value
    return snp.score if snp.score is not None else 0.0


def _stub_genes(locus: Locus, lead_pos: int) -> list[CandidateGene]:
    span = max(locus.end - locus.start, 1)
    genes: list[CandidateGene] = []
    for number in range(1, STUB_GENES_PER_LOCUS + 1):
        middle = locus.start + span * number // (STUB_GENES_PER_LOCUS + 1)
        distance = abs(middle - lead_pos)
        genes.append(
            CandidateGene(
                gene_id=f"stub.{locus.locus_id}.g{number}",
                locus_id=locus.locus_id,
                chrom=locus.chrom,
                start=max(0, middle - 1_500),
                end=middle + 1_500,
                strand="+" if number % 2 else "-",
                distance_bp=distance,
                overlaps_snp=distance <= 1_500,
                defline="Stub gene model (no data bundle loaded)",
            )
        )
    return genes


def _positional_evidence(gene: CandidateGene) -> EvidenceItem:
    return EvidenceItem(
        gene_id=gene.gene_id,
        category="positional",
        subtype="distance_to_lead",
        value=gene.model_dump(mode="json"),
        source_db=STUB_SOURCE,
        db_version=STUB_VERSION,
        source_record=f"{gene.locus_id}:{gene.gene_id}",
    )
