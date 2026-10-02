"""LangChain tools over the extended tier: expression, networks, regulation and pathways.

They follow :mod:`agrihub.tools.bundle_tools`: async, one worker thread per
call, batch inputs, one stored ``EvidenceItem`` per fact and compact text with
``E<n>`` aliases, ``truncated`` and ``output_ref``. Against a bundle without
the domain's tables they return no rows and say which tier provides them.
"""

from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel

from agrihub.tools.bundle_tools import _bundle, _clip, _respond, _run
from agrihub_data.availability import domain_status
from agrihub_data.bundle import Bundle
from agrihub_data.query import expression, traits


class DatasetTissues(BaseModel):
    """The trait-relevant tissues one expression atlas samples."""

    dataset: str
    tissues: list[str]


def _gap(bundle: Bundle, domain: str) -> list[str]:
    status = domain_status(bundle.species).get(domain)
    return [f"unavailable: {status.label}: {status.reason}"] if status is not None and not status.available else []


def _selection(bundle: Bundle, trait: str | None, tissues: list[str] | None) -> expression.TissueSelection:
    profile = traits.map_trait(trait, bundle.species, bundle) if trait else None
    return expression.trait_relevant_tissues(bundle.species, profile, bundle, tissues)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def trait_relevant_tissues(
    trait: str,
    config: RunnableConfig,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Return the tissues and stages where the trait is expected to act (curated), and which expression datasets sample them.

    Args:
        trait: Trait text, e.g. plant height; mapped with map_trait.
        species: Registered species, e.g. soybean.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        selection = _selection(bundle, trait, None)
        notes = [f"note: {selection.note}"] if selection.note else []
        notes += [f"stages: {', '.join(selection.stages)}"] if selection.stages else []
        rows = [DatasetTissues(dataset=dataset, tissues=found) for dataset, found in selection.in_bundle.items()]
        return _respond(
            config,
            "trait_relevant_tissues",
            f"trait_relevant_tissues '{trait}' ({selection.trait_key}, {selection.origin}): {', '.join(selection.tissues) or 'none'}",
            rows,
            lambda row: f"{row.dataset}: samples {', '.join(row.tissues)}",
            evidence=False,
            notes=[*notes, *_gap(bundle, "expression")],
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def expression_profile(
    gene_ids: list[str],
    config: RunnableConfig,
    trait: str | None = None,
    tissues: list[str] | None = None,
    dataset: str | None = None,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Return each gene's expression (TPM) per atlas: its top samples and its maximum in the trait-relevant tissues.

    Args:
        gene_ids: Canonical-assembly gene ids.
        trait: Trait text; picks the trait-relevant tissues (trait_relevant_tissues).
        tissues: Tissues to treat as trait-relevant instead, e.g. ["seed"].
        dataset: One atlas (Sreedasyam_Plott_2023 or Libault_Farmer_2010); default all.
        species: Registered species, e.g. soybean.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        selection = _selection(bundle, trait, tissues)
        rows = expression.expression_profile(bundle, gene_ids, selection, dataset)
        found = {row.gene_id for row in rows}
        missing = [gene for gene in gene_ids if gene not in found]
        return _respond(
            config,
            "expression_profile",
            f"expression_profile {len(found)} of {len(set(gene_ids))} genes; trait tissues {', '.join(selection.tissues) or 'none'}",
            rows,
            lambda row: (
                f"{row.gene_id} [{row.dataset}] max {row.max_value:g} {row.unit} in {row.max_sample}"
                + (
                    f"; trait tissues max {row.trait_max_value:g} in {row.trait_max_sample}"
                    if row.trait_max_value is not None
                    else "; no trait-tissue samples in this atlas"
                )
                + (" EXPRESSED-IN-TRAIT-TISSUE" if row.in_trait_tissue else "")
                + ("" if row.expressed else " NOT-EXPRESSED")
            ),
            evidence=True,
            notes=[*([f"not found in the expression atlases: {', '.join(missing)}"] if missing else []), *_gap(bundle, "expression")],
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def tissue_specificity(
    gene_ids: list[str],
    config: RunnableConfig,
    trait: str | None = None,
    tissues: list[str] | None = None,
    dataset: str | None = None,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Return tissue specificity per gene and atlas: tau (0 ubiquitous, 1 one tissue), top tissue and the z-score of the trait tissues.

    Args:
        gene_ids: Canonical-assembly gene ids.
        trait: Trait text; picks the trait-relevant tissues.
        tissues: Tissues to treat as trait-relevant instead.
        dataset: One atlas; default all.
        species: Registered species, e.g. soybean.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        selection = _selection(bundle, trait, tissues)
        rows = expression.tissue_specificity(bundle, gene_ids, selection, dataset)
        return _respond(
            config,
            "tissue_specificity",
            f"tissue_specificity {len({row.gene_id for row in rows})} genes; trait tissues {', '.join(selection.tissues) or 'none'}",
            rows,
            lambda row: (
                f"{row.gene_id} [{row.dataset}] tau={row.tau if row.tau is not None else 'n/a'} top={row.top_tissue or '-'}"
                + (f" trait_z={row.trait_z:g}" if row.trait_z is not None else "")
                + (" TRAIT-TISSUE-TOP" if row.trait_tissue_top else "")
                + f" | {_clip(', '.join(f'{tissue}:{value:g}' for tissue, value in row.tissue_means.items()), 90)}"
            ),
            evidence=True,
            notes=_gap(bundle, "expression"),
        )

    return await _run(work)


EXPRESSION_TOOLS: tuple[BaseTool, ...] = (trait_relevant_tissues, expression_profile, tissue_specificity)
EXTENDED_TOOLS: tuple[BaseTool, ...] = (*EXPRESSION_TOOLS,)
for _tool in EXTENDED_TOOLS:
    _tool.handle_tool_error = True
