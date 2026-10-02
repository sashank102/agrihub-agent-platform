"""Tools every agent shares: compact evidence lookup, findings, reflection and the done signal.

``get_evidence`` and ``record_finding`` wrap the store tools with compact
text for the model and an artifact (``evidence_ids``, ``finding_id``) for
run events. ``specialist_done`` only carries the summary; the specialist
loop ends when it sees the call.
"""

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, ToolException, tool

from agrihub.state import Stance, Strength
from agrihub.tools import store_tools

VALUE_CHARS = 160
QUOTE_CHARS = 300


@tool("get_evidence", response_format="content_and_artifact", parse_docstring=True)
async def get_evidence(ids: list[str], config: RunnableConfig) -> tuple[str, dict[str, Any]]:
    """Fetch stored evidence by E<n> alias or evidence_id (at most 40 per call): gene, category, value, source and quote.

    Args:
        ids: E<n> aliases or evidence_ids returned by earlier tools.
    """
    result = await store_tools.get_evidence.ainvoke({"ids": ids}, config)
    lines = [f"get_evidence {len(result['evidence'])} of {len(ids)} ids"]
    for item in result["evidence"]:
        value = json.dumps(item.get("value"), ensure_ascii=False, separators=(",", ":"))
        line = (
            f"{item['alias']} {item['gene_id']} {item['category']}/{item['subtype']} "
            f"{value[:VALUE_CHARS]}{'…' if len(value) > VALUE_CHARS else ''} "
            f"[{item['source_db']} {item['db_version']}]"
        )
        if item.get("primary_citation"):
            line += f" {item['primary_citation']}"
        if item.get("via_ortholog"):
            line += f" via {item['via_ortholog']['species']} {item['via_ortholog']['gene_id']}"
        if item.get("quote"):
            quote = str(item["quote"])
            line += f" \u00ab{quote[:QUOTE_CHARS]}{'…' if len(quote) > QUOTE_CHARS else ''}\u00bb"
        lines.append(line)
    if result["not_found"]:
        lines.append(f"not found: {', '.join(result['not_found'])}")
    if result["truncated"]:
        lines.append("truncated: true (40 ids per call)")
    return "\n".join(lines), {
        "evidence_ids": [item["evidence_id"] for item in result["evidence"]],
        "aliases": [item["alias"] for item in result["evidence"]],
    }


@tool("record_finding", response_format="content_and_artifact", parse_docstring=True)
async def record_finding(
    target: str,
    claim: str,
    stance: Stance,
    strength: Strength,
    evidence_ids: list[str],
    config: RunnableConfig,
) -> tuple[str, dict[str, Any]]:
    """Record one evidence-backed claim about a gene or locus; rejected if any evidence id is unknown.

    The strength is capped at what the cited evidence supports, and the result says when it was lowered.

    Args:
        target: A focus gene id, or a locus id such as L1.
        claim: One or two sentences stating what the cited evidence shows.
        stance: supports, conflicts or neutral with respect to the trait.
        strength: weak, moderate or strong.
        evidence_ids: E<n> aliases or evidence_ids returned by tools in this run.
    """
    result = await store_tools.record_finding.ainvoke(
        {"target": target, "claim": claim, "stance": stance, "strength": strength, "evidence_ids": evidence_ids},
        config,
    )
    if not isinstance(result, dict) or result.get("status") != "recorded":
        raise ToolException(str(result))
    note = result.get("strength_note")
    return (
        f"recorded {result['finding_id']} on {target} ({stance}, {result['strength']}) citing {len(result['evidence_ids'])} evidence items"
        + (f"; {note}" if note else ""),
        {
            "finding_id": result["finding_id"],
            "evidence_ids": list(result["evidence_ids"]),
            "aliases": list(dict.fromkeys(evidence_ids)),
            "target": target,
            "strength": result["strength"],
        },
    )


@tool("think", parse_docstring=True)
def think(reflection: str) -> str:
    """Write down a short plan or assessment before the next tool call; it is not shown to the user.

    Args:
        reflection: What you learned, what is missing, and what you will do next.
    """
    return "Noted."


@tool("specialist_done", parse_docstring=True)
def specialist_done(summary: str) -> str:
    """Finish your lane with a summary of genes covered, finding ids recorded and gaps.

    Args:
        summary: Two to five sentences: genes covered, finding ids, gaps and what was not available.
    """
    return "Lane closed."


COMMON_TOOLS: tuple[BaseTool, ...] = (get_evidence, record_finding, think, specialist_done)
for _tool in COMMON_TOOLS:
    _tool.handle_tool_error = True
    _tool.handle_validation_error = True
