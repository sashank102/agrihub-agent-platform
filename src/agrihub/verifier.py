"""Re-check top-candidate claims against an independent source.

The verifier never adds a claim. Each existing supporting claim is marked
``verified``, ``unverified`` or ``contradicted``. Literature mentions and
passages that do not co-mention the gene alias, the species and the trait
are downgraded.
"""

import re
from typing import Literal

from agrihub.state import ClaimVerdict, EvidenceItem, Finding, RankedCandidate

TOP_PER_STUDY = 10
TOP_PER_LOCUS = 3
_TOKEN = re.compile(r"[a-z0-9]{4,}")
_SPECIES_ALIASES = {
    "soybean": ("soybean", "glycine max", "glycine"),
}


def select_top(
    candidates: list[RankedCandidate],
    *,
    per_study: int = TOP_PER_STUDY,
    per_locus: int = TOP_PER_LOCUS,
) -> list[RankedCandidate]:
    """Return the top candidates: ``per_locus`` of each locus, then ``per_study`` overall."""
    chosen: list[RankedCandidate] = []
    seen: set[str] = set()
    by_locus: dict[str, list[RankedCandidate]] = {}
    for candidate in candidates:
        by_locus.setdefault(candidate.locus_id, []).append(candidate)
    for locus_id in sorted(by_locus):
        rows = sorted(by_locus[locus_id], key=lambda item: (item.rank_in_locus or 10**6, item.gene_id))
        for candidate in rows[:per_locus]:
            if candidate.gene_id not in seen:
                chosen.append(candidate)
                seen.add(candidate.gene_id)
    chosen.sort(key=lambda item: (-item.score, item.gene_id))
    return chosen[:per_study]


def verify_claims(
    candidates: list[RankedCandidate],
    evidence: list[EvidenceItem],
    findings: list[Finding],
    *,
    species: str,
    trait: str,
    per_study: int = TOP_PER_STUDY,
    per_locus: int = TOP_PER_LOCUS,
) -> list[ClaimVerdict]:
    """Re-check supporting claims of the top candidates. No new claims are created."""
    by_id = _index(evidence)
    selected = {candidate.gene_id for candidate in select_top(candidates, per_study=per_study, per_locus=per_locus)}
    verdicts: list[ClaimVerdict] = []
    for finding in findings:
        if finding.target not in selected or finding.stance != "supports" or finding.target_type != "gene":
            continue
        cited = [by_id[key] for key in finding.evidence_ids if key in by_id]
        verdicts.append(
            _verdict(
                claim_id=str(finding.finding_id or f"finding:{finding.target}:{len(verdicts)}"),
                gene_id=finding.target,
                text=finding.claim,
                cited=cited,
                pool=evidence,
                species=species,
                trait=trait,
                aliases=_aliases(finding.target, candidates, cited),
            )
        )
    for candidate in candidates:
        if candidate.gene_id not in selected or not candidate.reasons:
            continue
        cited = [by_id[key] for key in candidate.evidence_ids if key in by_id]
        if not cited:
            continue
        verdicts.append(
            _verdict(
                claim_id=f"{candidate.gene_id}:score",
                gene_id=candidate.gene_id,
                text="; ".join(candidate.reasons[:3]),
                cited=cited[:6],
                pool=evidence,
                species=species,
                trait=trait,
                aliases=_aliases(candidate.gene_id, candidates, cited),
            )
        )
    return verdicts


def status_for(verdicts: list[ClaimVerdict]) -> Literal["verified", "unverified", "contradicted", "unchecked"]:
    """Summarize a gene's verdicts. Contradicted wins; an empty list is unchecked."""
    if not verdicts:
        return "unchecked"
    if any(item.status == "contradicted" for item in verdicts):
        return "contradicted"
    if all(item.status == "verified" for item in verdicts):
        return "verified"
    return "unverified"


def _verdict(
    *,
    claim_id: str,
    gene_id: str,
    text: str,
    cited: list[EvidenceItem],
    pool: list[EvidenceItem],
    species: str,
    trait: str,
    aliases: set[str],
) -> ClaimVerdict:
    produced = cited[0].source_db if cited else ""
    literature = _literature_note(cited, species, trait, aliases)
    if literature == "rejected":
        return ClaimVerdict(
            claim_id=claim_id,
            gene_id=gene_id,
            text=text,
            evidence_ids=[str(item.alias or item.evidence_id) for item in cited],
            produced_by=produced,
            status="contradicted",
            note="downgraded: the passage does not co-mention the alias, species and trait",
        )
    if literature == "mention":
        return ClaimVerdict(
            claim_id=claim_id,
            gene_id=gene_id,
            text=text,
            evidence_ids=[str(item.alias or item.evidence_id) for item in cited],
            produced_by=produced,
            status="unverified",
            note="downgraded: literature mention only",
        )
    contradicted = _independent(cited, pool, contradict=True)
    if contradicted:
        return ClaimVerdict(
            claim_id=claim_id,
            gene_id=gene_id,
            text=text,
            evidence_ids=[str(item.alias or item.evidence_id) for item in cited],
            produced_by=produced,
            status="contradicted",
            independent_evidence_ids=[str(item.alias or item.evidence_id) for item in contradicted],
            note=f"contradicted by {contradicted[0].source_db}",
        )
    agreed = _independent(cited, pool, contradict=False)
    if agreed:
        return ClaimVerdict(
            claim_id=claim_id,
            gene_id=gene_id,
            text=text,
            evidence_ids=[str(item.alias or item.evidence_id) for item in cited],
            produced_by=produced,
            status="verified",
            independent_evidence_ids=[str(item.alias or item.evidence_id) for item in agreed],
            note=f"verified with {agreed[0].source_db}",
        )
    return ClaimVerdict(
        claim_id=claim_id,
        gene_id=gene_id,
        text=text,
        evidence_ids=[str(item.alias or item.evidence_id) for item in cited],
        produced_by=produced,
        status="unverified",
        note="no independent source",
    )


def _literature_note(cited: list[EvidenceItem], species: str, trait: str, aliases: set[str]) -> str | None:
    """Return ``mention`` or ``rejected`` when literature text does not support the claim."""
    passages = [item for item in cited if item.category == "literature"]
    if not passages:
        return None
    if any(item.subtype in {"gene2pubmed", "passage:mention"} or item.subtype.endswith(":mention") for item in passages):
        return "mention"
    names = {species.casefold(), *_SPECIES_ALIASES.get(species.casefold(), ())}
    traits = {trait.casefold(), *(token for token in _TOKEN.findall(trait.casefold()))}
    alias_needles = {alias.casefold() for alias in aliases if len(alias) >= 3}
    for item in passages:
        if not str(item.subtype).startswith("passage:"):
            continue
        quote = (item.quote or "").casefold()
        if not quote:
            return "rejected"
        if not any(name in quote for name in names if name):
            return "rejected"
        if not any(term in quote for term in traits if len(term) >= 4):
            return "rejected"
        if alias_needles and not any(alias in quote for alias in alias_needles):
            return "rejected"
    return None


def _independent(cited: list[EvidenceItem], pool: list[EvidenceItem], *, contradict: bool) -> list[EvidenceItem]:
    """Return evidence from a different source that agrees with or contradicts ``cited``."""
    found: list[EvidenceItem] = []
    targets = {str(item.evidence_id) for item in cited} | {str(item.alias) for item in cited if item.alias}
    sources = {item.source_db for item in cited}
    for item in pool:
        if item.source_db in sources or item.gene_id not in {row.gene_id for row in cited}:
            continue
        value = item.value if isinstance(item.value, dict) else {}
        marker = str(value.get("contradicts") if contradict else value.get("supports") or "")
        if marker and marker in targets:
            found.append(item)
    return found


def _index(evidence: list[EvidenceItem]) -> dict[str, EvidenceItem]:
    index: dict[str, EvidenceItem] = {}
    for item in evidence:
        if item.evidence_id:
            index[str(item.evidence_id)] = item
        if item.alias:
            index[item.alias] = item
    return index


def _aliases(gene_id: str, candidates: list[RankedCandidate], cited: list[EvidenceItem]) -> set[str]:
    names = {gene_id}
    for candidate in candidates:
        if candidate.gene_id == gene_id and candidate.symbol:
            names.add(candidate.symbol)
    for item in cited:
        value = item.value if isinstance(item.value, dict) else {}
        if isinstance(value.get("alias"), str):
            names.add(str(value["alias"]))
        for symbol in value.get("symbols") or []:
            names.add(str(symbol))
    return names

