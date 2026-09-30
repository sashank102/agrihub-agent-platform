"""Ortholog consensus and what the Arabidopsis ortholog is known to do."""

import hashlib
import re
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from agrihub.state import (
    EvidenceItem,
    OrthologConfidence,
    OrthologRef,
    OrthologRelation,
)
from agrihub_data.bundle import Bundle
from agrihub_data.query.annotation import EXPERIMENTAL_GO_CODES
from agrihub_data.query.common import registry_of

_AGI = re.compile(r"^AT[1-5CM]G\d{5}$", re.IGNORECASE)
TREE_METHODS = frozenset({"compara", "plaza_TROG"})
"""Phylogeny-based methods; a call needs one of them to be ``high`` confidence."""
MAX_TARGETS_PER_GENE = 5
MAX_PHENOTYPES = 5
MAX_GO = 12


class OrthologCall(BaseModel):
    """A consensus ortholog of one gene in one target species."""

    gene_id: str
    assembly: str
    target_species: str
    target_gene_id: str
    target_symbol: str | None = None
    methods: list[str]
    n_methods: int
    databases: list[str]
    relation: OrthologRelation
    confidence: OrthologConfidence
    identity: float | None = None
    source_version: str

    def ref(self) -> OrthologRef:
        """Return the structured reference carried by transferred evidence."""
        return OrthologRef(
            species=self.target_species,
            gene_id=self.target_gene_id,
            relation=self.relation,
            n_methods=self.n_methods,
            confidence=self.confidence,
        )

    def evidence(self) -> list[EvidenceItem]:
        """Return the ortholog call as one fact."""
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="ortholog",
                subtype=f"ortholog:{self.target_species}",
                value={
                    "target_gene_id": self.target_gene_id,
                    "target_symbol": self.target_symbol,
                    "methods": self.methods,
                    "n_methods": self.n_methods,
                    "relation": self.relation,
                    "confidence": self.confidence,
                    "identity": self.identity,
                },
                source_db=" + ".join(self.databases),
                db_version=self.source_version,
                source_record=f"{self.target_species}:{self.target_gene_id}",
                via_ortholog=self.ref(),
            )
        ]


class ArabidopsisPhenotype(BaseModel):
    """A TAIR germplasm phenotype."""

    germplasm: str | None
    phenotype: str
    pmid: str | None


class ArabidopsisGo(BaseModel):
    """An experimentally supported TAIR GO annotation."""

    go_id: str
    name: str | None
    evidence_code: str
    reference: str | None


class ArabidopsisKnowledge(BaseModel):
    """What TAIR records about an Arabidopsis gene, optionally reached through an ortholog."""

    gene_id: str
    """The soybean gene when reached through ``via``, otherwise the AGI."""
    agi: str
    found: bool
    symbols: list[str] = Field(default_factory=list)
    full_name: str | None = None
    short_description: str | None = None
    curator_summary: str | None = None
    computational_description: str | None = None
    phenotypes: list[ArabidopsisPhenotype] = Field(default_factory=list)
    n_phenotypes: int = 0
    go: list[ArabidopsisGo] = Field(default_factory=list)
    n_publications: int = 0
    via: OrthologCall | None = None
    source_version: str = ""

    def evidence(self) -> list[EvidenceItem]:
        """Return description, phenotype and experimental GO facts about the AGI."""
        if not self.found:
            return []
        via = self.via.ref() if self.via else None
        record = f"TAIR:{self.agi}"

        def item(subtype: str, value: Any, record_suffix: str = "", **extra: Any) -> EvidenceItem:
            return EvidenceItem(
                gene_id=self.gene_id,
                category="ortholog",
                subtype=subtype,
                value=value,
                source_db="TAIR",
                db_version=self.source_version,
                source_record=record + record_suffix,
                via_ortholog=via,
                **extra,
            )

        items = []
        summary = self.curator_summary or self.short_description or self.computational_description
        if summary or self.symbols:
            items.append(
                item(
                    "tair:description",
                    {
                        "symbols": self.symbols,
                        "full_name": self.full_name,
                        "short_description": self.short_description,
                        "curator_summary": self.curator_summary,
                        "n_publications": self.n_publications,
                    },
                    quote=summary,
                )
            )
        for phenotype in self.phenotypes:
            digest = hashlib.sha1(phenotype.phenotype.encode()).hexdigest()[:8]
            items.append(
                item(
                    "tair:phenotype",
                    phenotype.model_dump(),
                    f":{phenotype.germplasm or ''}:{digest}",
                    quote=phenotype.phenotype,
                    primary_citation=f"PMID:{phenotype.pmid}" if phenotype.pmid else None,
                )
            )
        for term in self.go:
            items.append(
                item(
                    f"tair:go:{term.go_id}",
                    term.model_dump(),
                    evidence_code=term.evidence_code,
                    primary_citation=term.reference,
                )
            )
        return items


def get_orthologs(
    bundle: Bundle,
    gene_ids: list[str],
    targets: list[str] | None = None,
) -> list[OrthologCall]:
    """Return consensus orthologs per gene, best supported first.

    Methods are Ensembl Compara, PLAZA's four integrative methods (TROG,
    BHIF, ORTHO, anchor_point) and the LIS BLAST best hit. ``high`` needs at
    least three methods including a tree-based one; ``medium`` needs two;
    anything else is ``low``. Relation comes from Compara when it has the
    pair, otherwise from how many genes each side has in the call set.
    """
    registry = registry_of(bundle)
    canonical = registry.canonical_assembly
    species = targets or ["arabidopsis"]
    wanted = list(dict.fromkeys(gene for gene in gene_ids if gene))
    if not wanted:
        return []
    genes = ", ".join("?" for _ in wanted)
    kinds = ", ".join("?" for _ in species)
    rows = bundle.rows(
        "SELECT gene_id, target_species, target_gene_id, method, relation, identity, source_db, source_version "
        f"FROM orthologs WHERE assembly = ? AND gene_id IN ({genes}) AND target_species IN ({kinds})",
        [canonical, *wanted, *species],
    )
    pairs: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        pairs[(row["gene_id"], row["target_species"], row["target_gene_id"])].append(row)
    compara_targets: dict[tuple[str, str], int] = defaultdict(int)
    supported_targets: dict[tuple[str, str], int] = defaultdict(int)
    for (gene_id, target_species, _), methods_rows in pairs.items():
        if any(row["method"] == "compara" for row in methods_rows):
            compara_targets[(gene_id, target_species)] += 1
        if len({row["method"] for row in methods_rows}) >= 2:
            supported_targets[(gene_id, target_species)] += 1
    target_ids = sorted({key[2] for key in pairs})
    compara_sources, supported_sources = _sources_per_target(bundle, canonical, target_ids)
    symbols = _symbols(bundle, target_ids)
    calls: list[OrthologCall] = []
    for (gene_id, target_species, target_gene), methods_rows in pairs.items():
        methods = sorted({str(row["method"]) for row in methods_rows})
        compara = next((row for row in methods_rows if row["method"] == "compara"), None)
        if compara is not None:
            many_targets = compara_targets[(gene_id, target_species)] > 1
            many_sources = compara_sources.get(target_gene, 1) > 1
        else:
            many_targets = supported_targets[(gene_id, target_species)] > 1
            many_sources = supported_sources.get(target_gene, 1) > 1
        relation = _relation(compara["relation"] if compara else None, many_targets, many_sources)
        calls.append(
            OrthologCall(
                gene_id=gene_id,
                assembly=canonical,
                target_species=target_species,
                target_gene_id=target_gene,
                target_symbol=symbols.get(target_gene),
                methods=methods,
                n_methods=len(methods),
                databases=sorted({str(row["source_db"]) for row in methods_rows}),
                relation=relation,
                confidence=_confidence(methods),
                identity=float(compara["identity"]) if compara and compara["identity"] is not None else None,
                source_version="; ".join(sorted({str(row["source_version"]) for row in methods_rows})),
            )
        )
    order = {gene: index for index, gene in enumerate(wanted)}
    calls.sort(key=lambda call: (order[call.gene_id], -call.n_methods, -(call.identity or 0.0), call.target_gene_id))
    kept: list[OrthologCall] = []
    per_gene: dict[tuple[str, str], int] = defaultdict(int)
    for call in calls:
        key = (call.gene_id, call.target_species)
        if per_gene[key] < MAX_TARGETS_PER_GENE:
            kept.append(call)
            per_gene[key] += 1
    return kept


def arabidopsis_knowledge(bundle: Bundle, ids: list[str]) -> list[ArabidopsisKnowledge]:
    """Return TAIR knowledge for AGIs, or for soybean genes through their best ortholog.

    Soybean genes are resolved with :func:`get_orthologs`; only the top call
    of at least ``medium`` confidence is followed, and every fact carries it as
    ``via_ortholog``.
    """
    requests: list[tuple[str, str, OrthologCall | None]] = []
    soybean = [identifier for identifier in ids if not _AGI.match(identifier.strip())]
    best: dict[str, OrthologCall] = {}
    for call in get_orthologs(bundle, soybean, ["arabidopsis"]):
        if call.gene_id not in best and call.confidence != "low":
            best[call.gene_id] = call
    for identifier in dict.fromkeys(item.strip() for item in ids if item.strip()):
        if _AGI.match(identifier):
            requests.append((identifier.upper(), identifier.upper(), None))
        elif identifier in best:
            requests.append((identifier, best[identifier].target_gene_id, best[identifier]))
    agis = sorted({agi for _, agi, _ in requests})
    if not agis:
        return []
    placeholders = ", ".join("?" for _ in agis)
    annotation: dict[str, dict[str, list[tuple[str, str | None]]]] = defaultdict(lambda: defaultdict(list))
    version = ""
    for row in bundle.rows(
        "SELECT gene_id, kind, value, label, source_version FROM annotation "
        f"WHERE species = 'arabidopsis' AND gene_id IN ({placeholders}) ORDER BY gene_id, kind, value",
        agis,
    ):
        annotation[row["gene_id"]][row["kind"]].append((row["value"], row["label"]))
        version = str(row["source_version"])
    phenotypes: dict[str, list[ArabidopsisPhenotype]] = defaultdict(list)
    for row in bundle.rows(
        "SELECT gene_id, germplasm, phenotype, pmid FROM phenotypes "
        f"WHERE species = 'arabidopsis' AND gene_id IN ({placeholders}) ORDER BY gene_id, pmid NULLS LAST, germplasm",
        agis,
    ):
        phenotypes[row["gene_id"]].append(
            ArabidopsisPhenotype(germplasm=row["germplasm"], phenotype=row["phenotype"], pmid=row["pmid"])
        )
    go_terms: dict[str, list[ArabidopsisGo]] = defaultdict(list)
    codes = sorted(EXPERIMENTAL_GO_CODES)
    for row in bundle.rows(
        "SELECT g.gene_id, g.go_id, t.name, g.evidence_code, g.reference FROM go_annot AS g "
        "LEFT JOIN ontology_terms AS t ON t.term_id = g.go_id "
        f"WHERE g.species = 'arabidopsis' AND g.gene_id IN ({placeholders}) "
        f"AND g.evidence_code IN ({', '.join('?' for _ in codes)}) ORDER BY g.gene_id, g.go_id, g.evidence_code",
        [*agis, *codes],
    ):
        if all(term.go_id != row["go_id"] for term in go_terms[row["gene_id"]]):
            go_terms[row["gene_id"]].append(
                ArabidopsisGo(go_id=row["go_id"], name=row["name"], evidence_code=row["evidence_code"], reference=row["reference"])
            )
    papers = {
        str(gene_id): int(count)
        for gene_id, count in bundle.rows_raw(
            "SELECT gene_id, count(DISTINCT pmid) FROM gene_publications "
            f"WHERE species = 'arabidopsis' AND gene_id IN ({placeholders}) GROUP BY gene_id",
            agis,
        )
    }
    results = []
    for gene_id, agi, via in requests:
        facts = annotation.get(agi, {})

        def first(kind: str, facts: dict[str, list[tuple[str, str | None]]] = facts) -> str | None:
            values = facts.get(kind)
            return values[0][0] if values else None

        symbols = [value for value, _ in facts.get("symbol", [])]
        full_names = [label for _, label in facts.get("symbol", []) if label]
        found = bool(facts) or agi in phenotypes or agi in go_terms
        results.append(
            ArabidopsisKnowledge(
                gene_id=gene_id,
                agi=agi,
                found=found,
                symbols=symbols,
                full_name=full_names[0] if full_names else None,
                short_description=first("short_description"),
                curator_summary=first("curator_summary"),
                computational_description=first("computational_description"),
                phenotypes=phenotypes.get(agi, [])[:MAX_PHENOTYPES],
                n_phenotypes=len(phenotypes.get(agi, [])),
                go=go_terms.get(agi, [])[:MAX_GO],
                n_publications=papers.get(agi, 0),
                via=via,
                source_version=version,
            )
        )
    return results


def _relation(compara: str | None, many_targets: bool, many_sources: bool) -> OrthologRelation:
    """Orient a cardinality from the soybean gene's side.

    Compara's ``one2many`` is symmetric, so the side with several members
    decides between ``one2many`` and ``many2one``. Without Compara, counts
    come from calls supported by at least two methods.
    """
    if compara == "one2one":
        return "one2one"
    if compara == "many2many":
        return "many2many"
    if compara == "one2many":
        return "one2many" if many_targets and not many_sources else "many2one"
    if many_targets and many_sources:
        return "many2many"
    if many_targets:
        return "one2many"
    if many_sources:
        return "many2one"
    return "one2one"


def _confidence(methods: list[str]) -> OrthologConfidence:
    if len(methods) >= 3 and TREE_METHODS & set(methods):
        return "high"
    if len(methods) >= 2:
        return "medium"
    return "low"


def _sources_per_target(
    bundle: Bundle,
    assembly: str,
    targets: list[str],
) -> tuple[dict[str, int], dict[str, int]]:
    """Return soybean genes per target from Compara, and from calls with two or more methods."""
    if not targets:
        return {}, {}
    rows = bundle.rows_raw(
        """
        SELECT target_gene_id,
               count(*) FILTER (WHERE has_compara),
               count(*) FILTER (WHERE n_methods >= 2)
        FROM (
            SELECT gene_id, target_gene_id, bool_or(method = 'compara') AS has_compara,
                   count(DISTINCT method) AS n_methods
            FROM orthologs
            WHERE assembly = ? AND target_gene_id IN ({placeholders})
            GROUP BY 1, 2
        )
        GROUP BY 1
        """.format(placeholders=", ".join("?" for _ in targets)),
        [assembly, *targets],
    )
    return (
        {str(target): int(compara) for target, compara, _ in rows},
        {str(target): int(supported) for target, _, supported in rows},
    )


def _symbols(bundle: Bundle, targets: list[str]) -> dict[str, str]:
    if not targets:
        return {}
    rows = bundle.rows_raw(
        "SELECT gene_id, min(value) FROM annotation WHERE species = 'arabidopsis' AND kind = 'symbol' "
        f"AND gene_id IN ({', '.join('?' for _ in targets)}) GROUP BY 1",
        targets,
    )
    return {str(gene): str(symbol) for gene, symbol in rows}
