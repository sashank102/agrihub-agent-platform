"""Answer a follow-up question from the finished study without changing it.

The node runs only when the input carries ``followup`` and the thread already
has a report. A follow-up is a new run on the study's thread, so its tools
read the report and the evidence store of the run that wrote the report
(``study_run_id``). When that run's database is gone, the store is restored
from the run's ``evidence_snapshot`` artifact, and only then from a snapshot
file. A question that needs a new window or trait is refused.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agrihub import models
from agrihub.configuration import StudyConfiguration, run_id_from_config, run_root
from agrihub.evidence_store import (
    DATABASE_NAME,
    SNAPSHOT_NAME,
    SNAPSHOT_SCHEMA,
    EvidenceStore,
)
from agrihub.state import RankedCandidate, StudyState

ArtifactReader = Callable[..., Awaitable[dict[str, Any] | None]]
"""Read one artifact of the caller's thread.

Called as ``reader(config, kind=..., artifact_id=...)``; returns its content or ``None``.
"""

_RERUN = re.compile(r"new window|another trait|re-?run|different trait|new snp", re.IGNORECASE)
MAX_STEPS = 4


async def open_store(
    config: RunnableConfig,
    state: StudyState,
    artifact_reader: ArtifactReader | None = None,
) -> EvidenceStore:
    """Open the finished study's evidence store.

    Tries the study run's database, then the run's ``evidence_snapshot``
    artifact, then the snapshot file in the study run directory, then a
    snapshot carried on the state. A restored store is written to the study
    run's directory, so retention removes it together with the study run.
    """
    study_run = str(state.get("study_run_id") or "")
    if study_run:
        existing = await asyncio.to_thread(_existing_store, study_run)
        if existing is not None:
            return existing
    snapshot: Any = None
    if artifact_reader is not None:
        snapshot = await artifact_reader(
            config,
            kind="evidence_snapshot",
            artifact_id=state.get("evidence_snapshot_id"),
        )
    if not _is_snapshot(snapshot) and study_run:
        snapshot = await asyncio.to_thread(_snapshot_file, study_run)
    if not _is_snapshot(snapshot):
        snapshot = state.get("evidence_snapshot")
    target = study_run or run_id_from_config(config)
    if _is_snapshot(snapshot):
        return await asyncio.to_thread(EvidenceStore.restore, snapshot, run_id=target)
    return await asyncio.to_thread(EvidenceStore.for_run, target)


def _existing_store(run_id: str) -> EvidenceStore | None:
    if not (run_root() / run_id / DATABASE_NAME).exists():
        return None
    store = EvidenceStore.for_run(run_id)
    if store.count() > 0:
        return store
    store.close()
    return None


def _snapshot_file(run_id: str) -> dict[str, Any] | None:
    path = run_root() / run_id / SNAPSHOT_NAME
    if not path.exists():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def _is_snapshot(value: Any) -> bool:
    return isinstance(value, dict) and value.get("schema") == SNAPSHOT_SCHEMA


def make_followup(artifact_reader: ArtifactReader | None = None) -> Callable[..., Any]:
    """Return the follow-up node bound to an optional artifact reader."""

    async def followup_qa(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
        """Answer ``followup`` from the report and the evidence store, then clear the question."""
        question = str(state.get("followup") or "").strip()
        if not question or not state.get("report"):
            return {"messages": [AIMessage(content="There is no finished report to ask about.")], "followup": ""}
        store = await open_store(config, state, artifact_reader)
        try:
            answer, transcript = await _converse(question, state, store, config)
        finally:
            store.close()
        return {"messages": transcript or [HumanMessage(content=question), AIMessage(content=answer)], "followup": ""}

    return followup_qa


followup_qa = make_followup()


async def _converse(
    question: str,
    state: StudyState,
    store: EvidenceStore,
    config: RunnableConfig,
) -> tuple[str, list[Any]]:
    settings = StudyConfiguration.from_runnable_config(config)
    report = state.get("report") or {}
    tools = [_search_report(report), _query_candidates(report), _explain_score(report), _get_evidence(store)]
    model = models.tool_model(
        settings.qa_model,
        tools,
        max_tokens=settings.model_max_tokens,
        max_retries=settings.model_max_retries,
        prompt_caching=settings.prompt_caching,
    )
    messages: list[Any] = [
        SystemMessage(
            content=(
                "You answer questions about one finished AgriHub study. Cite evidence aliases as [E12]. "
                "Do not change the study. If the question needs a new window, trait or SNP set, say so and stop."
            )
        ),
        HumanMessage(content=question),
    ]
    by_name = {item.name: item for item in tools}
    for _ in range(MAX_STEPS):
        response = await model.ainvoke(messages, config)
        messages.append(response)
        calls = list(getattr(response, "tool_calls", None) or [])
        if not calls:
            break
        for index, call in enumerate(calls):
            name = str(call.get("name"))
            tool = by_name.get(name)
            call_id = str(call.get("id") or f"qa-{index}")
            if tool is None:
                messages.append(ToolMessage(content=f"unknown tool {name}", tool_call_id=call_id, name=name, status="error"))
                continue
            result = await tool.ainvoke(call.get("args") or {})
            messages.append(ToolMessage(content=str(result), tool_call_id=call_id, name=name))
    text = _last_text(messages)
    known = {item.alias for item in store.query() if item.alias}
    cited = set(re.findall(r"\[(E\d+)\]", text))
    if _RERUN.search(question):
        text = "That needs a new study. Follow-up questions cannot change the window, the trait or the SNP set."
    elif not cited or not cited <= known:
        text = _grounded_answer(question, report, store)
    return text, [HumanMessage(content=question), AIMessage(content=text)]


def _last_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
            return str(message.content or "")
    return ""


def _grounded_answer(question: str, report: dict[str, Any], store: EvidenceStore) -> str:
    """Answer from the ranking when the model did not cite a resolvable id."""
    if _RERUN.search(question):
        return "That needs a new study. Follow-up questions cannot change the window, the trait or the SNP set."
    rows = [RankedCandidate.model_validate(raw) for raw in report.get("candidates_full") or report.get("candidates") or []]
    asked = question.casefold()
    gene = next((row for row in rows if row.gene_id.casefold() in asked), rows[0] if rows else None)
    if gene is None:
        return "The report has no ranked candidates to cite."
    aliases = _aliases(store, gene.evidence_ids)
    cited = " ".join(f"[{alias}]" for alias in aliases[:4]) or "no stored alias"
    conflicts = ", ".join(gene.conflicting_findings) or "none"
    return (
        f"{gene.gene_id} is a {gene.tier} candidate in {gene.locus_id} with score {gene.score:g}. "
        f"Evidence: {cited}. Conflicts: {conflicts}. The study was not modified."
    )


def _aliases(store: EvidenceStore, evidence_ids: list[str]) -> list[str]:
    if not evidence_ids:
        return []
    found = store.get(evidence_ids)
    return [str(item.alias) for item in found if item.alias]


def _search_report(report: dict[str, Any]):
    @tool
    def search_report(query: str) -> str:
        """Search the finished report (summary and tables) and candidate list.

        Args:
            query: Words to find in the report.
        """
        text = "\n".join(str(report.get(key) or "") for key in ("markdown", "details_markdown"))
        needles = [part.casefold() for part in query.split() if len(part) > 2][:6]
        lines = [line for line in text.splitlines() if any(needle in line.casefold() for needle in needles)]
        genes = [row.get("gene_id") for row in report.get("candidates_full") or report.get("candidates") or []]
        matched = [gene for gene in genes if gene and any(gene.casefold() in query.casefold() for _ in [0])]
        head = "\n".join(lines[:8]) or "No matching report lines."
        return head + ("\nGenes: " + ", ".join(str(gene) for gene in matched[:8]) if matched else "")

    return search_report


def _query_candidates(report: dict[str, Any]):
    @tool
    def query_candidates(locus_id: str = "", tier: str = "", limit: int = 10) -> str:
        """List ranked candidates, optionally filtered by locus or tier.

        Args:
            locus_id: Locus id such as L1. Empty lists every locus.
            tier: Tier such as T1. Empty lists every tier.
            limit: Maximum rows.
        """
        rows = report.get("candidates_full") or report.get("candidates") or []
        kept = []
        for raw in rows:
            if locus_id and raw.get("locus_id") != locus_id:
                continue
            if tier and raw.get("tier") != tier:
                continue
            kept.append(f"{raw.get('rank')} {raw.get('gene_id')} {raw.get('tier')} {raw.get('score')} {raw.get('locus_id')}")
            if len(kept) >= limit:
                break
        return "\n".join(kept) or "No candidates match."

    return query_candidates


def _explain_score(report: dict[str, Any]):
    @tool
    def explain_score(gene_id: str) -> str:
        """Explain one gene's tier, category points and evidence ids.

        Args:
            gene_id: A candidate gene id from the report.
        """
        rows = report.get("candidates_full") or report.get("candidates") or []
        raw = next((row for row in rows if row.get("gene_id") == gene_id), None)
        if raw is None:
            return f"{gene_id} is not in this study."
        points = raw.get("category_points") or {}
        evidence = ", ".join(raw.get("evidence_ids") or []) or "none"
        return (
            f"{gene_id} tier {raw.get('tier')} score {raw.get('score')} locus {raw.get('locus_id')} "
            f"points {points} evidence {evidence} verifier {raw.get('verifier_status')}"
        )

    return explain_score


def _get_evidence(store: EvidenceStore):
    @tool
    def get_evidence(ids: list[str]) -> str:
        """Fetch stored evidence by id or E<n> alias.

        Args:
            ids: Evidence ids or aliases.
        """
        items = store.get(ids)
        if not items:
            return "No evidence found."
        lines = []
        for item in items:
            lines.append(f"{item.alias} {item.gene_id} {item.source_db} {item.subtype} {(item.quote or '')[:180]}")
        return "\n".join(lines)

    return get_evidence
