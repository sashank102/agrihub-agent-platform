"""Functional annotation of genes and its relevance to a trait profile."""

import re
from collections import defaultdict
from typing import Any, Literal

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
FAMILY_WEIGHT = 1.0
KEYWORD_WEIGHT = 0.5
EXPERIMENTAL_GO_WEIGHT = 1.0
COMPUTATIONAL_GO_WEIGHT = 0.3
SHORT_FAMILY_CHARS = 2
"""Families this short (FT, E1, CO) match symbols only; in free text they collide with words like 'enzyme E1'."""
SYMBOL_FIELDS = frozenset({"symbol", "arabidopsis_symbol"})
_TOKEN = re.compile(r"[A-Za-z0-9]+")
MatchKind = Literal["go", "keyword", "family"]


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
    """One reason a gene's annotation fits the trait.

    ``field`` says where the match was found. ``symbol`` is the gene's own
    curated symbol; ``arabidopsis_symbol``, ``arabidopsis_best_hit``,
    ``tair_description`` and ``tair_curator_summary`` describe its
    Arabidopsis best hit, so they are ortholog-derived.
    """

    kind: MatchKind
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
    """Score how each gene's GO terms, names and descriptions match a trait profile.

    GO matches use the profile's terms and their descendants; experimental
    codes weigh 1.0 and computational ones 0.3. Seed families match whole
    symbol tokens, allowing a paralog suffix (``GA20ox`` matches
    ``GA20OX1``), in the gene's curated symbols (species prefix stripped),
    its Arabidopsis best hit's TAIR symbols and label, the defline and the
    domain labels; they weigh 1.0. Families of two characters or fewer match
    symbols only. Keyword matches in the defline, domain labels, best-hit
    label, TAIR short description and TAIR curator summary weigh 0.5.
    """
    annotations = gene_annotation(bundle, gene_ids, assembly)
    prefixes = registry_of(bundle).symbol_prefixes
    best_hits = sorted({a.arabidopsis_best_hit.id for a in annotations if a.arabidopsis_best_hit})
    tair: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    if best_hits:
        for row in bundle.rows(
            "SELECT gene_id, kind, value FROM annotation WHERE species = 'arabidopsis' "
            "AND kind IN ('symbol', 'short_description', 'curator_summary') "
            f"AND gene_id IN ({', '.join('?' for _ in best_hits)}) ORDER BY gene_id, kind, value",
            best_hits,
        ):
            tair[row["gene_id"]][row["kind"]].append(str(row["value"]))
    wanted_go = set(profile.expanded_ids) | set(profile.go)
    results = []
    for annotation in annotations:
        if not annotation.found:
            continue
        matches: list[RelevanceMatch] = []
        for term in annotation.go:
            if term.id in wanted_go:
                weight = EXPERIMENTAL_GO_WEIGHT if term.evidence_code in EXPERIMENTAL_GO_CODES else COMPUTATIONAL_GO_WEIGHT
                matches.append(
                    RelevanceMatch(kind="go", term=term.id, label=term.name, field="go", evidence_code=term.evidence_code, weight=weight)
                )
        hit = annotation.arabidopsis_best_hit
        facts = tair.get(hit.id, {}) if hit else {}
        hit_symbols, hit_description = _split_best_hit_label(hit.label if hit else None)
        domains = " ".join(term.label or "" for terms in annotation.domains.values() for term in terms)
        # The curated symbol comes from the same record as known-gene evidence,
        # so it is used only when no independent field names the family.
        family_fields = {
            "arabidopsis_symbol": " ".join([*facts.get("symbol", []), hit_symbols]),
            "defline": annotation.defline,
            "domains": domains,
            "arabidopsis_best_hit": hit_description,
            "symbol": " ".join(_strip_prefix(symbol, prefixes) for symbol in annotation.symbols),
        }
        found_families: set[str] = set()
        for field, text in family_fields.items():
            for family in matched_families(profile.seed_families, text, symbols=field in SYMBOL_FIELDS):
                if family.casefold() not in found_families:
                    found_families.add(family.casefold())
                    matches.append(RelevanceMatch(kind="family", term=family, field=field, weight=FAMILY_WEIGHT))
        keyword_fields = {
            "defline": annotation.defline,
            "domains": domains,
            "arabidopsis_best_hit": hit.label if hit else None,
            "tair_description": " ".join(facts.get("short_description", [])),
            "tair_curator_summary": " ".join(facts.get("curator_summary", [])),
        }
        seen: set[str] = set()
        for field, text in keyword_fields.items():
            for keyword in profile.matched_keywords(text):
                if keyword not in seen:
                    seen.add(keyword)
                    matches.append(RelevanceMatch(kind="keyword", term=keyword, field=field, weight=KEYWORD_WEIGHT))
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


def matched_families(families: list[str], text: str | None, *, symbols: bool) -> list[str]:
    """Return seed families that name a whole token of ``text``.

    A token matches its family exactly or with a paralog suffix: digits and an
    optional letter after a family ending in a letter (``PRR`` -> ``PRR7``,
    ``FT`` -> ``FT2a``), one letter after a family ending in a digit (``GID1`` ->
    ``GID1B``). Outside symbol fields, families of two characters or fewer
    are skipped.
    """
    if not text:
        return []
    tokens = {token.casefold() for token in _TOKEN.findall(text)}
    found = []
    for family in families:
        if not symbols and len(family) <= SHORT_FAMILY_CHARS:
            continue
        pattern = _family_pattern(family)
        if any(pattern.fullmatch(token) for token in tokens):
            found.append(family)
    return found


def _family_pattern(family: str) -> re.Pattern[str]:
    stem = re.escape(family.casefold())
    suffix = r"(?:\d+[a-z]?)?" if family[-1:].isalpha() else r"[a-z]?"
    return re.compile(stem + suffix)


def _strip_prefix(symbol: str, prefixes: list[str]) -> str:
    for prefix in prefixes:
        rest = symbol[len(prefix) :]
        if symbol.startswith(prefix) and rest[:1].isupper():
            return rest
    return symbol


def _split_best_hit_label(label: str | None) -> tuple[str, str | None]:
    """Split ``TFL-1,TFL1: PEBP family protein`` into its symbol list and description."""
    if not label:
        return "", None
    head, separator, tail = label.partition(":")
    if separator and head and " " not in head.strip():
        return head, tail.strip() or None
    return "", label
