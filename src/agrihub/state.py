"""Study request, evidence, and graph state schemas.

Checkpointed state holds plain JSON dictionaries and references only. Bulk
evidence lives in the run-scoped ``EvidenceStore`` so checkpoints stay small.
"""

import operator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, JsonValue, TypeAdapter, model_validator

SpecialistName = Literal[
    "locus_variant",
    "qtl_gwas",
    "function_orthology",
    "expression_network",
    "literature",
]
SPECIALISTS: tuple[SpecialistName, ...] = (
    "locus_variant",
    "qtl_gwas",
    "function_orthology",
    "expression_network",
    "literature",
)
EvidenceCategory = Literal[
    "positional",
    "association",
    "known_gene",
    "functional_annotation",
    "ortholog",
    "expression",
    "network",
    "regulation",
    "variant",
    "literature",
]
Stance = Literal["supports", "conflicts", "neutral"]
Strength = Literal["weak", "moderate", "strong"]
Tier = Literal["T1", "T2", "T3", "T4"]


class Window(BaseModel):
    """How a SNP becomes a locus interval."""

    mode: Literal["fixed", "ld"] = "fixed"
    flank_bp: int = Field(default=250_000, ge=0, le=5_000_000)
    r2: float = Field(default=0.2, ge=0.0, le=1.0)


class LiftedFrom(BaseModel):
    """Where a lifted SNP was given, and how its position on the study assembly was derived."""

    assembly: str
    chrom: str
    pos: int = Field(ge=1)
    method: str
    confidence: Literal["high", "medium", "low"]
    detail: str = ""
    anchors: list[str] = Field(default_factory=list)


class SnpInput(BaseModel):
    """One user-supplied SNP: positioned, a marker id, or raw text placed at intake.

    With neither ``chrom``/``pos`` nor ``marker_id``, intake parses ``raw``
    as a positional id (``S18_9263941``, ``Chr18:9263941``) and otherwise
    resolves it as a marker name. ``lifted_from`` is set by intake when the
    position was converted from another assembly.
    """

    raw: str = Field(min_length=1)
    marker_id: str | None = None
    chrom: str | None = None
    pos: int | None = Field(default=None, ge=1)
    score: float | None = None
    p_value: float | None = Field(default=None, ge=0.0, le=1.0)
    method: str | None = None
    score_type: str | None = None
    """What ``score`` measures when a model produced it (``gnnexplainer``, ``p_value``)."""
    model_id: str | None = None
    lifted_from: LiftedFrom | None = None

    @model_validator(mode="after")
    def require_complete_position(self) -> "SnpInput":
        """Reject a chromosome without a position and a position without a chromosome."""
        if (self.chrom is None) != (self.pos is None):
            raise ValueError("chrom and pos must be given together")
        if not self.raw.strip():
            raise ValueError("raw must not be blank")
        return self


class _StudyBase(BaseModel):
    species: str = Field(min_length=1)
    assembly: str = Field(min_length=1)
    trait_text: str = Field(min_length=1)
    trait_terms: list[str] = Field(default_factory=list)
    window: Window = Field(default_factory=Window)
    population_note: str | None = None
    genotype_vcf_ref: str | None = None
    top_k_per_locus: int = Field(default=5, ge=1, le=50)
    max_genes_per_locus: int = Field(default=200, ge=1, le=2_000)
    specialists_enabled: list[SpecialistName] = Field(
        default_factory=lambda: list(SPECIALISTS),
        min_length=1,
    )
    lifted_from_assembly: str | None = None
    """Set by intake when the SNPs were given on an assembly that is lifted to ``assembly``."""


class SnpStudy(_StudyBase):
    """A study that starts from a SNP list and skips the model step."""

    mode: Literal["snps"]
    snps: list[SnpInput] = Field(min_length=1)


class TraitStudy(_StudyBase):
    """A study that starts from species and trait and needs the model agent."""

    mode: Literal["trait"]
    model_preferences: list[str] = Field(default_factory=list)


StudyRequest = Annotated[SnpStudy | TraitStudy, Field(discriminator="mode")]
STUDY_REQUEST: TypeAdapter[SnpStudy | TraitStudy] = TypeAdapter(StudyRequest)


def parse_study(value: Any) -> SnpStudy | TraitStudy:
    """Validate a study request dictionary against the ``mode`` union."""
    return STUDY_REQUEST.validate_python(value)


class StudyWarning(BaseModel):
    """Something the pipeline changed, dropped or doubts about the input."""

    code: str
    message: str
    snp: str | None = None


class StudyInputIssue(BaseModel):
    """A reason the study cannot run; ``loc`` points into the request (``["snps", 2, "pos"]``)."""

    loc: list[str | int] = Field(default_factory=list)
    message: str


class Locus(BaseModel):
    """A merged genomic interval around one or more SNPs."""

    locus_id: str
    lead_snp: str
    supporting_snps: list[str] = Field(default_factory=list)
    chrom: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    assembly: str
    window_method: Literal["fixed", "ld"]
    merged_from: list[str] = Field(default_factory=list)
    lead_pos: int | None = None
    snp_positions: dict[str, int] = Field(default_factory=dict)
    n_genes: int = 0
    genes_capped: bool = False


class CandidateGene(BaseModel):
    """A gene inside a locus with its positional relation to the nearest SNP of the locus."""

    gene_id: str
    locus_id: str
    symbol: str | None = None
    chrom: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    strand: Literal["+", "-", "."] = "."
    distance_bp: int = Field(ge=0)
    overlaps_snp: bool = False
    nearest_snp: str | None = None
    defline: str = ""


OrthologRelation = Literal["one2one", "one2many", "many2one", "many2many", "family"]
OrthologConfidence = Literal["high", "medium", "low"]


class OrthologRef(BaseModel):
    """The ortholog a transferred fact came from and how well it is supported."""

    species: str = Field(min_length=1)
    gene_id: str = Field(min_length=1)
    relation: OrthologRelation
    n_methods: int = Field(ge=0)
    confidence: OrthologConfidence


class EvidenceItem(BaseModel):
    """One fact about a gene with its provenance.

    ``evidence_id`` and ``alias`` are assigned by the evidence store.
    ``via_ortholog`` is set when the fact describes an ortholog, not the gene.
    """

    evidence_id: str | None = None
    alias: str | None = None
    gene_id: str
    category: EvidenceCategory
    subtype: str
    value: JsonValue = None
    source_db: str
    db_version: str
    source_record: str
    primary_citation: str | None = None
    via_ortholog: OrthologRef | None = None
    evidence_code: str | None = None
    quote: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Finding(BaseModel):
    """A specialist claim that must be backed by stored evidence ids."""

    finding_id: str | None = None
    agent_id: str
    target: str = Field(min_length=1)
    target_type: Literal["gene", "locus"] = "gene"
    claim: str = Field(min_length=1)
    stance: Stance
    strength: Strength
    evidence_ids: list[str] = Field(min_length=1)


class RankedCandidate(BaseModel):
    """One ranked gene in the report.

    ``category_points`` holds rubric points per category (A-G);
    ``evidence_ids`` are the credited evidence behind them. The gene's
    position, its distance to the nearest SNP of the locus (``nearest_snp``)
    and the locus's ``lead_snp`` let a table render without further calls.
    """

    rank: int = Field(ge=1)
    gene_id: str
    locus_id: str
    symbol: str | None = None
    chrom: str | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    strand: Literal["+", "-", "."] = "."
    distance_bp: int | None = Field(default=None, ge=0)
    overlaps_snp: bool = False
    nearest_snp: str | None = None
    lead_snp: str | None = None
    defline: str = ""
    rank_in_locus: int | None = None
    score: float
    share_of_locus: float | None = None
    tier: Tier
    category_points: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    stability: str | None = None
    flags: list[str] = Field(default_factory=list)
    supporting_findings: list[str] = Field(default_factory=list)
    conflicting_findings: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    shortlist: bool = False
    """True when the gene is in the top-K highlighted shortlist of its locus."""
    verifier_status: Literal["verified", "unverified", "contradicted", "unchecked"] = "unchecked"


class SourceRef(BaseModel):
    """A data source cited by the report."""

    source_id: str
    name: str
    version: str
    license: str | None = None
    retrieved_at: str | None = None
    url: str | None = None


class EvidenceRef(BaseModel):
    """A citation the report may point at, with the quote the hover card shows."""

    alias: str
    evidence_id: str
    gene_id: str
    source_db: str
    category: str
    subtype: str
    quote: str | None = None
    verifier_status: str | None = None


Confidence = Literal["strong", "moderate", "suggestive", "positional only"]
TIER_CONFIDENCE: dict[str, Confidence] = {
    "T1": "strong",
    "T2": "moderate",
    "T3": "suggestive",
    "T4": "positional only",
}
"""The confidence term the summary uses for a gene; fixed by its tier, never by the writer."""


class SummaryFinding(BaseModel):
    """One key finding: a complete sentence with inline ``[E12]`` citations."""

    statement: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)


class SummaryCandidate(BaseModel):
    """A short evidence narrative for one top candidate."""

    gene_id: str
    narrative: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)
    symbol: str | None = None
    locus_id: str | None = None
    tier: Tier | None = None
    confidence: Confidence | None = None


class SummaryLocus(BaseModel):
    """One or two sentences on what leads a locus and how decisively."""

    locus_id: str
    narrative: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)


class ReportSummary(BaseModel):
    """The written executive summary, bottom line first.

    Text fields carry inline citation markers. ``tier`` and ``confidence``
    on candidates come from the rubric; ``written_by`` names the writer
    model, or ``template`` when the deterministic fallback wrote it.
    """

    bottom_line: str = Field(min_length=1)
    key_findings: list[SummaryFinding] = Field(default_factory=list)
    candidates: list[SummaryCandidate] = Field(default_factory=list)
    loci: list[SummaryLocus] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    written_by: str = "template"


class ClaimVerdict(BaseModel):
    """A verifier mark on one existing claim. The verifier never adds claims."""

    claim_id: str
    gene_id: str
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    produced_by: str = ""
    status: Literal["verified", "unverified", "contradicted"]
    independent_evidence_ids: list[str] = Field(default_factory=list)
    note: str = ""


class Report(BaseModel):
    """The structured study report. It cites stored evidence ids only."""

    title: str
    species: str
    assembly: str
    trait: str
    mode: Literal["snps", "trait"]
    provenance: dict[str, JsonValue] = Field(default_factory=dict)
    loci: list[Locus] = Field(default_factory=list)
    candidates: list[RankedCandidate] = Field(default_factory=list)
    """Top-K shortlist per locus, the genes the summary highlights."""
    candidates_full: list[RankedCandidate] = Field(default_factory=list)
    """Every scored gene, including those outside the shortlist."""
    verification: list[ClaimVerdict] = Field(default_factory=list)
    citations: list[EvidenceRef] = Field(default_factory=list)
    stability: dict[str, JsonValue] = Field(default_factory=dict)
    """Window-sensitivity summary: top genes per locus at each tested flank."""
    warnings: list[StudyWarning] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    suggested_validations: list[str] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    evidence_count: int = 0
    finding_count: int = 0
    summary: ReportSummary | None = None
    markdown: str = ""
    """The written summary as Markdown, with inline citations."""
    details_markdown: str = ""
    """The full report: study, loci and per-locus candidate tables, warnings and limitations."""


class StudyState(TypedDict, total=False):
    """Checkpointed study state: JSON dictionaries and references only."""

    messages: Annotated[list[AnyMessage], add_messages]
    study: dict[str, Any]
    snps: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    model_result: dict[str, Any]
    loci: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    triage_brief: dict[str, Any]
    dispatches: list[dict[str, Any]]
    findings: Annotated[list[str], operator.add]
    specialist_results: Annotated[list[dict[str, Any]], operator.add]
    orchestrator_usage: Annotated[list[dict[str, Any]], operator.add]
    delta_brief: dict[str, Any]
    round: int
    ranking: list[dict[str, Any]]
    scoring: dict[str, Any]
    report: dict[str, Any] | None
    evidence_snapshot: dict[str, Any]
    study_run_id: str
    """The run that wrote the report; follow-up runs open its evidence store."""
    evidence_snapshot_id: str | None
    """The ``evidence_snapshot`` artifact of that run, used when its directory is gone."""
    followup: str
    run_status: str


class SpecialistTask(TypedDict):
    """The ``Send`` payload for one specialist dispatch."""

    agent_id: str
    specialist: SpecialistName
    round: int
    focus_gene_ids: list[str]
    focus_loci: list[str]
    instructions: str
    rationale: str
    study: dict[str, Any]
    context: dict[str, Any]
    """Per focus gene and locus: position, provisional score and reasons from the triage."""
