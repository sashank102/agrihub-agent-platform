"""Deterministic candidate-gene rubric (master plan section 6).

Every gene gets 0-100 points in seven categories:

- A positional: ``max * exp(-d / LD50)`` from the distance to the nearest SNP
  of its locus, LD50 being the species' typical LD distance, or ``max * r2``
  when the gene has an LD r2 with its lead SNP; plus a bonus for a HIGH or
  MODERATE predicted consequence and one for a UTR, splice, upstream, TFBS
  or conserved-element hit of a lead SNP.
- B same-species functional: curated known trait genes, by trait match and
  curation confidence.
- C ortholog-transferred: TAIR phenotypes, experimental GO and seed-family
  names of the Arabidopsis ortholog that match the trait, times an orthology
  factor (relation, methods, confidence) and a phylogenetic factor.
- D annotation relevance: the gene's own GO terms (experimental full,
  computational 0.3), seed-family names, trait keywords and trait-matching
  pathways.
- E expression: expression in the trait-relevant tissues and tissue
  specificity (tau, z-score) per atlas.
- F network: seed-propagation empirical p, and STRING or ATTED-II neighbours
  that are trait seed genes.
- E and F are reported as not available, never as negative evidence, when
  the bundle has no atlas or network (``available``).
- G convergence: prior QTL (placement, markers, span, overlap, trait match)
  and catalog GWAS hits (distance to the gene, trait match, p-value).

Independence rules, applied per gene:

- A publication (PMID or DOI) is credited once: B, then C, then G, then D.
- Computational GO in D is dropped when C credits an ortholog.
- A curated symbol in D is dropped when B credits the curated record.
- Within a category: best credit + ``second_best`` x the runner-up.
- Paralogs in one locus that inherit C credit from the same ortholog share
  it and are flagged.

Then ``share_of_locus = softmax(score / tau)`` within each locus, and tiers:
T1 ``B >= 15``; T2 ``C >= 12`` and ``A >= 10``; T3 any functional evidence;
T4 positional only. Weights live in ``scoring_weights.yaml`` next to this
module so they can be tuned without code changes.
"""

import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from agrihub.state import CandidateGene, EvidenceItem, Locus, OrthologRef, Tier
from agrihub_data.query.annotation import EXPERIMENTAL_GO_CODES
from agrihub_data.query.traits import TraitProfile

WEIGHTS_FILE = "scoring_weights.yaml"
CATEGORY_CODES = ("A", "B", "C", "D", "E", "F", "G")
CITATION_ORDER = ("B", "C", "G", "D")
FUNCTIONAL_CODES = ("B", "C", "D", "E", "F")
OPTIONAL_CODES = frozenset({"E", "F"})
"""Categories only an extended-tier bundle can fill."""
ORTHOLOG_FIELDS = frozenset({"arabidopsis_symbol", "arabidopsis_best_hit", "tair_description", "tair_curator_summary"})
"""Relevance match fields that describe the Arabidopsis best hit; they score in C, not D."""
_PMID = re.compile(r"PMID:\s*(\d+)", re.IGNORECASE)
_DOI = re.compile(r"doi:\s*(\S+)", re.IGNORECASE)


class DistanceBand(BaseModel):
    """A factor for distances up to ``max_bp``; the last band has no limit."""

    max_bp: int | None = None
    factor: float = Field(ge=0.0)
    label: str | None = None


class CountBand(BaseModel):
    """A factor for counts of at least ``at_least``."""

    at_least: int = Field(ge=0)
    factor: float = Field(ge=0.0)


class PValueBand(BaseModel):
    """A factor for p-values up to ``max``; the last band also takes missing p-values."""

    max: float | None = None
    factor: float = Field(ge=0.0)


class CategorySpec(BaseModel):
    """A rubric category, its maximum, and whether the bundle can fill it yet."""

    name: str
    max: float = Field(ge=0.0)
    available: bool = True
    note: str | None = None


class PositionalWeights(BaseModel):
    """Distance decay or LD r2 for category A, and the variant bonuses."""

    ld_scale: float = Field(gt=0.0)
    not_available: str
    variant_impact: float = Field(ge=0.0)
    regulatory_hit: float = Field(ge=0.0)
    regulatory_classes: list[str]


class KnownGeneWeights(BaseModel):
    """Curated trait genes in category B."""

    trait: dict[str, float]
    confidence: dict[str, float]
    default_confidence: float


class OrthologWeights(BaseModel):
    """Ortholog-transferred function in category C."""

    phenotype_match: float
    experimental_go_match: float
    seed_family: float
    description_keyword: float
    relation: dict[str, float]
    single_method_one2one: float
    confidence: dict[str, float]
    best_hit_only: float
    phylogeny: dict[str, float]
    default_phylogeny: float
    paralog_share_exponent: float = Field(ge=0.0)


class AnnotationWeights(BaseModel):
    """The gene's own annotation in category D."""

    go_experimental: float
    go_computational: float
    family: float
    keyword: float
    pathway: float


class PointBand(BaseModel):
    """Points for a value of at least ``at_least`` (or at most ``max``)."""

    at_least: float | None = None
    max: float | None = None
    points: float = Field(ge=0.0)


class ExpressionWeights(BaseModel):
    """Expression in trait-relevant tissues in category E."""

    trait_tpm: list[PointBand]
    specific: float
    specific_tau: float
    enriched: float
    enriched_z: float


class NetworkWeights(BaseModel):
    """Network proximity to trait seeds in category F."""

    seed_p: list[PointBand]
    seed_neighbor: float
    coexpressed_seed: float


class QtlWeights(BaseModel):
    """How a prior QTL near a gene counts."""

    base: float = Field(ge=0.0)
    trait: dict[str, float]
    overlap: dict[str, float]
    markers: list[CountBand]
    span: list[DistanceBand]
    single_marker: list[DistanceBand]


class GwasWeights(BaseModel):
    """How a published GWAS hit near a gene counts."""

    base: float = Field(ge=0.0)
    trait: dict[str, float]
    distance: list[DistanceBand]
    p_value: list[PValueBand]


class PenaltyWeights(BaseModel):
    """Deductions for gene models that are unlikely candidates."""

    not_expressed: float
    not_expressed_tpm: float
    te_like: float
    te_terms: list[str]


class SensitivityWeights(BaseModel):
    """Flanks re-ranked by the window-sensitivity check."""

    flanks_bp: list[int]


class Rubric(BaseModel):
    """Every tunable weight of the rubric."""

    version: int
    tau: float = Field(gt=0.0)
    second_best: float = Field(ge=0.0, le=1.0)
    categories: dict[str, CategorySpec]
    positional: PositionalWeights
    known_gene: KnownGeneWeights
    ortholog: OrthologWeights
    annotation: AnnotationWeights
    expression: ExpressionWeights
    network: NetworkWeights
    qtl: QtlWeights
    gwas: GwasWeights
    penalties: PenaltyWeights
    tiers: dict[str, dict[str, float]]
    window_sensitivity: SensitivityWeights


class Dropped(BaseModel):
    """Evidence that was found but not credited, and why."""

    evidence_id: str
    reason: str


class CategoryScore(BaseModel):
    """One rubric category of one gene."""

    code: str
    name: str
    points: float
    max_points: float
    available: bool = True
    evidence_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    dropped: list[Dropped] = Field(default_factory=list)
    note: str | None = None


class Penalty(BaseModel):
    """A deduction from a gene's score."""

    reason: str
    points: float


class GeneScore(BaseModel):
    """A gene's rubric score, tier and place in its locus."""

    gene_id: str
    locus_id: str
    symbol: str | None = None
    score: float
    share_of_locus: float = 0.0
    tier: Tier
    rank_in_locus: int = 0
    distance_bp: int
    categories: dict[str, CategoryScore]
    penalties: list[Penalty] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)

    def points(self) -> dict[str, float]:
        """Return points per category code."""
        return {code: category.points for code, category in self.categories.items()}

    def evidence_ids(self) -> list[str]:
        """Return every credited evidence id, category by category."""
        return list(
            dict.fromkeys(evidence_id for category in self.categories.values() for evidence_id in category.evidence_ids)
        )


class StudyScores(BaseModel):
    """Scores for every candidate, ranked per locus."""

    rubric_version: int
    genes: dict[str, GeneScore]
    loci: dict[str, list[str]] = Field(default_factory=dict)
    """Gene ids per locus, best first."""

    def ranked(self, locus_id: str) -> list[GeneScore]:
        """Return a locus's genes best first."""
        return [self.genes[gene_id] for gene_id in self.loci.get(locus_id, [])]

    def explain(self, gene_id: str) -> dict[str, Any]:
        """Return the categories of one gene with the evidence ids behind each.

        Raises:
            KeyError: when the gene was not scored.
        """
        gene = self.genes[gene_id]
        return {
            "gene_id": gene.gene_id,
            "locus_id": gene.locus_id,
            "symbol": gene.symbol,
            "score": gene.score,
            "tier": gene.tier,
            "rank_in_locus": gene.rank_in_locus,
            "share_of_locus": gene.share_of_locus,
            "rubric_version": self.rubric_version,
            "categories": {
                code: {
                    "name": category.name,
                    "points": category.points,
                    "max": category.max_points,
                    "available": category.available,
                    "evidence_ids": list(category.evidence_ids),
                    "reasons": list(category.reasons),
                    "dropped": [item.model_dump() for item in category.dropped],
                    **({"note": category.note} if category.note else {}),
                }
                for code, category in gene.categories.items()
            },
            "penalties": [penalty.model_dump() for penalty in gene.penalties],
            "flags": list(gene.flags),
        }


@dataclass
class _Credit:
    category: str
    points: float
    evidence_id: str
    reason: str
    citations: frozenset[str] = frozenset()
    group: str | None = None
    kind: str = ""


@dataclass
class _GeneCredits:
    candidate: CandidateGene
    positional_points: float
    positional_ids: list[str]
    credits: list[_Credit] = field(default_factory=list)
    ld: _Credit | None = None
    max_expression: float | None = None
    dropped: dict[str, list[Dropped]] = field(default_factory=lambda: defaultdict(list))
    penalties: list[Penalty] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@cache
def _packaged_rubric() -> Rubric:
    text = (resources.files("agrihub") / WEIGHTS_FILE).read_text(encoding="utf-8")
    return Rubric.model_validate(yaml.safe_load(text))


def load_rubric(path: Path | str | None = None) -> Rubric:
    """Return the packaged rubric, or the one in ``path``."""
    if path is None:
        return _packaged_rubric()
    return Rubric.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def band(bands: list[DistanceBand], value: int) -> DistanceBand | None:
    """Return the first band whose ``max_bp`` admits ``value``, or ``None`` past the last limit."""
    for item in bands:
        if item.max_bp is None or value <= item.max_bp:
            return item
    return None


def qtl_points(value: dict[str, Any], rubric: Rubric | None = None) -> tuple[float, str]:
    """Return the points of one ``qtl_overlap`` hit and a short reason.

    Interval QTLs weigh trait match, overlap type, markers placed and span;
    wide QTLs count little. Single-marker QTLs count as marker proximity.
    """
    weights = (rubric or load_rubric()).qtl
    trait = weights.trait.get(str(value.get("trait_match")), 0.0)
    if value.get("kind") == "marker":
        proximity = band(weights.single_marker, int(value.get("distance_to_core") or 0))
        if proximity is None:
            return 0.0, "single-marker QTL too far from the gene"
        factor = proximity.factor
        reason = f"single-marker QTL {value.get('distance_to_core') or 0} bp from the gene"
    else:
        placed = int(value.get("n_markers_placed") or 0)
        markers = next((item.factor for item in weights.markers if placed >= item.at_least), 0.0)
        span = band(weights.span, int(value.get("span_bp") or 0))
        overlap = weights.overlap.get(str(value.get("overlap_type")), 0.0)
        factor = markers * (span.factor if span else 0.0) * overlap
        reason = (
            f"{placed}-marker QTL, {int(value.get('span_bp') or 0) / 1e6:.2f} Mb"
            + (" (wide)" if value.get("wide") else "")
            + f", {value.get('overlap_type')}"
        )
    return round(weights.base * trait * factor, 3), f"{reason}, trait match {value.get('trait_match')}"


def gwas_points(value: dict[str, Any], rubric: Rubric | None = None) -> tuple[float, str]:
    """Return the points of one ``gwas_catalog_overlap`` hit and a short reason.

    Distance from the hit to the gene body, trait match (ontology over
    keyword) and p-value multiply the base weight.
    """
    weights = (rubric or load_rubric()).gwas
    trait = weights.trait.get(str(value.get("trait_match")), 0.0)
    distance = int(value.get("distance_to_core") or 0)
    proximity = band(weights.distance, distance) or weights.distance[-1]
    p_value = value.get("p_value")
    p_factor = weights.p_value[-1].factor
    if p_value is not None:
        p_factor = next(
            (item.factor for item in weights.p_value if item.max is None or float(p_value) <= item.max),
            p_factor,
        )
    points = weights.base * trait * proximity.factor * p_factor
    p_text = f"p={float(p_value):.1e}" if p_value is not None else "no p-value"
    return round(points, 3), f"GWAS hit {proximity.label or distance}, {p_text}, trait match {value.get('trait_match')}"


def ortholog_factor(ref: OrthologRef | None, rubric: Rubric | None = None) -> float:
    """Return how much function transfers through an ortholog call (best-hit-only without one)."""
    weights = (rubric or load_rubric()).ortholog
    if ref is None:
        return weights.best_hit_only
    relation = weights.relation.get(ref.relation, weights.best_hit_only)
    if ref.relation == "one2one" and ref.n_methods < 2:
        relation = weights.single_method_one2one
    return relation * weights.confidence.get(ref.confidence, 1.0)


def score_candidates(
    candidates: Iterable[CandidateGene],
    evidence: Iterable[EvidenceItem],
    *,
    profile: TraitProfile,
    ld_kb: float,
    rubric: Rubric | None = None,
    available: set[str] | None = None,
) -> StudyScores:
    """Score every candidate from its stored evidence; the same input always gives the same output.

    ``available`` names the categories the bundle can fill (``E``, ``F``);
    the others are reported as not available. ``None`` uses the rubric's
    own flags.
    """
    weights = rubric or load_rubric()
    by_gene: dict[str, list[EvidenceItem]] = defaultdict(list)
    for item in evidence:
        by_gene[item.gene_id].append(item)
    raw = [
        _gene_credits(candidate, by_gene.get(candidate.gene_id, []), profile, ld_kb, weights)
        for candidate in sorted(candidates, key=lambda gene: (gene.locus_id, gene.gene_id))
    ]
    _share_paralog_credit(raw, weights)
    genes = {credits.candidate.gene_id: _finalize(credits, weights, available) for credits in raw}
    loci: dict[str, list[GeneScore]] = defaultdict(list)
    for gene in genes.values():
        loci[gene.locus_id].append(gene)
    order: dict[str, list[str]] = {}
    for locus_id, members in sorted(loci.items()):
        members.sort(key=lambda gene: (-gene.score, gene.distance_bp, gene.gene_id))
        top = max(gene.score for gene in members)
        exponents = [math.exp((gene.score - top) / weights.tau) for gene in members]
        total = sum(exponents)
        for rank, (gene, exponent) in enumerate(zip(members, exponents, strict=True), start=1):
            gene.rank_in_locus = rank
            gene.share_of_locus = round(exponent / total, 4)
        order[locus_id] = [gene.gene_id for gene in members]
    return StudyScores(rubric_version=weights.version, genes=genes, loci=order)


def available_categories(domains: Iterable[str]) -> set[str]:
    """Return the rubric categories a set of available evidence domains can fill."""
    present = set(domains)
    codes = {"B", "C", "D", "G"}
    if "variant_location" in present:
        codes.add("A")
    if "expression" in present:
        codes.add("E")
    if present & {"network", "coexpression"}:
        codes.add("F")
    return codes


def explain_score(scores: StudyScores, gene_id: str) -> dict[str, Any]:
    """Return a gene's per-category points with the evidence ids behind each.

    Raises:
        KeyError: when the gene was not scored.
    """
    return scores.explain(gene_id)


def _gene_credits(
    candidate: CandidateGene,
    items: list[EvidenceItem],
    profile: TraitProfile,
    ld_kb: float,
    rubric: Rubric,
) -> _GeneCredits:
    items = sorted(items, key=lambda item: (item.category, item.subtype, str(item.evidence_id)))
    ld_bp = ld_kb * 1_000 * rubric.positional.ld_scale
    positional = rubric.categories["A"].max * math.exp(-candidate.distance_bp / ld_bp)
    credits = _GeneCredits(
        candidate=candidate,
        positional_points=positional,
        positional_ids=[str(item.evidence_id) for item in items if item.category == "positional" and item.subtype == "in_window"],
    )
    calls = {
        str((item.value or {}).get("target_gene_id")): item.via_ortholog
        for item in items
        if item.category == "ortholog" and item.subtype.startswith("ortholog:") and isinstance(item.value, dict)
    }
    best_hit = next(
        (
            str(item.value.get("id"))
            for item in items
            if item.category == "functional_annotation" and item.subtype == "arabidopsis_best_hit" and isinstance(item.value, dict)
        ),
        None,
    )
    wanted_go = set(profile.expanded_ids) | set(profile.ids())
    for item in items:
        evidence_id = str(item.evidence_id)
        value = item.value if isinstance(item.value, dict) else {}
        citations = _citations(item)
        if item.category == "known_gene":
            credits.credits.append(_known_gene_credit(item, value, citations, rubric))
        elif item.category == "ortholog" and item.subtype.startswith("tair:"):
            credit = _tair_credit(item, value, wanted_go, profile, citations, rubric)
            if credit is not None:
                credits.credits.append(credit)
        elif item.category == "functional_annotation" and item.subtype.startswith("relevance:"):
            credits.credits.append(_relevance_credit(evidence_id, value, best_hit, calls, rubric))
        elif item.category == "association" and item.subtype.startswith("qtl:"):
            points, reason = qtl_points(value, rubric)
            credits.credits.append(_Credit("G", points, evidence_id, f"{value.get('trait_name')}: {reason}", citations))
        elif item.category == "association" and item.subtype.startswith("gwas:"):
            points, reason = gwas_points(value, rubric)
            credits.credits.append(_Credit("G", points, evidence_id, f"{value.get('trait_name')}: {reason}", citations))
        elif item.category == "functional_annotation" and item.subtype.startswith("pathway:"):
            if value.get("matched"):
                credits.credits.append(
                    _Credit("D", rubric.annotation.pathway, evidence_id, f"pathway {value.get('pathway_name')} matches {value['matched'][0]}", kind="pathway")
                )
        elif item.category == "expression":
            if item.subtype.startswith("expression_profile:"):
                peak = float(value.get("max_value") or 0.0)
                credits.max_expression = max(peak, credits.max_expression or 0.0)
            credit = _expression_credit(evidence_id, item.subtype, value, rubric)
            if credit is not None:
                credits.credits.append(credit)
        elif item.category == "network":
            credit = _network_credit(evidence_id, item.subtype, value, rubric)
            if credit is not None:
                credits.credits.append(credit)
        elif item.category in {"variant", "regulation"}:
            credit = _variant_credit(evidence_id, item.category, item.subtype, value, rubric)
            if credit is not None:
                credits.credits.append(credit)
        elif item.category == "positional" and item.subtype.startswith("ld_r2:"):
            r2 = float(value.get("r2") or 0.0)
            if credits.ld is None or r2 > credits.ld.points:
                credits.ld = _Credit("A", r2, evidence_id, f"r2 {r2:g} with {value.get('lead')} on {value.get('genotypes')}", kind="ld")
    if credits.max_expression is not None and credits.max_expression < rubric.penalties.not_expressed_tpm:
        credits.penalties.append(
            Penalty(reason=f"not expressed in any atlas sample (max {credits.max_expression:g} TPM)", points=rubric.penalties.not_expressed)
        )
    defline = candidate.defline.casefold()
    terms = [term for term in rubric.penalties.te_terms if term in defline]
    if terms:
        credits.penalties.append(Penalty(reason=f"transposable-element-like gene model ({terms[0]})", points=rubric.penalties.te_like))
    return credits


def _known_gene_credit(
    item: EvidenceItem,
    value: dict[str, Any],
    citations: frozenset[str],
    rubric: Rubric,
) -> _Credit:
    weights = rubric.known_gene
    match = str(value.get("trait_match") or "none")
    confidence = weights.confidence.get(str(value.get("confidence")), weights.default_confidence)
    points = weights.trait.get(match, 0.0) * confidence * float(value.get("weight") or 1.0)
    symbols = "/".join((value.get("symbols") or [])[:2]) or str(value.get("gene_id"))
    if points > 0:
        matched = ", ".join((value.get("matched") or [])[:2])
        reason = f"curated {symbols} for this trait ({match}: {matched}), confidence {value.get('confidence')}"
    else:
        reason = f"curated {symbols} for other traits: {', '.join((value.get('trait_names') or [])[:3])}"
    return _Credit("B", round(points, 3), str(item.evidence_id), reason, citations, kind=f"known_gene:{match}")


def _tair_credit(
    item: EvidenceItem,
    value: dict[str, Any],
    wanted_go: set[str],
    profile: TraitProfile,
    citations: frozenset[str],
    rubric: Rubric,
) -> _Credit | None:
    weights = rubric.ortholog
    via = item.via_ortholog
    factor = ortholog_factor(via, rubric) * weights.phylogeny.get(via.species if via else "", weights.default_phylogeny)
    target = via.gene_id if via else "ortholog"
    if item.subtype == "tair:phenotype":
        keywords = profile.matched_keywords(item.quote)
        if not keywords:
            return None
        return _Credit(
            "C",
            round(weights.phenotype_match * factor, 3),
            str(item.evidence_id),
            f"{target} mutant phenotype mentions {', '.join(keywords[:2])}",
            citations,
            group=target,
            kind="phenotype",
        )
    if item.subtype.startswith("tair:go:"):
        go_id = item.subtype.removeprefix("tair:go:")
        if go_id not in wanted_go:
            return None
        return _Credit(
            "C",
            round(weights.experimental_go_match * factor, 3),
            str(item.evidence_id),
            f"{target} {value.get('evidence_code')} {go_id} {value.get('name') or ''}".strip(),
            citations,
            group=target,
            kind="experimental_go",
        )
    return None


def _relevance_credit(
    evidence_id: str,
    value: dict[str, Any],
    best_hit: str | None,
    calls: dict[str, OrthologRef | None],
    rubric: Rubric,
) -> _Credit:
    kind = str(value.get("kind"))
    term = str(value.get("term"))
    where = str(value.get("field"))
    if where in ORTHOLOG_FIELDS:
        weights = rubric.ortholog
        via = calls.get(best_hit or "")
        factor = ortholog_factor(via, rubric) * weights.phylogeny.get("arabidopsis", weights.default_phylogeny)
        base = weights.seed_family if kind == "family" else weights.description_keyword
        label = "seed family" if kind == "family" else "keyword"
        return _Credit(
            "C",
            round(base * factor, 3),
            evidence_id,
            f"best hit {best_hit or '?'} {label} {term} ({where})",
            group=best_hit,
            kind=f"relevance:{kind}",
        )
    weights_d = rubric.annotation
    if kind == "go":
        experimental = value.get("evidence_code") in EXPERIMENTAL_GO_CODES
        points = weights_d.go_experimental if experimental else weights_d.go_computational
        label = value.get("label") or ""
        return _Credit(
            "D",
            points,
            evidence_id,
            f"{value.get('evidence_code')} {term} {label}".strip(),
            kind="go" if experimental else "projected_go",
        )
    if kind == "family":
        return _Credit(
            "D",
            weights_d.family,
            evidence_id,
            f"seed family {term} in {where}",
            kind="curated_symbol" if where == "symbol" else "family",
        )
    return _Credit("D", weights_d.keyword, evidence_id, f"keyword {term} in {where}", kind="keyword")


def _band_points(bands: list[PointBand], value: float) -> float:
    for item in bands:
        if (item.at_least is not None and value >= item.at_least) or (item.max is not None and value <= item.max):
            return item.points
    return 0.0


def _expression_credit(evidence_id: str, subtype: str, value: dict[str, Any], rubric: Rubric) -> _Credit | None:
    weights = rubric.expression
    dataset = subtype.split(":", 1)[1] if ":" in subtype else ""
    if subtype.startswith("expression_profile:"):
        if value.get("trait_max_value") is None:
            return None
        trait_max = float(value["trait_max_value"])
        points = _band_points(weights.trait_tpm, trait_max)
        if points <= 0:
            return None
        return _Credit("E", points, evidence_id, f"{trait_max:g} TPM in {value.get('trait_max_sample')} ({dataset})", kind="trait_expression")
    if subtype.startswith("tissue_specificity:"):
        tau = value.get("tau")
        trait_z = value.get("trait_z")
        if value.get("trait_tissue_top") and tau is not None and float(tau) >= weights.specific_tau:
            return _Credit("E", weights.specific, evidence_id, f"specific to {value.get('top_tissue')} (tau {float(tau):.2f}, {dataset})", kind="specific")
        if trait_z is not None and float(trait_z) >= weights.enriched_z:
            return _Credit("E", weights.enriched, evidence_id, f"enriched in a trait tissue (z {float(trait_z):.1f}, {dataset})", kind="enriched")
    return None


def _network_credit(evidence_id: str, subtype: str, value: dict[str, Any], rubric: Rubric) -> _Credit | None:
    weights = rubric.network
    if subtype.startswith("seed_propagation:"):
        p_value = value.get("empirical_p")
        if p_value is None or value.get("is_seed"):
            return None
        points = _band_points(weights.seed_p, float(p_value))
        if points <= 0:
            return None
        return _Credit("F", points, evidence_id, f"near {value.get('trait_key')} seeds on {value.get('network')} (empirical p {float(p_value):.3g})", kind="seed_propagation")
    if value.get("neighbor_is_seed"):
        if subtype.startswith("coexpression:"):
            return _Credit("F", weights.coexpressed_seed, evidence_id, f"co-expressed with seed {value.get('neighbor_id')} (z {value.get('score')})", kind="coexpressed_seed")
        if subtype.startswith("neighbor:") and float(value.get("score") or 0.0) >= 0.7:
            return _Credit("F", weights.seed_neighbor, evidence_id, f"STRING neighbour of seed {value.get('neighbor_id')} (score {value.get('score')})", kind="seed_neighbor")
    return None


def _variant_credit(evidence_id: str, category: str, subtype: str, value: dict[str, Any], rubric: Rubric) -> _Credit | None:
    weights = rubric.positional
    if category == "regulation":
        if subtype.startswith(("tfbs_hit:", "cns_hit:")):
            kind = "TFBS" if subtype.startswith("tfbs_hit:") else "conserved element"
            return _Credit("A", weights.regulatory_hit, evidence_id, f"{value.get('snp')} in a {kind} ({value.get('relation') or 'near'} this gene)", kind="regulatory_hit")
        return None
    impact = str(value.get("impact") or "").upper()
    if impact in {"HIGH", "MODERATE"}:
        terms = ",".join((value.get("consequences") or [{}])[0].get("terms") or [])
        return _Credit("A", weights.variant_impact, evidence_id, f"{value.get('variant')} {terms} ({impact})", kind="variant_impact")
    if value.get("location_class") in weights.regulatory_classes:
        return _Credit("A", weights.regulatory_hit, evidence_id, f"{value.get('variant')} in the {value.get('location_class')}", kind="regulatory_hit")
    return None


def _share_paralog_credit(genes: list[_GeneCredits], rubric: Rubric) -> None:
    holders: dict[tuple[str, str], set[str]] = defaultdict(set)
    for credits in genes:
        for credit in credits.credits:
            if credit.category == "C" and credit.group and credit.points > 0:
                holders[(credits.candidate.locus_id, credit.group)].add(credits.candidate.gene_id)
    exponent = rubric.ortholog.paralog_share_exponent
    for credits in genes:
        for (locus_id, group), members in sorted(holders.items()):
            if locus_id != credits.candidate.locus_id or credits.candidate.gene_id not in members or len(members) < 2:
                continue
            others = sorted(members - {credits.candidate.gene_id})
            credits.flags.append(f"paralog: shares {group} ortholog credit with {', '.join(others)}")
            share = len(members) ** -exponent
            for credit in credits.credits:
                if credit.category == "C" and credit.group == group:
                    credit.points = round(credit.points * share, 3)
                    credit.reason += f" (shared by {len(members)} paralogs)"


def _finalize(credits: _GeneCredits, rubric: Rubric, available: set[str] | None = None) -> GeneScore:
    by_category: dict[str, list[_Credit]] = defaultdict(list)
    for credit in credits.credits:
        by_category[credit.category].append(credit)
    dropped = credits.dropped
    if any(credit.points > 0 for credit in by_category["B"]):
        _drop(by_category["D"], dropped["D"], "curated_symbol", "the curated symbol is the record already credited in B")
    if any(credit.points > 0 for credit in by_category["C"]):
        _drop(by_category["D"], dropped["D"], "projected_go", "computational GO is not independent of the ortholog credited in C")
    consumed: dict[str, str] = {}
    for code in CITATION_ORDER:
        kept = []
        for credit in sorted(by_category[code], key=lambda item: (-item.points, item.evidence_id)):
            earlier = sorted(citation for citation in credit.citations if consumed.get(citation, code) != code)
            if earlier:
                dropped[code].append(
                    Dropped(evidence_id=credit.evidence_id, reason=f"{earlier[0]} is already credited in {consumed[earlier[0]]}")
                )
                continue
            kept.append(credit)
            if credit.points > 0:
                for citation in credit.citations:
                    consumed.setdefault(citation, code)
        by_category[code] = kept
    categories: dict[str, CategoryScore] = {}
    for code in CATEGORY_CODES:
        spec = rubric.categories[code]
        if code == "A":
            base, reason = credits.positional_points, f"{credits.candidate.distance_bp} bp from {credits.candidate.nearest_snp or 'the lead SNP'}"
            evidence_ids = list(credits.positional_ids)
            if credits.ld is not None:
                base, reason = spec.max * credits.ld.points, credits.ld.reason
                evidence_ids.append(credits.ld.evidence_id)
            bonuses = []
            for kind in ("variant_impact", "regulatory_hit"):
                best = max((credit for credit in by_category["A"] if credit.kind == kind), key=lambda credit: (credit.points, credit.evidence_id), default=None)
                if best is not None:
                    bonuses.append(best)
            categories[code] = CategoryScore(
                code=code,
                name=spec.name,
                points=round(min(spec.max, base + sum(credit.points for credit in bonuses)), 2),
                max_points=spec.max,
                evidence_ids=[*evidence_ids, *(credit.evidence_id for credit in bonuses)],
                reasons=[reason, *(credit.reason for credit in bonuses)],
                note=None if available is None or "A" in available else rubric.positional.not_available,
            )
            continue
        ranked = sorted(by_category[code], key=lambda item: (-item.points, item.evidence_id))
        counted = [credit for credit in ranked if credit.points > 0]
        for credit in ranked:
            if credit.points <= 0:
                dropped[code].append(Dropped(evidence_id=credit.evidence_id, reason=credit.reason))
        total = 0.0
        if counted:
            total = counted[0].points + (rubric.second_best * counted[1].points if len(counted) > 1 else 0.0)
        is_available = spec.available if available is None or code not in OPTIONAL_CODES else code in available
        categories[code] = CategoryScore(
            code=code,
            name=spec.name,
            points=round(min(spec.max, total), 2),
            max_points=spec.max,
            available=is_available,
            evidence_ids=[credit.evidence_id for credit in counted],
            reasons=[credit.reason for credit in counted[:3]],
            dropped=dropped.get(code, []),
            note=spec.note if not is_available else None,
        )
    points = {code: category.points for code, category in categories.items()}
    penalty = sum(item.points for item in credits.penalties)
    score = round(max(0.0, min(100.0, sum(points.values()) - penalty)), 2)
    return GeneScore(
        gene_id=credits.candidate.gene_id,
        locus_id=credits.candidate.locus_id,
        symbol=credits.candidate.symbol,
        score=score,
        tier=_tier(points, rubric),
        distance_bp=credits.candidate.distance_bp,
        categories=categories,
        penalties=credits.penalties,
        flags=credits.flags,
    )


def _drop(credits: list[_Credit], dropped: list[Dropped], kind: str, reason: str) -> None:
    for credit in [credit for credit in credits if credit.kind == kind]:
        credits.remove(credit)
        dropped.append(Dropped(evidence_id=credit.evidence_id, reason=reason))


def _tier(points: dict[str, float], rubric: Rubric) -> Tier:
    for tier in ("T1", "T2"):
        rule = rubric.tiers.get(tier) or {}
        if rule and all(points.get(code, 0.0) >= minimum for code, minimum in rule.items()):
            return "T1" if tier == "T1" else "T2"
    if any(points.get(code, 0.0) > 0 for code in FUNCTIONAL_CODES):
        return "T3"
    return "T4"


def _citations(item: EvidenceItem) -> frozenset[str]:
    texts = [item.primary_citation or ""]
    value = item.value if isinstance(item.value, dict) else {}
    if item.category == "known_gene":
        texts.extend(f"PMID:{pmid}" for pmid in value.get("pmids") or [])
        texts.extend(f"doi:{doi}" for doi in value.get("dois") or [])
    found = {f"PMID:{match}" for text in texts for match in _PMID.findall(text)}
    found |= {f"doi:{match.casefold().rstrip('.')}" for text in texts for match in _DOI.findall(text)}
    return frozenset(found)


class GeneStability(BaseModel):
    """A gene's rank in its locus at each tested flank."""

    gene_id: str
    locus_id: str
    ranks: dict[str, int | None]
    """Rank per flank label (``50kb``); ``None`` when the gene is outside that window."""
    stable: bool
    label: str


class LocusSensitivity(BaseModel):
    """The top genes of one locus at each tested flank."""

    locus_id: str
    n_genes: dict[str, int]
    top: dict[str, list[str]]


class WindowSensitivity(BaseModel):
    """How candidate ranks move when the fixed window changes."""

    flanks_bp: list[int]
    top_k: int
    loci: list[LocusSensitivity]
    genes: dict[str, GeneStability]


def flank_label(flank_bp: int) -> str:
    """Return ``50kb`` for 50000."""
    return f"{flank_bp // 1_000}kb" if flank_bp % 1_000 == 0 else f"{flank_bp}bp"


def window_sensitivity(
    loci: list[Locus],
    scores: StudyScores,
    genes_for: Callable[[Locus, int], list[CandidateGene]],
    rescore: Callable[[list[CandidateGene]], StudyScores],
    *,
    flanks: list[int] | None = None,
    top_k: int = 5,
    rubric: Rubric | None = None,
) -> WindowSensitivity:
    """Re-rank each locus with the windows its SNPs would get at other flanks.

    ``genes_for(locus, flank)`` lists the genes in the union of the locus's
    SNP windows at ``flank``; genes not yet scored are scored with
    ``rescore``. Scores do not depend on the flank (distances are to the
    nearest SNP), so only membership changes. A gene is stable when it stays
    in the top ``top_k`` at every flank.
    """
    weights = rubric or load_rubric()
    tested = sorted(set(flanks or weights.window_sensitivity.flanks_bp))
    labels = [flank_label(flank) for flank in tested]
    results: list[LocusSensitivity] = []
    ranks: dict[str, dict[str, int | None]] = defaultdict(dict)
    owner: dict[str, str] = {}
    for locus in loci:
        n_genes: dict[str, int] = {}
        top: dict[str, list[str]] = {}
        per_flank: dict[str, list[str]] = {}
        for flank, label in zip(tested, labels, strict=True):
            members = genes_for(locus, flank)
            missing = [gene for gene in members if gene.gene_id not in scores.genes]
            extra = rescore(missing).genes if missing else {}
            scored = [scores.genes.get(gene.gene_id) or extra[gene.gene_id] for gene in members]
            scored.sort(key=lambda gene: (-gene.score, gene.distance_bp, gene.gene_id))
            per_flank[label] = [gene.gene_id for gene in scored]
            n_genes[label] = len(scored)
            top[label] = per_flank[label][:top_k]
        followed = {gene_id for ids in top.values() for gene_id in ids} | set(
            (scores.loci.get(locus.locus_id) or [])[:top_k]
        )
        for gene_id in sorted(followed):
            owner[gene_id] = locus.locus_id
            for label in labels:
                ordered = per_flank[label]
                ranks[gene_id][label] = ordered.index(gene_id) + 1 if gene_id in ordered else None
        results.append(LocusSensitivity(locus_id=locus.locus_id, n_genes=n_genes, top=top))
    genes = {}
    for gene_id, by_flank in sorted(ranks.items()):
        inside = [label for label in labels if (by_flank[label] or top_k + 1) <= top_k]
        stable = len(inside) == len(labels)
        if stable:
            label = f"top {top_k} at every flank ({', '.join(labels)})"
        elif inside:
            label = f"top {top_k} only at {', '.join(inside)}"
        else:
            label = f"outside the top {top_k} at every flank"
        genes[gene_id] = GeneStability(gene_id=gene_id, locus_id=owner[gene_id], ranks=by_flank, stable=stable, label=label)
    return WindowSensitivity(flanks_bp=tested, top_k=top_k, loci=results, genes=genes)
