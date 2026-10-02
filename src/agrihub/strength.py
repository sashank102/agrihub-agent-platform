"""The strongest claim the cited evidence can carry.

``record_finding`` caps every finding at :func:`max_strength` of the evidence
it cites, so a specialist cannot call a wide QTL or a keyword match
"moderate". Per evidence item:

- curated trait gene: ``strong`` for an ontology match with curation
  confidence 3 or more, ``moderate`` for a weaker match, otherwise ``weak``;
- QTL: ``moderate`` only for an ontology-matched interval placed from two or
  more markers and spanning at most ``NARROW_QTL_BP``; single-marker, wide or
  keyword-matched QTLs are ``weak``;
- catalog GWAS hit: ``moderate`` for an ontology match within
  ``GWAS_NEAR_BP`` of the gene, otherwise ``weak``;
- ortholog-transferred TAIR phenotype or experimental GO: ``moderate`` with
  a medium- or high-confidence ortholog call, otherwise ``weak``; the call
  itself and descriptions are ``weak``;
- the gene's own GO: ``moderate`` with an experimental evidence code;
- expression: ``moderate`` when the gene is specific (tau at least
  ``SPECIFIC_TAU``) to a trait-relevant tissue; network: ``moderate`` at an
  empirical p of at most ``NETWORK_P``; variant: ``moderate`` for a HIGH or
  MODERATE consequence;
- literature: ``strong`` for a causal-experimental passage, ``moderate`` for
  an association passage, ``weak`` for mentions and links;
- everything else (positional, regulation, annotation keywords) is ``weak``.

A finding may be as strong as its best item; it may be ``strong`` without a
strong item when it cites both a moderate QTL and a moderate GWAS hit
(convergence).
"""

from dataclasses import dataclass
from typing import Any

from agrihub.state import EvidenceItem, Strength
from agrihub_data.query.annotation import EXPERIMENTAL_GO_CODES

LEVELS: tuple[Strength, ...] = ("weak", "moderate", "strong")
NARROW_QTL_BP = 1_000_000
GWAS_NEAR_BP = 50_000
SPECIFIC_TAU = 0.8
NETWORK_P = 0.01
STRONG_CURATION = 3


@dataclass(frozen=True)
class Cap:
    """The strongest claim one item supports and why."""

    strength: Strength
    reason: str
    kind: str = ""


def item_cap(item: EvidenceItem) -> Cap:
    """Return the strongest claim one evidence item can support."""
    value: dict[str, Any] = item.value if isinstance(item.value, dict) else {}
    alias = item.alias or item.evidence_id or "evidence"
    if item.category == "known_gene":
        match = str(value.get("trait_match") or "none")
        confidence = value.get("confidence")
        if match == "ontology" and (confidence is None or int(confidence) >= STRONG_CURATION):
            return Cap("strong", f"{alias} is a curated {item.gene_id} record for this trait")
        if match in {"ontology", "keyword"}:
            return Cap("moderate", f"{alias} is a curated record with a {match} trait match, confidence {confidence}")
        return Cap("weak", f"{alias} is a curated record for other traits")
    if item.category == "association" and item.subtype.startswith("qtl:"):
        return _qtl_cap(alias, value)
    if item.category == "association" and item.subtype.startswith("gwas:"):
        distance = int(value.get("distance_to_core") or 0)
        if value.get("trait_match") != "ontology":
            return Cap("weak", f"{alias} is a GWAS hit matched by keyword only")
        if distance > GWAS_NEAR_BP:
            return Cap("weak", f"{alias} is a GWAS hit {distance / 1000:.0f} kb from the gene")
        return Cap("moderate", f"{alias} is an ontology-matched GWAS hit within {GWAS_NEAR_BP // 1000} kb", "gwas")
    if item.category == "ortholog":
        if item.subtype == "tair:phenotype" or item.subtype.startswith("tair:go:"):
            via = item.via_ortholog
            if via is not None and via.confidence in {"high", "medium"}:
                return Cap("moderate", f"{alias} is transferred from a {via.confidence}-confidence ortholog")
            return Cap("weak", f"{alias} is transferred from a low-confidence or best-hit ortholog")
        return Cap("weak", f"{alias} is an ortholog call or description, not a phenotype")
    if item.category == "functional_annotation" and item.subtype.startswith("go:"):
        if item.evidence_code in EXPERIMENTAL_GO_CODES:
            return Cap("moderate", f"{alias} is GO with experimental code {item.evidence_code}")
        return Cap("weak", f"{alias} is computational GO ({item.evidence_code})")
    if item.category == "expression":
        tau = value.get("tau")
        if value.get("trait_tissue_top") and tau is not None and float(tau) >= SPECIFIC_TAU:
            return Cap("moderate", f"{alias} shows tissue-specific expression (tau {float(tau):.2f}) in a trait tissue")
        return Cap("weak", f"{alias} is expression without trait-tissue specificity")
    if item.category == "network":
        p_value = value.get("empirical_p")
        if p_value is not None and float(p_value) <= NETWORK_P:
            return Cap("moderate", f"{alias} is network proximity to trait genes at empirical p {float(p_value):.3g}")
        return Cap("weak", f"{alias} is network or co-expression context without a significant seed proximity")
    if item.category == "variant":
        if str(value.get("impact") or "").upper() in {"HIGH", "MODERATE"}:
            return Cap("moderate", f"{alias} is a {str(value.get('impact')).upper()}-impact variant")
        return Cap("weak", f"{alias} is a variant location without a coding consequence")
    if item.category == "literature":
        if item.subtype == "passage:causal-experimental":
            return Cap("strong", f"{alias} is a causal-experimental passage")
        if item.subtype == "passage:association":
            return Cap("moderate", f"{alias} is an association passage")
        return Cap("weak", f"{alias} is a mention or a publication link")
    return Cap("weak", f"{alias} is {item.category.replace('_', ' ')} evidence")


def max_strength(items: list[EvidenceItem]) -> Cap:
    """Return the strongest claim a set of cited items can carry."""
    caps = [item_cap(item) for item in items]
    if not caps:
        return Cap("weak", "no evidence was cited")
    kinds = {cap.kind for cap in caps if cap.strength == "moderate"}
    if {"qtl", "gwas"} <= kinds and all(cap.strength != "strong" for cap in caps):
        return Cap("strong", "a narrow trait QTL and a nearby trait GWAS hit converge")
    return max(caps, key=lambda cap: LEVELS.index(cap.strength))


def capped(requested: Strength, items: list[EvidenceItem]) -> tuple[Strength, str | None]:
    """Return the allowed strength and, when it is lower than requested, why."""
    cap = max_strength(items)
    if LEVELS.index(requested) <= LEVELS.index(cap.strength):
        return requested, None
    return cap.strength, f"strength capped from {requested} to {cap.strength}: the strongest cited evidence ({cap.reason}) supports at most {cap.strength}"


def _qtl_cap(alias: str, value: dict[str, Any]) -> Cap:
    span = int(value.get("span_bp") or 0)
    if value.get("kind") == "marker" or int(value.get("n_markers_placed") or 0) < 2:
        return Cap("weak", f"{alias} is a QTL placed from a single marker")
    if value.get("wide") or span > NARROW_QTL_BP:
        return Cap("weak", f"{alias} is a QTL spanning {span / 1e6:.2f} Mb")
    if value.get("trait_match") != "ontology":
        return Cap("weak", f"{alias} is a QTL matched by keyword only")
    return Cap("moderate", f"{alias} is a narrow multi-marker ontology-matched QTL ({span / 1e6:.2f} Mb)", "qtl")
