"""System prompts for the AgriHub agents.

Each agent module defines one ``AgentPrompt`` constant: role, inputs,
procedure, output contract, stop rules, the tools it may call, and the
evidence domains it depends on. :func:`render` turns a prompt and a
:class:`PromptContext` into the system message, prefixed with the shared
guardrails of :mod:`agrihub.prompts.guardrails`. The context lists the
domains that are unavailable for this study's bundle
(:mod:`agrihub_data.availability`), so the prompt names a gap only when its
data or binary is actually missing.
"""

from dataclasses import dataclass, field

from agrihub.prompts.guardrails import render_guardrails

UNAVAILABLE_HEADING = "## Unavailable domains in this build"


@dataclass(frozen=True)
class AgentPrompt:
    """One agent's system prompt as data."""

    name: str
    title: str
    role: str
    inputs: tuple[str, ...]
    procedure: tuple[str, ...]
    output_contract: tuple[str, ...]
    stop_rules: tuple[str, ...]
    tools: tuple[str, ...]
    domains: tuple[str, ...] = ()
    """Evidence domain keys (``agrihub_data.availability``) this agent's tools serve."""


@dataclass(frozen=True)
class PromptContext:
    """Study values a prompt is templated with."""

    species: str
    assembly: str
    trait: str
    max_steps: int
    profile_key: str | None = None
    profile_terms: tuple[str, ...] = ()
    profile_keywords: tuple[str, ...] = ()
    seed_families: tuple[str, ...] = ()
    focus_gene_ids: tuple[str, ...] = ()
    focus_loci: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    """One line per unavailable domain of this agent: what it covers and why it is missing."""
    extra: dict[str, str] = field(default_factory=dict)


def render(prompt: AgentPrompt, context: PromptContext, *, tools: tuple[str, ...] | None = None) -> str:
    """Return the full system message for ``prompt``; ``tools`` narrows the listed tools to those bound."""
    listed = tools if tools is not None else prompt.tools
    lines = [render_guardrails(context), "", f"# Role: {prompt.title}", prompt.role.format(**_values(context))]
    if context.focus_gene_ids or context.focus_loci:
        lines += [
            "",
            "## Your focus",
            f"Genes: {', '.join(context.focus_gene_ids) or 'none'}",
            f"Loci: {', '.join(context.focus_loci) or 'none'}",
        ]
    lines += ["", "## Trait profile", _profile(context)]
    lines += ["", "## Inputs", *(f"- {item.format(**_values(context))}" for item in prompt.inputs)]
    lines += ["", "## Procedure", *(f"{index}. {step.format(**_values(context))}" for index, step in enumerate(prompt.procedure, start=1))]
    lines += ["", "## Tools", "You may call only these tools: " + ", ".join(listed) + "."]
    if context.unavailable:
        lines += [
            "",
            UNAVAILABLE_HEADING,
            "The data or binaries for these domains are missing. Do not pretend to have their data; list each domain you would have checked as a gap "
            '("not available in this build"), never as negative evidence:',
            *(f"- {item}" for item in context.unavailable),
        ]
    lines += ["", "## Output contract", *(f"- {item.format(**_values(context))}" for item in prompt.output_contract)]
    lines += ["", "## Stop rules", *(f"- {item.format(**_values(context))}" for item in prompt.stop_rules)]
    return "\n".join(lines)


def _profile(context: PromptContext) -> str:
    if not context.profile_key:
        return f'No curated profile; use the trait text "{context.trait}".'
    parts = [f"profile {context.profile_key}"]
    if context.profile_terms:
        parts.append("ontology terms: " + "; ".join(context.profile_terms))
    if context.profile_keywords:
        parts.append("keywords: " + ", ".join(context.profile_keywords))
    if context.seed_families:
        parts.append("trait gene families: " + ", ".join(context.seed_families))
    return ". ".join(parts) + "."


def _values(context: PromptContext) -> dict[str, str]:
    return {
        "species": context.species,
        "assembly": context.assembly,
        "trait": context.trait,
        "max_steps": str(context.max_steps),
        **context.extra,
    }
