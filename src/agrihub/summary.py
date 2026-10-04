"""The written executive summary: facts pack, draft validation, template and Markdown.

The report writer reads a compact facts pack and drafts a summary with one
``write_summary`` call (:class:`WriterDraft`). :func:`validate_draft` keeps
only genes and loci of the study and citations that resolve, and fixes each
candidate's confidence from its tier, so the model can neither invent a gene
nor overstate one. :func:`template_draft` writes the same structure from the
facts alone; it is used when no model is configured, the model fails, or
validation leaves no usable summary.
"""

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from agrihub import citations
from agrihub.state import (
    TIER_CONFIDENCE,
    Finding,
    RankedCandidate,
    Report,
    ReportSummary,
    SummaryCandidate,
    SummaryFinding,
    SummaryLocus,
)

FACTS_MARKER = "Facts pack JSON:"
MAX_CANDIDATES = 5
MAX_FINDINGS = 6
MAX_CAVEATS = 4
MAX_NEXT_STEPS = 4
MAX_EVIDENCE_PER_GENE = 6
QUOTE_CHARS = 180
CATEGORY_LABELS = {
    "A": "position",
    "B": "curated trait gene",
    "C": "ortholog function",
    "D": "annotation",
    "E": "expression",
    "F": "network",
    "G": "prior QTL/GWAS",
}
_REASON = re.compile(r"^([A-G]): (.+)$")


class CandidateDraft(BaseModel):
    """One top candidate's paragraph."""

    gene_id: str = Field(description="A gene id from the facts pack candidates.")
    narrative: str = Field(description="Three or four sentences with inline citations such as [E12].")


class LocusDraft(BaseModel):
    """One locus's sentence or two."""

    locus_id: str = Field(description="A locus id from the facts pack, such as L1.")
    narrative: str = Field(description="Which gene leads the locus, its competitors and how decisive the lead is.")


class WriterDraft(BaseModel):
    """Write the executive summary of the finished study, bottom line first."""

    bottom_line: str = Field(description="Two or three sentences naming the strongest candidates overall, with citations.")
    key_findings: list[str] = Field(
        default_factory=list,
        description="Four to six complete findings, each starting with a **bold lead clause** and carrying citations.",
    )
    candidates: list[CandidateDraft] = Field(default_factory=list, description="One paragraph per top candidate, in rank order.")
    loci: list[LocusDraft] = Field(default_factory=list, description="One entry per locus.")
    caveats: list[str] = Field(default_factory=list, description="The three or four limitations that matter most.")
    next_steps: list[str] = Field(default_factory=list, description="Two to four concrete validations tied to genes.")


def gene_label(gene_id: str, symbol: str | None) -> str:
    """Return ``gene_id (symbol)``, or the id alone."""
    return f"{gene_id} ({symbol})" if symbol else gene_id


def position_text(item: RankedCandidate) -> str:
    """Return ``chrom:start-end (strand)``, or nothing when the position is unknown."""
    if item.chrom is None or item.start is None or item.end is None:
        return ""
    return f"{item.chrom}:{item.start}-{item.end} ({item.strand})"


def distance_text(item: RankedCandidate) -> str:
    """Return how far the gene is from its nearest study SNP."""
    if item.distance_bp is None:
        return ""
    snp = item.nearest_snp or item.lead_snp or "SNP"
    if item.overlaps_snp:
        return f"overlaps {snp}"
    return f"{item.distance_bp / 1000:.1f} kb from {snp}"


def reason_text(reason: str) -> str:
    """Turn a rubric reason such as ``G: ...`` into ``prior QTL/GWAS: ...``."""
    match = _REASON.match(reason)
    if not match:
        return reason
    return f"{CATEGORY_LABELS.get(match.group(1), match.group(1))}: {match.group(2)}"


def build_facts(report: Report, findings: list[Finding], alias_of: dict[str, str]) -> dict[str, Any]:
    """Return the facts pack: everything the writer may say, and the citations it may use."""
    quotes = {ref.alias: ref for ref in report.citations}
    ranked = sorted(report.candidates or report.candidates_full, key=lambda item: item.rank)
    top = ranked[:MAX_CANDIDATES]
    by_target: dict[str, list[Finding]] = {}
    for finding in findings:
        by_target.setdefault(finding.target, []).append(finding)
    allowed: set[str] = set()
    candidates = []
    for item in top:
        aliases = [alias_of.get(key, key) for key in item.evidence_ids if alias_of.get(key, key) in quotes][:MAX_EVIDENCE_PER_GENE]
        allowed.update(aliases)
        gene_findings = []
        for finding in by_target.get(item.gene_id, []):
            cites = [alias_of.get(key, key) for key in finding.evidence_ids]
            cites = [alias for alias in cites if alias.startswith("E")]
            allowed.update(cites)
            gene_findings.append({"claim": finding.claim, "stance": finding.stance, "strength": finding.strength, "citations": cites})
        candidates.append(
            {
                "rank": item.rank,
                "gene_id": item.gene_id,
                "symbol": item.symbol,
                "locus_id": item.locus_id,
                "rank_in_locus": item.rank_in_locus,
                "position": position_text(item),
                "distance": distance_text(item),
                "defline": item.defline[:160],
                "tier": item.tier,
                "confidence": TIER_CONFIDENCE[item.tier],
                "score": round(item.score, 2),
                "share_of_locus": round(item.share_of_locus or 0.0, 2),
                "points": {code: round(value, 2) for code, value in item.category_points.items() if value},
                "reasons": [reason_text(reason) for reason in item.reasons],
                "stability": item.stability,
                "verifier_status": item.verifier_status,
                "evidence": [
                    {
                        "cite": alias,
                        "category": quotes[alias].category,
                        "source": quotes[alias].source_db,
                        "quote": (quotes[alias].quote or "")[:QUOTE_CHARS],
                    }
                    for alias in aliases
                ],
                "findings": gene_findings,
            }
        )
    loci = []
    for locus in report.loci:
        leaders = sorted(
            (item for item in report.candidates_full if item.locus_id == locus.locus_id),
            key=lambda item: item.rank_in_locus or 10**6,
        )[:3]
        loci.append(
            {
                "locus_id": locus.locus_id,
                "region": f"{locus.chrom}:{locus.start}-{locus.end}",
                "lead_snp": locus.lead_snp,
                "n_genes": locus.n_genes,
                "leaders": [
                    {
                        "gene_id": item.gene_id,
                        "symbol": item.symbol,
                        "tier": item.tier,
                        "score": round(item.score, 2),
                        "share_of_locus": round(item.share_of_locus or 0.0, 2),
                        "stability": item.stability,
                    }
                    for item in leaders
                ],
            }
        )
    sources = [{"cite": f"source:{source.source_id}", "name": source.name} for source in report.sources]
    allowed.update(source["cite"] for source in sources)
    return {
        "study": {
            "title": report.title,
            "species": report.species,
            "assembly": report.assembly,
            "trait": report.trait,
            "n_loci": len(report.loci),
            "n_genes": len(report.candidates_full),
            "evidence_items": report.evidence_count,
            "findings": report.finding_count,
        },
        "candidates": candidates,
        "loci": loci,
        "verification": _verification_counts(report),
        "limitations": report.limitations,
        "validations": report.suggested_validations,
        "sources": sources,
        "allowed_citations": sorted(allowed, key=_citation_order),
    }


def briefing(facts: dict[str, Any]) -> str:
    """Return the writer's user message: the facts pack and the allowed citation markers."""
    allowed = " ".join(f"[{token}]" for token in facts.get("allowed_citations") or [])
    return "\n".join(
        [
            FACTS_MARKER,
            json.dumps(facts, ensure_ascii=False, separators=(",", ":")),
            f"Allowed citations: {allowed or 'none'}",
            "Write the summary now with one write_summary call.",
        ]
    )


def validate_draft(
    draft: WriterDraft,
    report: Report,
    known: set[str],
    written_by: str,
) -> tuple[ReportSummary | None, list[str]]:
    """Keep the study's genes and loci and the citations that resolve; fix confidence from the tier.

    Returns ``(None, orphans)`` when nothing usable is left: no bottom line
    or no valid candidate paragraph.
    """
    orphans: list[str] = []

    def clean(text: str) -> str:
        cleaned, dropped = citations.validate_citations(text or "", known)
        orphans.extend(dropped)
        return cleaned.strip()

    bottom_line = clean(draft.bottom_line)
    ranked = {item.gene_id: item for item in report.candidates_full}
    candidates: list[SummaryCandidate] = []
    for entry in draft.candidates:
        item = ranked.get(entry.gene_id)
        narrative = clean(entry.narrative)
        if item is None or not narrative or any(existing.gene_id == item.gene_id for existing in candidates):
            continue
        candidates.append(
            SummaryCandidate(
                gene_id=item.gene_id,
                narrative=narrative,
                citations=citations.citation_tokens(narrative),
                symbol=item.symbol,
                locus_id=item.locus_id,
                tier=item.tier,
                confidence=TIER_CONFIDENCE[item.tier],
            )
        )
        if len(candidates) == MAX_CANDIDATES:
            break
    if not bottom_line or not candidates:
        return None, list(dict.fromkeys(orphans))
    locus_ids = {locus.locus_id for locus in report.loci}
    loci: list[SummaryLocus] = []
    for entry in draft.loci:
        narrative = clean(entry.narrative)
        if entry.locus_id in locus_ids and narrative and all(existing.locus_id != entry.locus_id for existing in loci):
            loci.append(SummaryLocus(locus_id=entry.locus_id, narrative=narrative, citations=citations.citation_tokens(narrative)))
    findings = [
        SummaryFinding(statement=text, citations=citations.citation_tokens(text))
        for text in (clean(statement) for statement in draft.key_findings)
        if text
    ][:MAX_FINDINGS]
    summary = ReportSummary(
        bottom_line=bottom_line,
        key_findings=findings,
        candidates=candidates,
        loci=sorted(loci, key=lambda item: _locus_order(item.locus_id)),
        caveats=[text for text in (clean(item) for item in draft.caveats) if text][:MAX_CAVEATS],
        next_steps=[text for text in (clean(item) for item in draft.next_steps) if text][:MAX_NEXT_STEPS],
        written_by=written_by,
    )
    return summary, list(dict.fromkeys(orphans))


def template_draft(facts: dict[str, Any]) -> WriterDraft:
    """Write the summary from the facts pack alone, in the same structure the writer model fills."""
    study = facts.get("study") or {}
    top = list(facts.get("candidates") or [])
    loci = list(facts.get("loci") or [])
    trait = study.get("trait") or "the trait"
    scope = f"{study.get('n_genes', 0)} genes in {study.get('n_loci', 0)} loci"
    if not top:
        return WriterDraft(bottom_line=f"No candidate genes were ranked among {scope}.")
    functional = [item for item in top if item["tier"] != "T4"]
    if functional:
        named = "; ".join(f"{_tagged(item)}, {_main_reason(item)}{_cites(item)}" for item in functional[:3])
        bottom_line = f"Of {scope}, the strongest {trait} candidates are {named}."
    else:
        named = ", ".join(f"{_label(item)} ({item['distance'] or 'in the window'})" for item in top[:3])
        bottom_line = (
            f"None of the {scope} has evidence beyond position in this build, so the ranking is positional only. "
            f"The leading genes are {named}."
        )
    leader = top[0]
    findings = [
        f"**{_label(leader)} leads the study.** It ranks first overall with a score of {leader['score']:g} "
        f"({leader['tier']}, confidence {leader['confidence']}); {_main_reason(leader)}{_cites(leader)}.",
    ]
    if functional:
        findings.append(
            f"**{len(functional)} of {len(top)} highlighted candidates have support beyond position.** "
            + "; ".join(f"{_label(item)}: {_main_reason(item)}{_cites(item)}" for item in functional[:3])
            + "."
        )
    else:
        findings.append(
            "**All highlighted candidates are positional only.** No curated trait gene, ortholog phenotype, specific "
            "expression or network signal was found for them in this build."
        )
    associated = [item for item in top if item["points"].get("G")]
    if associated:
        findings.append(
            "**Prior QTL or GWAS evidence overlaps "
            + ", ".join(_label(item) for item in associated)
            + ".** "
            + "; ".join(f"{_label(item)}: {_category_reason(item, 'G')}{_cites(item, 'association')}" for item in associated[:3])
            + "."
        )
    unstable = [item for item in top if item.get("stability") and "every" not in str(item["stability"])]
    if top and not unstable:
        findings.append("**Rankings are stable across window sizes.** Every highlighted candidate stays in the top genes of its locus at each tested window.")
    elif unstable:
        findings.append(
            "**Some rankings depend on the window size.** "
            + "; ".join(f"{_label(item)}: {item['stability']}" for item in unstable[:3])
            + "."
        )
    checked = facts.get("verification") or {}
    if checked.get("total"):
        findings.append(
            f"**The verifier independently confirmed {checked.get('verified', 0)} of {checked['total']} checked claims**; "
            f"{checked.get('unverified', 0)} remain unverified and {checked.get('contradicted', 0)} were contradicted."
        )
    findings.append(
        f"**The study covers {scope}** with {study.get('evidence_items', 0)} evidence items and "
        f"{study.get('findings', 0)} specialist findings."
    )
    candidates = [CandidateDraft(gene_id=item["gene_id"], narrative=_candidate_paragraph(item)) for item in top]
    locus_drafts = [LocusDraft(locus_id=locus["locus_id"], narrative=_locus_sentence(locus)) for locus in loci if locus.get("leaders")]
    return WriterDraft(
        bottom_line=bottom_line,
        key_findings=findings[:MAX_FINDINGS],
        candidates=candidates,
        loci=locus_drafts,
        caveats=list(facts.get("limitations") or [])[:3],
        next_steps=list(facts.get("validations") or [])[:MAX_NEXT_STEPS],
    )


def render_markdown(summary: ReportSummary, report: Report) -> str:
    """Render the summary as Markdown with inline citations, bottom line first."""
    lines = [
        f"# {report.title}",
        "",
        f"*{report.species}, assembly {report.assembly}, trait {report.trait}; {len(report.loci)} loci, "
        f"{len(report.candidates_full)} genes scored.*",
        "",
        "## Bottom line",
        "",
        summary.bottom_line,
    ]
    if summary.key_findings:
        lines += ["", "## Key findings at a glance", ""]
        lines += [f"{index}. {item.statement}" for index, item in enumerate(summary.key_findings, start=1)]
    if summary.candidates:
        lines += ["", "## Top candidates"]
        for item in summary.candidates:
            heading = gene_label(item.gene_id, item.symbol)
            badge = f"{item.tier}, {item.confidence}" if item.tier else ""
            lines += ["", f"### {heading}" + (f" · {badge}" if badge else ""), "", item.narrative]
    if summary.loci:
        lines += ["", "## Locus by locus", ""]
        lines += [f"- **{item.locus_id}.** {item.narrative}" for item in summary.loci]
    if summary.caveats:
        lines += ["", "## Confidence and caveats", ""]
        lines += [f"- {item}" for item in summary.caveats]
    if summary.next_steps:
        lines += ["", "## Recommended next steps", ""]
        lines += [f"- {item}" for item in summary.next_steps]
    return "\n".join(lines)


def _verification_counts(report: Report) -> dict[str, int]:
    counts = {"total": len(report.verification), "verified": 0, "unverified": 0, "contradicted": 0}
    for verdict in report.verification:
        counts[verdict.status] += 1
    return counts


def _label(item: dict[str, Any]) -> str:
    return gene_label(str(item["gene_id"]), item.get("symbol"))


def _tagged(item: dict[str, Any]) -> str:
    """Return ``gene (symbol; T1, strong)`` without nesting parentheses."""
    tag = f"{item['tier']}, {item['confidence']}"
    symbol = item.get("symbol")
    return f"{item['gene_id']} ({symbol}; {tag})" if symbol else f"{item['gene_id']} ({tag})"


def _ordinal(number: int) -> str:
    words = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}
    return words.get(number, f"{number}th")


def _percent(value: float) -> str:
    text = f"{value:.0%}"
    return ("an " if text.startswith(("8", "11", "18")) else "a ") + text


def _main_reason(item: dict[str, Any]) -> str:
    reasons = [reason for reason in item.get("reasons") or [] if not reason.startswith(CATEGORY_LABELS["A"])]
    return (reasons or item.get("reasons") or [item.get("distance") or "positional evidence"])[0]


def _category_reason(item: dict[str, Any], code: str) -> str:
    label = CATEGORY_LABELS[code]
    return next((reason for reason in item.get("reasons") or [] if reason.startswith(label)), label)


def _cites(item: dict[str, Any], category: str | None = None) -> str:
    aliases = [entry["cite"] for entry in item.get("evidence") or [] if category is None or entry.get("category") == category][:2]
    return " " + "".join(f"[{alias}]" for alias in aliases) if aliases else ""


def _candidate_paragraph(item: dict[str, Any]) -> str:
    where = f"{_label(item)} lies in {item['locus_id']}"
    if item.get("distance"):
        where += f", {item['distance']}"
    sentences = [
        f"{where}, and ranks {_ordinal(int(item.get('rank_in_locus') or item['rank']))} in its locus with "
        f"{_percent(float(item.get('share_of_locus') or 0))} share of the locus score."
    ]
    reasons = [reason for reason in item.get("reasons") or [] if not reason.startswith(CATEGORY_LABELS["A"])][:3]
    if reasons:
        sentences.append("Evidence: " + "; ".join(reasons) + _cites(item) + ".")
    supporting = [finding for finding in item.get("findings") or [] if finding.get("stance") != "neutral"][:1]
    for finding in supporting:
        cites = "".join(f"[{alias}]" for alias in (finding.get("citations") or [])[:2])
        sentences.append(f"A specialist found that {finding['claim'].rstrip('.')}" + (f" {cites}" if cites else "") + ".")
    sentences.append(f"Verifier status: {item.get('verifier_status') or 'unchecked'}. Confidence: {item['confidence']}.")
    return " ".join(sentences)


def _locus_sentence(locus: dict[str, Any]) -> str:
    leaders = locus["leaders"]
    first = leaders[0]
    rivals = ", ".join(_label(item) for item in leaders[1:])
    share = float(first.get("share_of_locus") or 0)
    decisive = "a clear lead" if share >= 0.5 else "a narrow lead" if share >= 0.2 else "no decisive lead"
    text = (
        f"{_label(first)} leads {locus['locus_id']} ({locus['region']}, lead SNP {locus['lead_snp']}, "
        f"{locus['n_genes']} genes) with {decisive} ({share:.0%} of the locus score)"
    )
    return text + (f", ahead of {rivals}." if rivals else ".")


def _citation_order(token: str) -> tuple[int, int, str]:
    if token.startswith("E") and token[1:].isdigit():
        return (0, int(token[1:]), "")
    return (1, 0, token)


def _locus_order(locus_id: str) -> tuple[int, str]:
    digits = locus_id.lstrip("L")
    return (int(digits), "") if digits.isdigit() else (10**6, locus_id)
