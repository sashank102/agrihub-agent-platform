"""A scripted, deterministic chat model for tests, fixture recording and offline demos.

Model names ``agrihub-fake:<script>`` select a script: a function from the
conversation and the bound tool names to the next ``AIMessage``. Tool-call
ids are derived from the lane and step, and usage is counted at four
characters per token, so two runs on the same bundle produce the same calls.
``agrihub-fake:<script>@<seconds>`` waits that long before each reply, so a
demo run stays on screen long enough to watch.

The ``poster`` script plays every role:

- orchestrator: inspects the first locus, then dispatches the deterministic
  plan of :mod:`agrihub.planning`; in a follow-up round it dispatches a
  literature check on genes that gained support, or finishes;
- specialists: call their domain tools on the focus genes, record findings
  that cite the evidence aliases those tools returned, then call
  ``specialist_done``. Phases whose tools are not bound (their domain is
  unavailable in the bundle) are skipped, and the summary lists the gaps the
  system prompt names. The literature lane stays offline (aliases and
  gene2pubmed only), so recordings never depend on the network.
"""

import json
import re
import time
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

from agrihub.prompts import UNAVAILABLE_HEADING

PREFIX = "agrihub-fake:"
Script = Callable[[list[BaseMessage], tuple[str, ...]], AIMessage]
_SCRIPTS: dict[str, Script] = {}
_ALIAS_PREFIX = re.compile(r"^((?:E\d+(?:\.\.E\d+)?,?)+) ")
_DISTANCE = re.compile(r"dist=(\d+)")
_GENE_DISTANCE = re.compile(r"gene_dist=(\d+)")
_SPAN = re.compile(r"span=([\d.]+)Mb")
_MARKERS = re.compile(r"markers=(\d+)/")


class ScriptedChatModel(BaseChatModel):
    """A chat model whose replies come from a registered script."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    script_name: str
    delay: float = 0.0
    tool_names: tuple[str, ...] = ()

    @property
    def _llm_type(self) -> str:
        return "agrihub-fake"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":  # type: ignore[override]
        """Remember the tool names; the script decides which to call."""
        names = tuple(getattr(tool, "name", None) or tool["name"] for tool in tools)
        return self.model_copy(update={"tool_names": names})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        script = _SCRIPTS.get(self.script_name)
        if script is None:
            raise ValueError(f"no fake LLM script named {self.script_name!r}; known: {', '.join(sorted(_SCRIPTS))}")
        if self.delay:
            time.sleep(self.delay)
        message = script(list(messages), self.tool_names)
        written = str(message.content) + json.dumps([call["args"] for call in message.tool_calls], sort_keys=True)
        prompt = sum(len(str(item.content)) for item in messages)
        usage = {"input_tokens": prompt // 4 + 1, "output_tokens": len(written) // 4 + 1}
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        message = message.model_copy(
            update={"usage_metadata": usage, "response_metadata": {"model_name": PREFIX + self.script_name}},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def register_script(name: str, script: Script) -> None:
    """Make ``agrihub-fake:<name>`` available."""
    _SCRIPTS[name] = script


def unregister_script(name: str) -> None:
    """Remove a script registered by a test."""
    _SCRIPTS.pop(name, None)


def scripted_model(name: str) -> ScriptedChatModel:
    """Return the model for ``<script>`` or ``<script>@<seconds>``."""
    script, _, delay = name.partition("@")
    return ScriptedChatModel(script_name=script, delay=float(delay or 0))


def call(name: str, args: dict[str, Any], call_id: str) -> ToolCall:
    """Build one tool call."""
    return ToolCall(name=name, args=args, id=call_id, type="tool_call")


def reply(*calls: ToolCall, content: str = "") -> AIMessage:
    """Build an AI message that makes ``calls``."""
    return AIMessage(content=content, tool_calls=list(calls))


def human_text(messages: list[BaseMessage]) -> str:
    """Return the first human message (the briefing or assignment)."""
    return next((str(message.content) for message in messages if isinstance(message, HumanMessage)), "")


def steps_taken(messages: list[BaseMessage]) -> int:
    """Return how many AI messages the conversation already has."""
    return sum(1 for message in messages if isinstance(message, AIMessage))


def assignment(messages: list[BaseMessage]) -> dict[str, Any]:
    """Return the specialist's ``Assignment JSON`` payload."""
    for line in human_text(messages).splitlines():
        if line.startswith("Assignment JSON: "):
            return dict(json.loads(line.removeprefix("Assignment JSON: ")))
    return {}


def json_after(text: str, marker: str) -> dict[str, Any] | None:
    """Return the JSON object on the line after ``marker`` in a briefing."""
    lines = text.splitlines()
    for index, line in enumerate(lines[:-1]):
        if line.strip() == marker:
            return dict(json.loads(lines[index + 1]))
    return None


def tool_rows(messages: list[BaseMessage], tool: str) -> list[tuple[list[str], str]]:
    """Return ``(aliases, line)`` for every evidence row of one tool's results."""
    rows: list[tuple[list[str], str]] = []
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name != tool or message.status == "error":
            continue
        for line in str(message.content).splitlines():
            match = _ALIAS_PREFIX.match(line)
            if match:
                rows.append((_expand(match.group(1)), line[match.end() :]))
    return rows


def _expand(ranges: str) -> list[str]:
    aliases: list[str] = []
    for part in ranges.strip(",").split(","):
        if ".." in part:
            first, last = (int(value[1:]) for value in part.split(".."))
            aliases.extend(f"E{number}" for number in range(first, last + 1))
        elif part:
            aliases.append(part)
    return aliases


def poster_script(messages: list[BaseMessage], tools: tuple[str, ...]) -> AIMessage:
    """Play the orchestrator, a specialist, the verifier or follow-up Q&A."""
    if "dispatch_specialists" in tools:
        return _orchestrator(messages)
    if "specialist_done" in tools:
        return _specialist(messages, tools)
    if "explain_score" in tools or "search_report" in tools:
        return _qa(messages, tools)
    if "get_evidence" in tools and "record_finding" not in tools:
        return AIMessage(content="Verifier notes are already decided. No new claims.")
    return AIMessage(content="No tools are bound.")


def _orchestrator(messages: list[BaseMessage]) -> AIMessage:
    from agrihub import planning

    text = human_text(messages)
    round_match = re.search(r"Round (\d+) of", text)
    round_number = int(round_match.group(1)) if round_match else 1
    trait_match = re.search(r'trait "([^"]*)"', text)
    trait = trait_match.group(1) if trait_match else "the trait"
    enabled_match = re.search(r"Specialists enabled: ([^.]*)\.", text)
    enabled = [name.strip() for name in enabled_match.group(1).split(",")] if enabled_match else []
    step = steps_taken(messages)
    prefix = f"call_orch_r{round_number}_s{step + 1}"
    brief = json_after(text, "Triage brief (JSON):") or {}
    delta = json_after(text, "Delta brief (JSON):")
    if delta is None:
        loci = [str(entry["locus_id"]) for entry in brief.get("loci") or []]
        if step == 0 and loci:
            return reply(call("inspect_triage", {"locus_id": loci[0]}, prefix))
        summary, dispatches, skipped = planning.plan_from_brief(brief, enabled, 12, trait)
        return reply(call("dispatch_specialists", {"summary": summary, "dispatches": dispatches, "skipped": skipped}, prefix))
    followup = planning.followup_from_delta(delta, 12, trait)
    if followup is None or "literature" not in enabled:
        return reply(call("finish_research", {"reason": "No gene gained support that literature has not covered."}, prefix))
    summary, dispatches = followup
    return reply(call("dispatch_specialists", {"summary": summary, "dispatches": dispatches}, prefix))


_PHASES: dict[str, tuple[str, ...]] = {
    "locus_variant": ("window", "variants", "linkage", "record", "done"),
    "qtl_gwas": ("overlap", "record", "done"),
    "function_orthology": ("annotate", "orthologs", "record", "done"),
    "expression_network": ("tissues", "expression", "network", "regulation", "record", "done"),
    "literature": ("aliases", "publications", "record", "done"),
}
_TSPEC = re.compile(r"tau=([\d.]+)")
_P_VALUE = re.compile(r" p=([\d.]+)")
_R2 = re.compile(r"(Glyma\.\S+) r2=([\d.]+) with (\S+)")


def _phase_calls(phase: str, task: dict[str, Any], tools: tuple[str, ...]) -> list[tuple[str, dict[str, Any]]]:
    """Return the ``(tool, args)`` calls of one data phase, keeping only bound tools."""
    genes = [str(gene) for gene in task.get("focus_gene_ids") or []]
    species = str(task.get("species") or "soybean")
    trait = str(task.get("trait") or "")
    loci = [locus for locus in task.get("focus_loci") or [] if locus.get("chrom") and locus.get("lead_pos")]
    leads = [{"label": str(locus.get("lead_snp") or locus["locus_id"]), "chrom": str(locus["chrom"]), "pos": int(locus["lead_pos"])} for locus in loci]
    calls: list[tuple[str, dict[str, Any]]] = []
    if phase == "window":
        windows = [
            {"chrom": locus["chrom"], "start": max(1, int(locus["start"])), "end": int(locus["end"]), "label": locus["locus_id"], "snp_pos": locus.get("lead_pos")}
            for locus in task.get("focus_loci") or []
            if locus.get("chrom")
        ]
        calls = [("genes_in_window", {"windows": windows, "species": species})]
    elif phase == "variants":
        calls = [
            ("annotate_variants", {"variants": [{"id": lead["label"], "chrom": lead["chrom"], "pos": lead["pos"]} for lead in leads], "species": species}),
            ("snp_in_tfbs_or_cns", {"snps": leads, "species": species}),
            ("homeologs", {"gene_ids": genes, "species": species}),
        ]
        calls = [call for call in calls if call[0] == "homeologs" or leads]
    elif phase == "linkage":
        calls = [("ld_with_lead", {"leads": leads, "species": species}), ("gene_haplotypes", {"gene_ids": genes, "species": species})]
        calls = [call for call in calls if call[0] == "gene_haplotypes" or leads]
    elif phase == "overlap":
        args = {"gene_ids": genes, "trait": trait, "trait_only": True, "species": species}
        calls = [("qtl_overlap", args), ("gwas_catalog_overlap", args)]
    elif phase == "annotate":
        calls = [("gene_annotation", {"gene_ids": genes, "species": species}), ("annotation_relevance", {"gene_ids": genes, "trait": trait, "species": species})]
    elif phase == "orthologs":
        calls = [("arabidopsis_knowledge", {"ids_or_genes": genes, "species": species})]
    elif phase == "tissues":
        calls = [("trait_relevant_tissues", {"trait": trait, "species": species})]
    elif phase == "expression":
        calls = [("tissue_specificity", {"gene_ids": genes, "trait": trait, "species": species}), ("expression_profile", {"gene_ids": genes, "trait": trait, "species": species})]
    elif phase == "network":
        calls = [("seed_propagation", {"gene_ids": genes, "trait": trait, "species": species}), ("coexpression_neighbors", {"gene_ids": genes, "trait": trait, "species": species})]
    elif phase == "regulation":
        calls = [("get_regulation", {"gene_ids": genes, "species": species}), ("get_pathways", {"gene_ids": genes, "trait": trait, "species": species})]
    elif phase == "aliases":
        calls = [("gene_aliases", {"gene_ids": genes, "species": species})]
    elif phase == "publications":
        calls = [("gene_publications", {"gene_ids": genes, "species": species})]
    return [call for call in calls if call[0] in tools]


def _specialist(messages: list[BaseMessage], tools: tuple[str, ...]) -> AIMessage:
    task = assignment(messages)
    specialist = str(task.get("specialist") or "")
    phases = [phase for phase in _PHASES.get(specialist, ("done",)) if phase in {"record", "done"} or _phase_calls(phase, task, tools)]
    step = steps_taken(messages)
    phase = phases[min(step, len(phases) - 1)]
    prefix = f"{task.get('agent_id') or specialist}-s{step + 1}"
    genes = [str(gene) for gene in task.get("focus_gene_ids") or []]
    if phase not in {"record", "done"}:
        return reply(*(call(name, args, f"{prefix}-{index}") for index, (name, args) in enumerate(_phase_calls(phase, task, tools))))
    if phase == "record":
        findings = _findings(specialist, genes, messages)
        if findings:
            return reply(*(call("record_finding", finding, f"{prefix}-{index}") for index, finding in enumerate(findings)))
    recorded = [str(message.artifact["finding_id"]) for message in messages if isinstance(message, ToolMessage) and isinstance(message.artifact, dict) and message.artifact.get("finding_id")]
    return reply(call("specialist_done", {"summary": _summary(specialist, genes, recorded, messages)}, f"{prefix}-0"))


def _findings(specialist: str, genes: list[str], messages: list[BaseMessage]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if specialist == "locus_variant":
        rows = tool_rows(messages, "genes_in_window")
        located = tool_rows(messages, "annotate_variants")
        linked = tool_rows(messages, "ld_with_lead")
        for gene in genes:
            row = next((item for item in rows if f" {gene} " in f" {item[1]} "), None)
            if row is None:
                continue
            distance = _DISTANCE.search(row[1])
            where = "overlaps the lead SNP" if "SNP-in-gene" in row[1] else f"lies {int(distance.group(1)) / 1000:.1f} kb from the lead SNP" if distance else "lies in the locus window"
            aliases = row[0][:2]
            details = []
            hit = next((item for item in located if f" {gene} " in f" {item[1]} "), None)
            if hit is not None:
                details.append(f"the lead SNP is {hit[1].split(f' {gene} ', 1)[1].split(' ')[0]} of it")
                aliases += hit[0][:1]
            ld_row = next((item for item in linked if (match := _R2.search(item[1])) and match.group(1) == gene), None)
            ld = _R2.search(ld_row[1]) if ld_row else None
            if ld_row is not None and ld is not None:
                details.append(f"r2 {ld.group(2)} with {ld.group(3)}")
                aliases += ld_row[0][:1]
            claim = f"{gene} {where}" + (f"; {', '.join(details)}" if details else "") + "; this is positional evidence only."
            findings.append(_finding(gene, claim, "neutral", "weak", aliases))
    elif specialist == "qtl_gwas":
        rows = tool_rows(messages, "qtl_overlap") + tool_rows(messages, "gwas_catalog_overlap")
        for gene in genes:
            hits = [item for item in rows if f"[{gene}]" in item[1]]
            if hits:
                hits.sort(key=lambda item: not _narrow_trait_hit(item[1]))
                aliases = [alias for item in hits for alias in item[0]][:6]
                narrow = sum(1 for item in hits if _narrow_trait_hit(item[1]))
                claim = f"{len(hits)} trait-matched QTL or catalog GWAS records overlap {gene} or lie within 50 kb of it" + (
                    f"; {narrow} of them are narrow ontology-matched QTLs or GWAS hits within 50 kb." if narrow else "; all are wide, single-marker or keyword-matched."
                )
                findings.append(_finding(gene, claim, "supports", "moderate" if narrow else "weak", aliases))
    elif specialist == "function_orthology":
        rows = tool_rows(messages, "annotation_relevance")
        for gene in genes:
            row = next((item for item in rows if item[1].startswith(f"{gene} ") and "no match" not in item[1]), None)
            if row is not None:
                findings.append(_finding(gene, f"Annotation of {gene} or its Arabidopsis best hit matches the trait profile ({row[1].split(' ', 2)[1]}).", "supports", "weak", row[0][:4]))
    elif specialist == "expression_network":
        specific = tool_rows(messages, "tissue_specificity")
        profiles = tool_rows(messages, "expression_profile")
        walks = tool_rows(messages, "seed_propagation")
        for gene in genes:
            own = [item for item in specific if item[1].startswith(f"{gene} ")]
            top = [item for item in own if "TRAIT-TISSUE-TOP" in item[1] and _tau(item[1]) >= 0.8]
            expressed = [item for item in profiles if item[1].startswith(f"{gene} ") and "EXPRESSED-IN-TRAIT-TISSUE" in item[1]]
            if top:
                findings.append(_finding(gene, f"{gene} is most expressed in a trait-relevant tissue and tissue-specific ({top[0][1].split(' ', 2)[2].split(' |')[0]}).", "supports", "moderate", top[0][0][:1] + (expressed[0][0][:1] if expressed else [])))
            elif expressed:
                findings.append(_finding(gene, f"{gene} is expressed in a trait-relevant tissue ({expressed[0][1].split('; ', 1)[-1]}), without trait-tissue specificity.", "supports", "weak", expressed[0][0][:1]))
            elif own:
                findings.append(_finding(gene, f"{gene} is not expressed above 1 TPM in the trait-relevant tissues of the atlases.", "neutral", "weak", own[0][0][:1]))
            walk = next((item for item in walks if item[1].startswith(f"{gene} ")), None)
            p_value = _P_VALUE.search(walk[1]) if walk else None
            if walk is not None and p_value is not None and float(p_value.group(1)) <= 0.05:
                strength = "moderate" if float(p_value.group(1)) <= 0.01 else "weak"
                findings.append(_finding(gene, f"{gene} is close to the trait seed genes in the STRING network (seed propagation empirical p {p_value.group(1)}).", "supports", strength, walk[0][:1]))
    elif specialist == "literature":
        rows = tool_rows(messages, "gene_publications")
        for gene in genes:
            linked = [item for item in rows if item[1].startswith(f"{gene} ")]
            if linked:
                pmids = ", ".join(item[1].split()[1] for item in linked[:3])
                findings.append(_finding(gene, f"NCBI gene2pubmed links {gene} to {len(linked)} non-hub papers ({pmids}); no passage was read.", "neutral", "weak", [alias for item in linked for alias in item[0]][:5]))
    return findings


def _tau(line: str) -> float:
    match = _TSPEC.search(line)
    return float(match.group(1)) if match else 0.0


def _narrow_trait_hit(line: str) -> bool:
    """Return whether a qtl_overlap or gwas_catalog_overlap row qualifies for a moderate claim."""
    if "match=ontology" not in line:
        return False
    gene_distance = _GENE_DISTANCE.search(line)
    if gene_distance is not None:
        return int(gene_distance.group(1)) <= 50_000
    span = _SPAN.search(line)
    markers = _MARKERS.search(line)
    return (
        span is not None
        and markers is not None
        and float(span.group(1)) <= 1.0
        and int(markers.group(1)) >= 2
        and " WIDE" not in line
        and "marker_within" not in line
    )


def _finding(target: str, claim: str, stance: str, strength: str, evidence_ids: list[str]) -> dict[str, Any]:
    return {"target": target, "claim": claim, "stance": stance, "strength": strength, "evidence_ids": evidence_ids}


def _qa(messages: list[BaseMessage], tools: tuple[str, ...]) -> AIMessage:
    """Look up the gene in the question, then answer with the aliases the tool returned."""
    step = steps_taken(messages)
    text = human_text(messages)
    if re.search(r"new window|another trait|re-?run|different trait", text, re.IGNORECASE):
        return AIMessage(content="That needs a new study. Follow-up questions cannot change the window, the trait or the SNP set.")
    gene = re.search(r"Glyma\.\d+G\d+", text)
    if step == 0 and gene and "explain_score" in tools:
        return reply(call("explain_score", {"gene_id": gene.group(0)}, "qa-explain"))
    if step == 0 and "search_report" in tools:
        return reply(call("search_report", {"query": text[-240:]}, "qa-search"))
    aliases = re.findall(r"\bE\d+\b", "\n".join(str(message.content) for message in messages))
    cited = " ".join(f"[{alias}]" for alias in list(dict.fromkeys(aliases))[:6])
    subject = gene.group(0) if gene else "The candidate"
    if not cited:
        return AIMessage(content=f"{subject} is in the finished report. The study was not modified.")
    return AIMessage(content=f"{subject} is a candidate because of the stored evidence {cited}. The study was not modified.")


def unavailable_domains(messages: list[BaseMessage]) -> list[str]:
    """Return the domain labels the system prompt lists as unavailable."""
    system = next((str(message.content) for message in messages if isinstance(message, SystemMessage)), "")
    _, found, rest = system.partition(f"\n{UNAVAILABLE_HEADING}\n")
    if not found:
        return []
    labels = []
    for line in rest.splitlines()[1:]:
        if not line.startswith("- "):
            break
        labels.append(line[2:].split(":", 1)[0])
    return labels


def _summary(specialist: str, genes: list[str], recorded: list[str], messages: list[BaseMessage]) -> str:
    gaps = unavailable_domains(messages)
    text = f"Covered {len(genes)} genes; recorded {', '.join(recorded) or 'no findings'}."
    if gaps:
        text += f" Not available in this build: {'; '.join(gaps)}."
    if specialist == "literature":
        text += " The offline script read gene2pubmed only; Europe PMC, PubMed and PubTator3 were not searched."
    return text


register_script("poster", poster_script)
