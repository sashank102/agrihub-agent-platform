"""Functional annotation of genes and its relevance to a trait profile."""

from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from agrihub.state import EvidenceItem
from agrihub_data.bundle import Bundle
from agrihub_data.query.common import registry_of
from agrihub_data.query.traits import TraitProfile

EXPERIMENTAL_GO_CODES = frozenset(
    {"EXP", "IDA", "IPI", "IMP", "IGI", "IEP", "HTP", "HDA", "HMP", "HGI", "HEP", "TAS", "IC"}
)
"""Codes that count fully in the rubric; IEA/ISS/ISO and similar count 0.3."""
ANNOTATION_KINDS = ("pfam", "panther", "kog", "ec", "ko", "interpro")


class Term(BaseModel):
    """An annotation value with its label."""

    id: str
    label: str | None = None


class GoTerm(BaseModel):
    """A GO annotation with its evidence code."""

    id: str
    name: str | None = None
    evidence_code: str


class GeneAnnotation(BaseModel):
    """Everything the bundle says a gene is."""

    gene_id: str
    assembly: str
    found: bool
    defline: str | None = None
    symbols: list[str] = Field(default_factory=list)
    domains: dict[str, list[Term]] = Field(default_factory=dict)
    go: list[GoTerm] = Field(default_factory=list)
    arabidopsis_best_hit: Term | None = None
    rice_best_hit: Term | None = None
    source_db: str = "LIS"
    source_version: str = ""

    def evidence(self) -> list[EvidenceItem]:
        """Return one item per defline, domain, GO term and best hit."""
        if not self.found:
            return []
        record = f"{self.assembly}:{self.gene_id}"

        def item(subtype: str, value: Any, **extra: Any) -> EvidenceItem:
            return EvidenceItem(
                gene_id=self.gene_id,
                category="functional_annotation",
                subtype=subtype,
                value=value,
                source_db=self.source_db,
                db_version=self.source_version,
                source_record=record,
                **extra,
            )

        items = []
        if self.defline:
            items.append(item("defline", self.defline, quote=self.defline))
        for kind, terms in self.domains.items():
            items.extend(item(f"{kind}:{term.id}", term.model_dump(exclude_none=True)) for term in terms)
        items.extend(
            item(f"go:{term.id}", term.model_dump(exclude_none=True), evidence_code=term.evidence_code)
            for term in self.go
        )
        for subtype, hit in (("arabidopsis_best_hit", self.arabidopsis_best_hit), ("rice_best_hit", self.rice_best_hit)):
            if hit is not None:
                items.append(item(subtype, hit.model_dump(exclude_none=True)))
        return items


class RelevanceMatch(BaseModel):
    """One reason a gene's annotation fits the trait."""

    kind: str
    """``go`` or ``keyword``."""
    term: str
    label: str | None = None
    field: str
    evidence_code: str | None = None
    weight: float


class GeneRelevance(BaseModel):
    """How a gene's annotation relates to a trait profile."""

    gene_id: str
    assembly: str
    trait_key: str
    score: float
    matches: list[RelevanceMatch] = Field(default_factory=list)
    source_version: str = ""

    def evidence(self) -> list[EvidenceItem]:
        """Return one item per GO or keyword match, keyed by the trait profile."""
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="functional_annotation",
                subtype=f"relevance:{match.kind}:{match.term}",
                value=match.model_dump(exclude_none=True),
                source_db="AgriHub annotation relevance",
                db_version=self.source_version,
                source_record=f"{self.trait_key}|{self.assembly}:{self.gene_id}",
                evidence_code=match.evidence_code,
            )
            for match in self.matches
        ]


def gene_annotation(bundle: Bundle, gene_ids: list[str], assembly: str | None = None) -> list[GeneAnnotation]:
    """Return defline, domains, GO, symbols and best hits for each gene, in input order."""
    registry = registry_of(bundle)
    target = registry.assembly(assembly).id
    wanted = list(dict.fromkeys(gene for gene in gene_ids if gene))
    if not wanted:
        return []
    placeholders = ", ".join("?" for _ in wanted)
    genes = {
        row["gene_id"]: row
        for row in bundle.rows(
            f"SELECT gene_id, defline, source_db, source_version FROM genes WHERE assembly = ? AND gene_id IN ({placeholders})",
            [target, *wanted],
        )
    }
    annotations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in bundle.rows(
        f"SELECT gene_id, kind, value, label FROM annotation WHERE assembly = ? AND gene_id IN ({placeholders}) "
        "ORDER BY gene_id, kind, value",
        [target, *wanted],
    ):
        annotations[row["gene_id"]].append(row)
    go_terms: dict[str, list[GoTerm]] = defaultdict(list)
    for row in bundle.rows(
        "SELECT g.gene_id, g.go_id, g.evidence_code, t.name FROM go_annot AS g "
        "LEFT JOIN ontology_terms AS t ON t.term_id = g.go_id "
        f"WHERE g.assembly = ? AND g.gene_id IN ({placeholders}) ORDER BY g.gene_id, g.go_id",
        [target, *wanted],
    ):
        go_terms[row["gene_id"]].append(GoTerm(id=row["go_id"], name=row["name"], evidence_code=row["evidence_code"]))
    symbols: dict[str, list[str]] = defaultdict(list)
    for row in bundle.rows(
        f"SELECT gene_id, symbols FROM known_genes WHERE assembly = ? AND gene_id IN ({placeholders})",
        [target, *wanted],
    ):
        for symbol in row["symbols"] or []:
            if symbol not in symbols[row["gene_id"]]:
                symbols[row["gene_id"]].append(symbol)
    results = []
    for gene_id in wanted:
        gene = genes.get(gene_id)
        if gene is None:
            results.append(GeneAnnotation(gene_id=gene_id, assembly=target, found=False))
            continue
        domains: dict[str, list[Term]] = defaultdict(list)
        best: dict[str, Term] = {}
        for row in annotations.get(gene_id, []):
            if row["kind"] in ANNOTATION_KINDS:
                domains[row["kind"]].append(Term(id=row["value"], label=row["label"]))
            elif row["kind"] in {"arabidopsis_best_hit", "rice_best_hit"}:
                best[row["kind"]] = Term(id=row["value"], label=row["label"])
        results.append(
            GeneAnnotation(
                gene_id=gene_id,
                assembly=target,
                found=True,
                defline=gene["defline"],
                symbols=symbols.get(gene_id, []),
                domains=dict(domains),
                go=go_terms.get(gene_id, []),
                arabidopsis_best_hit=best.get("arabidopsis_best_hit"),
                rice_best_hit=best.get("rice_best_hit"),
                source_db=str(gene["source_db"]),
                source_version=str(gene["source_version"]),
            )
        )
    return results


def annotation_relevance(
    bundle: Bundle,
    gene_ids: list[str],
    profile: TraitProfile,
    assembly: str | None = None,
) -> list[GeneRelevance]:
    """Score how each gene's GO terms and descriptions match a trait profile.

    GO matches use the profile's terms and their descendants; experimental
    codes weigh 1.0 and computational ones 0.3. Keyword matches in the
    defline, domain labels, Arabidopsis best hit and its TAIR description
    weigh 0.5.
    """
    annotations = gene_annotation(bundle, gene_ids, assembly)
    best_hits = sorted({a.arabidopsis_best_hit.id for a in annotations if a.arabidopsis_best_hit})
    descriptions: dict[str, str] = {}
    if best_hits:
        for row in bundle.rows(
            "SELECT gene_id, string_agg(value, ' ') AS text FROM annotation WHERE species = 'arabidopsis' "
            f"AND kind IN ('short_description', 'curator_summary') AND gene_id IN ({', '.join('?' for _ in best_hits)}) "
            "GROUP BY gene_id",
            best_hits,
        ):
            descriptions[row["gene_id"]] = str(row["text"])
    wanted_go = set(profile.expanded_ids) | set(profile.go)
    results = []
    for annotation in annotations:
        if not annotation.found:
            continue
        matches: list[RelevanceMatch] = []
        for term in annotation.go:
            if term.id in wanted_go:
                weight = 1.0 if term.evidence_code in EXPERIMENTAL_GO_CODES else 0.3
                matches.append(
                    RelevanceMatch(kind="go", term=term.id, label=term.name, field="go", evidence_code=term.evidence_code, weight=weight)
                )
        hit = annotation.arabidopsis_best_hit
        fields = {
            "defline": annotation.defline,
            "domains": " ".join(term.label or "" for terms in annotation.domains.values() for term in terms),
            "arabidopsis_best_hit": hit.label if hit else None,
            "tair_description": descriptions.get(hit.id) if hit else None,
        }
        seen: set[str] = set()
        for field, text in fields.items():
            for keyword in profile.matched_keywords(text):
                if keyword not in seen:
                    seen.add(keyword)
                    matches.append(RelevanceMatch(kind="keyword", term=keyword, field=field, weight=0.5))
        results.append(
            GeneRelevance(
                gene_id=annotation.gene_id,
                assembly=annotation.assembly,
                trait_key=profile.key,
                score=round(sum(match.weight for match in matches), 2),
                matches=matches,
                source_version=annotation.source_version,
            )
        )
    results.sort(key=lambda relevance: -relevance.score)
    return results
