"""Agent tools over the run-scoped evidence store.

Each tool has a synchronous body and a coroutine that runs it with
``asyncio.to_thread``, so ``ainvoke`` never blocks the event loop on DuckDB.
"""

import asyncio
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, ToolException
from pydantic import ValidationError

from agrihub.configuration import agent_id_from_config, run_id_from_config
from agrihub.evidence_store import EvidenceStore, UnknownEvidenceError
from agrihub.state import Finding, Stance, Strength
from agrihub.strength import capped

MAX_EVIDENCE_PER_CALL = 40


def store_for_config(config: RunnableConfig) -> EvidenceStore:
    """Open the evidence store of the run executing this call."""
    return EvidenceStore.for_run(run_id_from_config(config))


def _get_evidence(ids: list[str], config: RunnableConfig) -> dict[str, Any]:
    """Fetch stored evidence by evidence_id or E<n> alias (at most 40 per call)."""
    store = store_for_config(config)
    requested = list(dict.fromkeys(ids))[:MAX_EVIDENCE_PER_CALL]
    items = store.get(requested)
    found = {str(item.evidence_id) for item in items} | {str(item.alias) for item in items}
    return {
        "evidence": [
            item.model_dump(mode="json", exclude_none=True) for item in items
        ],
        "not_found": [key for key in requested if key not in found],
        "truncated": len(set(ids)) > MAX_EVIDENCE_PER_CALL,
    }


async def _aget_evidence(ids: list[str], config: RunnableConfig) -> dict[str, Any]:
    """Fetch stored evidence by evidence_id or E<n> alias (at most 40 per call)."""
    return await asyncio.to_thread(_get_evidence, ids, config)


def _record_finding(
    target: str,
    claim: str,
    stance: Stance,
    strength: Strength,
    evidence_ids: list[str],
    config: RunnableConfig,
) -> dict[str, Any]:
    """Record a claim about a gene or locus backed by stored evidence ids.

    The strength is capped at what the cited evidence supports
    (:func:`agrihub.strength.capped`); a downgrade is reported in
    ``strength_note``.

    Args:
        target: The gene id, or a locus id such as ``L1``.
        claim: One sentence stating what the evidence shows.
        stance: supports, conflicts, or neutral with respect to the trait.
        strength: weak, moderate, or strong.
        evidence_ids: evidence_ids or E<n> aliases returned by evidence tools.
    """
    store = store_for_config(config)
    try:
        allowed, note = capped(strength, store.get(evidence_ids))
        finding = Finding(
            agent_id=agent_id_from_config(config) or "unknown",
            target=target,
            target_type="locus" if _is_locus_id(target) else "gene",
            claim=claim,
            stance=stance,
            strength=allowed,
            evidence_ids=evidence_ids,
        )
        saved = store.put_finding(finding)
    except UnknownEvidenceError as exc:
        raise ToolException(f"Finding rejected. {exc}") from exc
    except ValidationError as exc:
        messages = "; ".join(str(error["msg"]) for error in exc.errors())
        raise ToolException(f"Finding rejected. {messages}") from exc
    return {
        "status": "recorded",
        "finding_id": saved.finding_id,
        "evidence_ids": saved.evidence_ids,
        "strength": saved.strength,
        "requested_strength": strength,
        "strength_note": note,
    }


async def _arecord_finding(
    target: str,
    claim: str,
    stance: Stance,
    strength: Strength,
    evidence_ids: list[str],
    config: RunnableConfig,
) -> dict[str, Any]:
    """Record a claim about a gene or locus backed by stored evidence ids.

    Args:
        target: The gene id, or a locus id such as ``L1``.
        claim: One sentence stating what the evidence shows.
        stance: supports, conflicts, or neutral with respect to the trait.
        strength: weak, moderate, or strong.
        evidence_ids: evidence_ids or E<n> aliases returned by evidence tools.
    """
    return await asyncio.to_thread(
        _record_finding, target, claim, stance, strength, evidence_ids, config
    )


get_evidence = StructuredTool.from_function(
    func=_get_evidence,
    coroutine=_aget_evidence,
    name="get_evidence",
    parse_docstring=True,
)
record_finding = StructuredTool.from_function(
    func=_record_finding,
    coroutine=_arecord_finding,
    name="record_finding",
    parse_docstring=True,
    handle_tool_error=True,
)

STORE_TOOLS = (get_evidence, record_finding)


def _is_locus_id(target: str) -> bool:
    return target.startswith("L") and target[1:].isdigit()
