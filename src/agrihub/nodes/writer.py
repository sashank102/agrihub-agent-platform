"""Write the structured report and export the evidence ledger."""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from agrihub import events
from agrihub.configuration import run_id_from_config
from agrihub.evidence_store import EvidenceStore
from agrihub.nodes.locus_builder import STUB_SOURCE, STUB_VERSION
from agrihub.state import Locus, RankedCandidate, Report, SourceRef, StudyState

ArtifactSink = Callable[..., Awaitable[str | None]]
"""Persist one artifact and return its id.

Called as ``sink(config, kind=..., title=..., content=..., metadata=...)``.
"""


def make_writer(artifact_sink: ArtifactSink | None = None) -> Callable[..., Any]:
    """Return the writer node bound to an optional artifact sink."""

    async def writer(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
        events.phase("reporting")
        store = EvidenceStore.for_run(run_id_from_config(config))
        report = _report(state, store)
        snapshot = store.snapshot()
        snapshot_path = store.export()
        report_id = None
        snapshot_id = None
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
        events.artifact_created("evidence_snapshot", title="Evidence ledger", artifact_id=snapshot_id)
        events.artifact_created("report", title=report.title, artifact_id=report_id)
        store.close()
        events.phase("reporting", "completed")
        return {
            "report": report.model_dump(mode="json"),
            "messages": [AIMessage(content=report.markdown)],
            "run_status": "completed",
        }

    return writer


def _report(state: StudyState, store: EvidenceStore) -> Report:
    study = state.get("study") or {}
    loci = [Locus.model_validate(raw) for raw in state.get("loci") or []]
    candidates = [RankedCandidate.model_validate(raw) for raw in state.get("ranking") or []]
    title = f"{study.get('trait_text')} candidate genes in {study.get('species')}"
    lines = [
        f"# {title}",
        "",
        f"Assembly {study.get('assembly')}; {len(loci)} loci; "
        f"{len(candidates)} ranked candidates.",
        "",
        "This is a skeleton report built from stub data. It is not a research result.",
        "",
    ]
    lines.extend(
        f"{item.rank}. {item.gene_id} ({item.locus_id}, {item.tier}, score {item.score})"
        + (f" [{', '.join(item.evidence_ids)}]" if item.evidence_ids else "")
        for item in candidates
    )
    return Report(
        title=title,
        species=str(study.get("species") or ""),
        assembly=str(study.get("assembly") or ""),
        trait=str(study.get("trait_text") or ""),
        mode=study.get("mode") or "snps",
        provenance={
            "window": study.get("window") or {},
            "model": state.get("model_result") or {},
            "rounds": int(state.get("round") or 0),
        },
        loci=loci,
        candidates=candidates,
        limitations=["All genes and evidence come from the plan-1 stub source."],
        suggested_validations=[],
        sources=[
            SourceRef(
                source_id=STUB_SOURCE,
                name="AgriHub stub source",
                version=STUB_VERSION,
                license="internal test data",
            )
        ],
        evidence_count=store.count(),
        finding_count=len(store.findings()),
        markdown="\n".join(lines),
    )
