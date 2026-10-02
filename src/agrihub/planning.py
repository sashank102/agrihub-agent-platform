"""A deterministic, differentiated dispatch plan read from the triage brief.

The orchestrator falls back to it when its model fails or runs out of
steps, and the scripted fake model uses it as its policy. Each specialist
gets the genes its domain can say something about, read from the rubric
reasons in the brief (``A`` positional, ``B`` curated gene, ``C`` ortholog,
``D`` annotation, ``E`` expression, ``G`` QTL/GWAS convergence).
Expression and network work is dispatched only when the brief's ``domains``
say the bundle has expression, co-expression, network or regulation data;
otherwise the specialist is skipped with the reasons the bundle gave.
"""

from typing import Any

from agrihub_data.availability import SPECIALIST_REQUIRES

EXPRESSION_GENES_PER_LOCUS = 3


def reason_codes(gene: dict[str, Any]) -> set[str]:
    """Return the rubric categories that gave a brief gene points."""
    return {str(reason).split(" ", 1)[0] for reason in gene.get("why") or [] if str(reason).strip()}


def unavailable_reason(brief: dict[str, Any], specialist: str) -> str | None:
    """Return why none of the domains a specialist needs can be served, or ``None`` when one can.

    Specialists with core-tier tools (and briefs without ``domains``) are never declined here.
    """
    keys = SPECIALIST_REQUIRES.get(specialist) or ()
    domains = brief.get("domains")
    if not keys or not isinstance(domains, dict):
        return None
    available = set(domains.get("available") or [])
    if available & set(keys):
        return None
    missing = domains.get("unavailable") or {}
    reasons = list(dict.fromkeys(str(missing[key]) for key in keys if key in missing))
    return "; ".join(reasons) or "its data is not in this build"


def plan_from_brief(
    brief: dict[str, Any],
    enabled: list[str],
    max_genes: int,
    trait: str,
) -> tuple[str, list[dict[str, Any]], list[dict[str, str]]]:
    """Return ``(summary, dispatches, skipped)`` for the first round."""
    loci = list(brief.get("loci") or [])
    locus_ids = [str(entry["locus_id"]) for entry in loci]
    dispatches: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    def add(specialist: str, genes: list[str], focus_loci: list[str], instructions: str, rationale: str) -> None:
        if specialist not in enabled:
            return
        genes = list(dict.fromkeys(genes))[:max_genes]
        if not genes and not focus_loci:
            skipped.append({"specialist": specialist, "reason": "no candidate has evidence in its domain"})
            return
        dispatches.append(
            {
                "specialist": specialist,
                "focus_gene_ids": genes,
                "focus_loci": focus_loci,
                "instructions": instructions,
                "rationale": rationale,
            }
        )

    nearest: list[str] = []
    overlapping: list[str] = []
    for entry in loci:
        top = sorted(entry.get("top") or [], key=lambda gene: (gene.get("dist") is None, gene.get("dist") or 0))
        nearest.extend(gene["gene_id"] for gene in top[:2])
        overlapping.extend(gene["gene_id"] for gene in top if gene.get("dist") == 0)
    add(
        "locus_variant",
        nearest,
        locus_ids,
        "Validate each locus window and state the positional relation of the genes nearest each lead SNP; flag duplicated genes in the window.",
        (f"{', '.join(overlapping[:3])} overlap a lead SNP; " if overlapping else "")
        + f"the two genes nearest each of the {len(locus_ids)} lead SNPs carry the positional case.",
    )

    genetic = [
        gene["gene_id"]
        for entry in loci
        for gene in entry.get("top") or []
        if reason_codes(gene) & {"B", "G"}
    ]
    genetic += [str(known["gene_id"]) for entry in loci for known in entry.get("known_genes") or []]
    contexts = [
        f"{entry['locus_id']}: {entry['context'].get('qtl', '')}"
        for entry in loci
        if isinstance(entry.get("context"), dict) and entry["context"].get("qtl")
    ]
    add(
        "qtl_gwas",
        genetic or [entry["top"][0]["gene_id"] for entry in loci if entry.get("top")],
        locus_ids,
        f"Check QTLs, catalog GWAS hits and curated genes for {trait} over each locus and these genes; note competing curated genes.",
        (
            f"{len(set(genetic))} top genes already have QTL/GWAS or curated-gene points"
            if genetic
            else "No top gene has QTL/GWAS points yet; the loci themselves need a genetics check"
        )
        + (f" ({contexts[0]})." if contexts else "."),
    )

    functional = [
        gene["gene_id"]
        for entry in loci
        for gene in (entry.get("top") or [])[:4]
        if reason_codes(gene) & {"C", "D"}
    ]
    add(
        "function_orthology",
        functional or [gene["gene_id"] for entry in loci for gene in (entry.get("top") or [])[:3]],
        [],
        f"Assess domains, GO and Arabidopsis ortholog phenotypes of these genes against the {trait} profile; separate own from transferred evidence.",
        f"{len(functional)} top genes score on ortholog (C) or annotation (D) evidence, which needs an orthology-quality check."
        if functional
        else "No top gene has ortholog or annotation points yet; the three best-ranked genes per locus need a function check.",
    )

    named = [
        gene["gene_id"]
        for entry in loci
        for gene in entry.get("top") or []
        if gene.get("symbol") or reason_codes(gene) & {"B", "C"}
    ]
    literate = list(dict.fromkeys([*named, *overlapping]))
    literate = literate or [entry["top"][0]["gene_id"] for entry in loci if entry.get("top")]
    add(
        "literature",
        literate[: min(max_genes, 6)],
        [],
        f"Search the literature for these genes with {trait} terms and quote passages; classify each relation.",
        (f"{len(named)} genes have a symbol, a curated record or a characterized ortholog" if named else "No top gene has a symbol yet")
        + (f"; {len(overlapping)} genes overlap a lead SNP" if overlapping else "")
        + ", so papers are worth checking.",
    )
    declined = unavailable_reason(brief, "expression_network")
    if "expression_network" in enabled and declined is not None:
        skipped.append({"specialist": "expression_network", "reason": f"no expression or network data: {declined}"})
    elif "expression_network" in enabled:
        expressed = [
            str(gene["gene_id"])
            for entry in loci
            for gene in (entry.get("top") or [])[:EXPRESSION_GENES_PER_LOCUS]
        ]
        expressed += [str(known["gene_id"]) for entry in loci for known in entry.get("known_genes") or []]
        add(
            "expression_network",
            expressed,
            [],
            f"Check expression of these genes in {trait}-relevant tissues and their tissue specificity, their co-expression "
            "and network proximity to known trait genes, and whether they are or are regulated by transcription factors.",
            f"The top {EXPRESSION_GENES_PER_LOCUS} genes of each of the {len(locus_ids)} loci"
            + (" and the curated trait genes" if any(entry.get("known_genes") for entry in loci) else "")
            + " need expression (E) and network (F) evidence, which harvest scores only in part.",
        )
    summary = (
        f"Dispatch {len(dispatches)} specialists on different genes of {len(locus_ids)} loci: "
        + ", ".join(f"{item['specialist']} ({len(item['focus_gene_ids'])} genes)" for item in dispatches)
        + "."
    )
    return summary, dispatches, skipped


def followup_from_delta(delta: dict[str, Any], max_genes: int, trait: str) -> tuple[str, list[dict[str, Any]]] | None:
    """Return a narrow literature follow-up on genes that gained support without literature, or ``None``."""
    covered = {str(gene) for gene in delta.get("literature_covered") or []}
    gained = [str(item["gene_id"]) for item in delta.get("gained_support") or [] if str(item["gene_id"]) not in covered]
    if not gained:
        return None
    genes = gained[: min(max_genes, 3)]
    return (
        f"Follow up on {len(genes)} genes that gained support in round 1 but have no literature check.",
        [
            {
                "specialist": "literature",
                "focus_gene_ids": genes,
                "focus_loci": [],
                "instructions": f"Look for publications linking these genes to {trait}; quote passages and classify them.",
                "rationale": f"{', '.join(genes)} gained supporting findings in round 1 and were not covered by literature.",
            }
        ],
    )
