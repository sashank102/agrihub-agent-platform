"""Write the structured report and its executive summary, validate citations, and export the ledger.

The report is assembled from the study, the full ranking and the verifier
marks; its tables go to ``details_markdown``. The report writer agent then
drafts the executive summary from a facts pack (:mod:`agrihub.summary`):
only the study's genes, loci and existing citations survive validation, the
confidence of each gene follows its tier, and a deterministic template
writes the summary when no model is available or the draft is unusable.
The evidence snapshot and a compact run trace are stored beside the report.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool

from agrihub import citations, events, models, summary
from agrihub.configuration import StudyConfiguration, run_id_from_config
from agrihub.evidence_store import EvidenceStore
from agrihub.nodes.harvest import bundle_sources
from agrihub.prompts import PromptContext, render
from agrihub.prompts.writer import WRITER
from agrihub.scoring import load_rubric
from agrihub.state import (
    ClaimVerdict,
    EvidenceRef,
    Locus,
    RankedCandidate,
    Report,
    ReportSummary,
    SourceRef,
    StudyState,
    StudyWarning,
)
from agrihub_data.bundle import BundleMissingError, open_bundle
from agrihub_data.registry import UnknownSpeciesError, load_species

WRITE_SUMMARY = StructuredTool.from_function(
    func=lambda **_: "recorded",
    name="write_summary",
    description=summary.WriterDraft.__doc__ or "Write the executive summary.",
    args_schema=summary.WriterDraft,
)

ArtifactSink = Callable[..., Awaitable[str | None]]
"""Persist one artifact and return its id.

Called as ``sink(config, kind=..., title=..., content=..., metadata=...)``.
"""


def make_writer(artifact_sink: ArtifactSink | None = None) -> Callable[..., Any]:
    """Return the writer node bound to an optional artifact sink."""

    async def writer(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
        events.phase("reporting")
        run_id = run_id_from_config(config)
        store = EvidenceStore.for_run(run_id)
        report, snapshot, snapshot_path, facts, known = await asyncio.to_thread(_prepare, state, store)
        report = await _summarize(report, facts, known, config)
        trace = _run_trace(state, report)
        report_id = None
        snapshot_id = None
        trace_id = None
        if artifact_sink is not None:
            report_id = await artifact_sink(
                config,
                kind="report",
                title=report.title,
                content=report.model_dump(mode="json"),
                metadata={"schema": "agrihub.report/v1"},
            )
            snapshot_id = await artifact_sink(
                config,
                kind="evidence_snapshot",
                title="Evidence ledger",
                content=snapshot,
                metadata={
                    "evidence": len(snapshot["evidence"]),
                    "findings": len(snapshot["findings"]),
                    "path": str(snapshot_path),
                },
            )
            trace_id = await artifact_sink(
                config,
                kind="run_trace",
                title="Run trace",
                content=trace,
                metadata={"schema": "agrihub.run-trace/v1"},
            )
        events.artifact_created("evidence_snapshot", title="Evidence ledger", artifact_id=snapshot_id)
        events.artifact_created("run_trace", title="Run trace", artifact_id=trace_id, agent=events.WRITER)
        events.artifact_created("report", title=report.title, artifact_id=report_id, agent=events.WRITER)
        store.close()
        events.phase("reporting", "completed")
        return {
            "report": report.model_dump(mode="json"),
            "messages": [AIMessage(content=report.markdown)],
            "run_status": "completed",
            "study_run_id": run_id,
            "evidence_snapshot_id": snapshot_id,
        }

    return writer


def _prepare(
    state: StudyState,
    store: EvidenceStore,
) -> tuple[Report, dict[str, Any], Path, dict[str, Any], set[str]]:
    study = state.get("study") or {}
    try:
        sources = [source for source, _ in bundle_sources(open_bundle(str(study.get("species") or "")))]
    except BundleMissingError:
        sources = []
    report = _report(state, store, sources)
    stored = store.query()
    alias_of = {str(item.evidence_id): str(item.alias) for item in stored if item.evidence_id and item.alias}
    known = {str(item.alias) for item in stored if item.alias} | {source.source_id for source in report.sources}
    facts = summary.build_facts(report, store.findings(), alias_of)
    return report, store.snapshot(), store.export(), facts, known


async def _summarize(report: Report, facts: dict[str, Any], known: set[str], config: RunnableConfig) -> Report:
    """Have the writer model draft the summary; fall back to the template when the draft is unusable."""
    settings = StudyConfiguration.from_runnable_config(config)
    started = time.monotonic()
    events.agent_started(
        events.WRITER,
        focus={"candidates": len(facts.get("candidates") or []), "instructions": "Write the executive summary, bottom line first."},
        max_steps=1,
    )
    events.agent_step(events.WRITER, 1, 1, "Write the executive summary")
    written: ReportSummary | None = None
    orphans: list[str] = []
    problem = ""
    try:
        model = models.tool_model(
            settings.writer_model,
            [WRITE_SUMMARY],
            max_tokens=settings.model_max_tokens,
            max_retries=settings.model_max_retries,
            prompt_caching=settings.prompt_caching,
        )
        context = PromptContext(species=report.species, assembly=report.assembly, trait=report.trait, max_steps=1)
        response = await model.ainvoke(
            [SystemMessage(content=render(WRITER, context)), HumanMessage(content=summary.briefing(facts))],
            config,
        )
        metadata = getattr(response, "usage_metadata", None) or {}
        events.agent_usage(
            events.WRITER,
            model=settings.writer_model,
            input_tokens=int(metadata.get("input_tokens") or 0),
            output_tokens=int(metadata.get("output_tokens") or 0),
            cached_input_tokens=models.cached_input_tokens(response),
        )
        call = next((item for item in getattr(response, "tool_calls", None) or [] if item["name"] == WRITE_SUMMARY.name), None)
        if call is None:
            problem = "the writer model did not call write_summary"
        else:
            draft = summary.WriterDraft.model_validate(call["args"])
            written, orphans = summary.validate_draft(draft, report, known, settings.writer_model)
            if written is None:
                problem = "the writer's draft named no candidate of this study"
    except Exception as exc:  # noqa: BLE001
        problem = f"the writer model failed ({type(exc).__name__})"
    limitations = list(report.limitations)
    if written is None:
        written, _ = summary.validate_draft(summary.template_draft(facts), report, known, "template")
        limitations.append(f"The summary was written from the deterministic template because {problem}.")
    if orphans:
        limitations.append("Citations removed from the written summary because they do not resolve: " + ", ".join(orphans) + ".")
    if written is None:
        written = ReportSummary(bottom_line=f"No candidate genes were ranked for {report.trait}.", written_by="template")
    events.agent_completed(
        events.WRITER,
        summary=f"Summary written by {written.written_by}: {len(written.key_findings)} key findings, {len(written.candidates)} candidates.",
        findings=0,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    drafted = report.model_copy(update={"limitations": limitations, "summary": written})
    return drafted.model_copy(update={"markdown": summary.render_markdown(written, drafted)})


def _gene_linkouts(species: str) -> list[dict[str, str]]:
    """Return the species' gene page templates (``{gene_id}`` placeholders), or none for an unknown species."""
    try:
        registry = load_species(species)
    except (UnknownSpeciesError, FileNotFoundError, ValueError):
        return []
    return [{"name": item.name, "template": item.template} for item in registry.linkouts if item.kind == "gene"]


def _report(state: StudyState, store: EvidenceStore, sources: list[SourceRef]) -> Report:
    study = state.get("study") or {}
    scoring = state.get("scoring") or {}
    loci = [Locus.model_validate(raw) for raw in state.get("loci") or []]
    ranked = [RankedCandidate.model_validate(raw) for raw in state.get("ranking") or []]
    shortlist = [item for item in ranked if item.shortlist] or ranked
    warnings = [StudyWarning.model_validate(raw) for raw in state.get("warnings") or []]
    verification = [ClaimVerdict.model_validate(raw) for raw in scoring.get("verification") or []]
    sensitivity = scoring.get("window_sensitivity") or {}
    stability = {
        "flanks_bp": sensitivity.get("flanks_bp") or [],
        "top_k": sensitivity.get("top_k"),
        "loci": sensitivity.get("loci") or [],
    }
    title = f"{study.get('trait_text')} candidate genes in {study.get('species')}"
    limitations = _limitations(study, scoring, warnings, list(state.get("specialist_results") or []))
    report = Report(
        title=title,
        species=str(study.get("species") or ""),
        assembly=str(study.get("assembly") or ""),
        trait=str(study.get("trait_text") or ""),
        mode=study.get("mode") or "snps",
        provenance={
            "window": study.get("window") or {},
            "model": state.get("model_result") or {},
            "rounds": int(state.get("round") or 0),
            "rubric_version": scoring.get("rubric_version"),
            "score_explanations_ref": scoring.get("explanations_ref"),
            "evidence_matrix_ref": (state.get("triage_brief") or {}).get("matrix_ref"),
            "tiers": scoring.get("tiers") or {},
            "gene_linkouts": _gene_linkouts(str(study.get("species") or "")),
        },
        loci=loci,
        candidates=shortlist,
        candidates_full=ranked,
        verification=verification,
        stability=stability,
        warnings=warnings,
        limitations=limitations,
        suggested_validations=_validations(ranked),
        sources=_annotate_sources(sources),
        evidence_count=store.count(),
        finding_count=len(store.findings()),
    )
    stored = store.query()
    alias_of = {str(item.evidence_id): item.alias for item in stored if item.evidence_id and item.alias}
    known = {item.alias for item in stored if item.alias}
    known |= {source.source_id for source in report.sources}
    markdown = _markdown(title, study, loci, shortlist, warnings, limitations, verification, state.get("model_result") or {})
    cited_lines = []
    for candidate in shortlist[:5]:
        aliases = [alias_of[key] for key in candidate.evidence_ids if key in alias_of][:2]
        if aliases:
            cited_lines.append(f"{candidate.gene_id} ({candidate.tier}) is supported by " + " ".join(f"[{alias}]" for alias in aliases) + ".")
    if cited_lines:
        markdown += "\n\n## Cited evidence\n\n" + "\n".join(f"- {line}" for line in cited_lines)
    markdown, orphans = citations.validate_citations(markdown, known)
    if orphans:
        note = "Citations removed because they do not resolve: " + ", ".join(orphans) + "."
        limitations = [*limitations, note]
        markdown = _markdown(title, study, loci, shortlist, warnings, limitations, verification, state.get("model_result") or {})
        markdown, _ = citations.validate_citations(markdown, known)
    return report.model_copy(
        update={
            "limitations": limitations,
            "details_markdown": markdown,
            "citations": _citation_index(stored, shortlist, verification),
        }
    )


def _limitations(
    study: dict[str, Any],
    scoring: dict[str, Any],
    warnings: list[StudyWarning],
    specialist_results: list[dict[str, Any]] | None = None,
) -> list[str]:
    rubric = load_rubric()
    known = scoring.get("known_gene_records") or {}
    categories = set(scoring.get("categories") or [code for code, spec in rubric.categories.items() if spec.available])
    domains = scoring.get("domains") or {}
    unavailable = domains.get("unavailable") or {}
    missing = [f"{code} ({spec.name.lower()})" for code, spec in rubric.categories.items() if code not in categories and code != "A"]
    window = study.get("window") or {}
    items = [
        (
            f"Known-gene coverage is thin: the bundle has {known.get('total', 0)} curated LIS trait genes "
            f"({known.get('canonical', 0)} on the canonical assembly) because the SoyBase gene-symbol registry "
            "could not be downloaded. Tier T1 needs such a record, so T1 is rare, and a gene without one is "
            "not evidence against it."
        ),
    ]
    if missing:
        items.append(
            f"Categories {', '.join(missing)} have no data in this bundle; they score 0 for every gene "
            "and are reported as not available, not as negative evidence."
        )
    if "A" in categories:
        items.append(
            "Category A uses LD r2 with the lead SNP where it was computed and distance decay elsewhere, plus bonuses for "
            "a HIGH/MODERATE consequence (needs REF/ALT alleles) and for UTR, splice, upstream, TFBS or conserved-element hits."
        )
    else:
        items.append(f"Category A uses distance only; {rubric.positional.not_available}.")
    if window.get("mode") == "ld":
        items.append(
            f"Loci use LD windows (r2 >= {window.get('r2', 0.2)}) where the genotype panel covers the lead SNP and fixed "
            f"±{int(window.get('flank_bp') or 0) // 1000} kb windows elsewhere; window sensitivity at 50/100/250 kb is reported per candidate."
        )
    else:
        items.append(
            f"Loci use fixed ±{int(window.get('flank_bp') or 0) // 1000} kb windows"
            + ("; LD windows are available for this species" if "ld" in (domains.get("available") or []) else "")
            + ". Window sensitivity at 50/100/250 kb is reported per candidate."
        )
    gaps = [f"{key.replace('_', ' ')} ({reason})" for key, reason in unavailable.items() if key not in {"cross_species_convergence", "protein_records", "gene_family", "rice_orthologs"}]
    if gaps:
        items.append("Not available in this build: " + "; ".join(gaps) + ".")
    items.append(
        "Supporting findings change scores only through the evidence they cite, and that evidence is counted once. "
        "Conflicting findings are listed on the candidate and do not add points. The verifier re-checked the top "
        "claims against an independent source and did not add claims."
    )
    missing_done = [
        str(row.get("specialist") or row.get("agent_id"))
        for row in (specialist_results or [])
        if row.get("missing_specialist_done")
    ]
    if missing_done:
        items.append("Lanes that ended without a specialist_done tool call: " + ", ".join(missing_done) + ".")
    if study.get("lifted_from_assembly"):
        items.append(
            f"The SNPs were given on {study['lifted_from_assembly']} and lifted to {study.get('assembly')} through "
            "one-to-one pangene gene anchors; each SNP keeps its original position, the method and a confidence. "
            "Confirm the source assembly with whoever produced the SNPs."
        )
    if scoring.get("skipped_steps"):
        items.append(
            "Orthology, TAIR and curated-gene evidence are keyed to the canonical assembly and were skipped for "
            f"{study.get('assembly')}: {', '.join(scoring['skipped_steps'])}."
        )
    if warnings:
        items.append(f"{len(warnings)} input warnings: " + "; ".join(warning.message for warning in warnings[:3]))
    return items


def _markdown(
    title: str,
    study: dict[str, Any],
    loci: list[Locus],
    candidates: list[RankedCandidate],
    warnings: list[StudyWarning],
    limitations: list[str],
    verification: list[ClaimVerdict] | None = None,
    model_result: dict[str, Any] | None = None,
) -> str:
    lines = [
        f"# {title}",
        "",
        f"Assembly {study.get('assembly')}; {len(loci)} loci; {len(candidates)} shortlisted candidates. "
        "Scores come from the deterministic rubric. Supporting findings add only the evidence they cite.",
        "",
        "## Study and assembly",
        "",
        f"Species {study.get('species')}, assembly {study.get('assembly')}, trait {study.get('trait_text')}.",
        "",
        *_model_lines(model_result or {}),
        "## Loci",
        "",
        "| Locus | Region | Lead SNP | SNPs | Genes |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {locus.locus_id} | {locus.chrom}:{locus.start}-{locus.end} | {locus.lead_snp} | "
        f"{len(locus.snp_positions) or 1} | {locus.n_genes}{' (capped)' if locus.genes_capped else ''} |"
        for locus in loci
    )
    for locus in loci:
        rows = sorted(
            (item for item in candidates if item.locus_id == locus.locus_id),
            key=lambda item: item.rank_in_locus or 0,
        )
        if not rows:
            continue
        lines.extend(
            [
                "",
                f"## {locus.locus_id} candidates",
                "",
                "| # | Gene | Position | Distance | Tier | Score | Share | Evidence | Findings | Stability |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        lines.extend(
            f"| {item.rank_in_locus} | {item.gene_id}{f' ({item.symbol})' if item.symbol else ''} | "
            f"{summary.position_text(item)} | {summary.distance_text(item)} | {item.tier} | "
            f"{item.score:g} | {item.share_of_locus or 0:.2f} | {'; '.join(item.reasons[:3])} | {_findings(item)} | {item.stability or ''} |"
            for item in rows
        )
    if warnings:
        lines.extend(["", "## Input warnings", ""])
        lines.extend(f"- {warning.message}" for warning in warnings)
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in limitations)
    return "\n".join(lines)


def _model_lines(model_result: dict[str, Any]) -> list[str]:
    """Describe the model step of a trait study: which models proposed the SNPs and what their scores mean."""
    runs = model_result.get("runs") or []
    if not runs:
        return []
    lines = ["## Model step", "", str(model_result.get("rationale") or ""), "", "| Model | Dataset | Score type | SNPs placed | Note |", "| --- | --- | --- | --- | --- |"]
    for run in runs:
        lifted = f"; lifted from {run['assembly']} to {run['lifted_to']}" if run.get("lifted_to") else ""
        lines.append(
            f"| {run.get('name')} | {run.get('dataset') or ''} | {run.get('score_type')} | "
            f"{run.get('placed')} of {run.get('proposed')} | {run.get('label')}{lifted} |"
        )
    lines.extend(["", "Scores of different types are not compared or combined.", ""])
    return lines


def _citation_index(
    stored: list[Any],
    shortlist: list[RankedCandidate],
    verification: list[ClaimVerdict],
) -> list[EvidenceRef]:
    """Return quotes for evidence the shortlist cites, with the verifier mark when one exists."""
    wanted = {key for candidate in shortlist for key in candidate.evidence_ids}
    status: dict[str, str] = {}
    for verdict in verification:
        for evidence_id in verdict.evidence_ids:
            status.setdefault(evidence_id, verdict.status)
    refs = []
    for item in stored:
        alias = item.alias
        evidence_id = str(item.evidence_id or "")
        if not alias or (evidence_id not in wanted and alias not in wanted):
            continue
        refs.append(
            EvidenceRef(
                alias=alias,
                evidence_id=evidence_id,
                gene_id=item.gene_id,
                source_db=item.source_db,
                category=item.category,
                subtype=item.subtype,
                quote=item.quote,
                verifier_status=status.get(alias) or status.get(evidence_id),
            )
        )
    return refs


def _validations(candidates: list[RankedCandidate]) -> list[str]:
    """Suggest experiments from the tiers and evidence that are actually present."""
    tiers = {item.tier for item in candidates}
    items = ["Haplotype analysis of the shortlisted candidates in the association panel."]
    if any(item.category_points.get("E", 0) > 0 for item in candidates):
        items.append("qRT-PCR of expression-supported candidates in lines with extreme phenotypes.")
    if tiers & {"T1", "T2"}:
        items.append("Mutant or CRISPR lines for tier T1 and T2 candidates that already have functional evidence.")
    else:
        items.append("Mutant or transgenic validation once a candidate reaches tier T1 or T2.")
    return items


def _annotate_sources(sources: list[SourceRef]) -> list[SourceRef]:
    """Flag academic-only text already on the license, and sources whose license is not stated."""
    stamped = datetime.now(UTC).date().isoformat()
    annotated = []
    for source in sources:
        license_text = source.license or "license not stated"
        source_id = source.source_id.casefold()
        if source_id.startswith(("atted", "pmn")) and "not stated" not in license_text.casefold():
            license_text = f"{license_text} (license not stated)"
        annotated.append(source.model_copy(update={"license": license_text, "retrieved_at": source.retrieved_at or stamped}))
    return annotated


def _run_trace(state: StudyState, report: Report) -> dict[str, Any]:
    """Return a compact trace for reloads: phases, lanes, verdicts and the shortlist."""
    return {
        "schema": "agrihub.run-trace/v1",
        "run_status": "completed",
        "species": report.species,
        "assembly": report.assembly,
        "trait": report.trait,
        "rounds": int(state.get("round") or 0),
        "specialists": [
            {
                "agent_id": row.get("agent_id"),
                "specialist": row.get("specialist"),
                "status": row.get("status"),
                "summary": row.get("summary"),
                "missing_specialist_done": bool(row.get("missing_specialist_done")),
            }
            for row in state.get("specialist_results") or []
        ],
        "verification": [item.model_dump(mode="json") for item in report.verification],
        "shortlist": [item.gene_id for item in report.candidates],
        "n_candidates": len(report.candidates_full),
    }


def _findings(item: RankedCandidate) -> str:
    parts = []
    if item.supporting_findings:
        parts.append("supports " + ", ".join(item.supporting_findings))
    if item.conflicting_findings:
        parts.append("conflicts " + ", ".join(item.conflicting_findings))
    return "; ".join(parts)
