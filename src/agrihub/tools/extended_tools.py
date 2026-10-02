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

from agrihub.tools.bundle_tools import SnpPosition, _as, _bundle, _clip, _respond, _run
from agrihub_data.availability import domain_status
from agrihub_data.bundle import Bundle
from agrihub_data.query import expression, network, pathways, regulation, traits
from agrihub_data.query.common import Region


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


def _seeds(bundle: Bundle, trait: str | None, seeds: list[str] | None) -> tuple[str, list[network.Seed]]:
    if seeds:
        return "custom", [network.Seed(gene_id=gene, reason="given") for gene in seeds]
    if not trait:
        return "any", []
    profile = traits.map_trait(trait, bundle.species, bundle)
    return profile.key, network.trait_seeds(bundle, profile)


def _neighbors(
    config: RunnableConfig,
    tool_name: str,
    species: str,
    gene_ids: list[str],
    graph: network.Network,
    trait: str | None,
    min_score: float | None,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    bundle = _bundle(species)
    trait_key, seeds = _seeds(bundle, trait, None)
    seed_ids = {seed.gene_id for seed in seeds}
    rows = network.network_neighbors(bundle, gene_ids, graph, min_score, limit, seed_ids, trait_key if trait else None)
    covered = {row.gene_id for row in rows}
    missing = [gene for gene in gene_ids if gene not in covered]
    threshold = network.DEFAULT_MIN_SCORE[graph] if min_score is None else min_score
    unit = "z" if graph == "atted" else "score"
    return _respond(
        config,
        tool_name,
        f"{tool_name} {graph} ({unit} >= {threshold:g}): {len(rows)} neighbours of {len(covered)} genes"
        + (f"; {sum(1 for row in rows if row.neighbor_is_seed)} are {trait_key} seed genes" if trait else ""),
        rows,
        lambda row: (
            f"{row.gene_id} -> {row.neighbor_id} {unit}={row.score:g}"
            + (f" rank={row.rank}" if row.rank else "")
            + (" SEED" if row.neighbor_is_seed else "")
            + (f" [{','.join(name for name, value in (row.channels or {}).items() if value)}]" if row.channels else "")
            + f" | {_clip(row.neighbor_defline, 60)}"
        ),
        evidence=True,
        notes=[
            *([f"no {graph} neighbours above the threshold: {', '.join(missing)}"] if missing else []),
            *_gap(bundle, "coexpression" if graph == "atted" else "network"),
        ],
    )


@tool(response_format="content_and_artifact", parse_docstring=True)
async def network_neighbors(
    gene_ids: list[str],
    config: RunnableConfig,
    trait: str | None = None,
    min_score: float | None = None,
    limit: int = 10,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Return each gene's strongest STRING neighbours (no text mining; score 0-1, default >= 0.7), flagging trait seed genes.

    Args:
        gene_ids: Canonical-assembly gene ids.
        trait: Trait text; neighbours that are curated trait genes or have a trait-matched Arabidopsis ortholog are marked SEED.
        min_score: Minimum combined score; 0.4 medium, 0.7 high, 0.9 highest confidence.
        limit: Neighbours per gene, at most 25.
        species: Registered species, e.g. soybean.
    """
    return await _run(lambda: _neighbors(config, "network_neighbors", species, gene_ids, "string", trait, min_score, min(limit, 25)))


@tool(response_format="content_and_artifact", parse_docstring=True)
async def coexpression_neighbors(
    gene_ids: list[str],
    config: RunnableConfig,
    trait: str | None = None,
    min_z: float | None = None,
    limit: int = 10,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Return each gene's top ATTED-II co-expression partners (z-score, default >= 3), flagging trait seed genes.

    Args:
        gene_ids: Canonical-assembly gene ids.
        trait: Trait text; partners that are trait seed genes are marked SEED.
        min_z: Minimum co-expression z-score.
        limit: Partners per gene, at most 25.
        species: Registered species, e.g. soybean.
    """
    return await _run(lambda: _neighbors(config, "coexpression_neighbors", species, gene_ids, "atted", trait, min_z, min(limit, 25)))


@tool(response_format="content_and_artifact", parse_docstring=True)
async def seed_propagation(
    gene_ids: list[str],
    config: RunnableConfig,
    trait: str | None = None,
    seeds: list[str] | None = None,
    network_name: network.Network = "string",
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Rank candidates by random-walk-with-restart proximity to trait seed genes, with an empirical p from 1000 degree-matched random seed sets.

    Seeds default to the trait's curated genes plus genes whose Arabidopsis
    ortholog has a trait-matching phenotype or experimental GO; a candidate
    that is a seed is scored without itself.

    Args:
        gene_ids: Candidate gene ids (canonical assembly).
        trait: Trait text that picks the seeds; required unless seeds are given.
        seeds: Explicit seed gene ids instead of the trait seeds.
        network_name: string (protein associations) or atted (co-expression).
        species: Registered species, e.g. soybean.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        trait_key, chosen = _seeds(bundle, trait, seeds)
        if not chosen:
            raise ValueError("give a trait with seed genes or explicit seeds")
        rows = network.seed_propagation(bundle, gene_ids, [seed.gene_id for seed in chosen], network_name, trait_key=trait_key)
        absent = [row.gene_id for row in rows if not row.in_network]
        first = next(iter(rows), None)
        return _respond(
            config,
            "seed_propagation",
            f"seed_propagation {network_name} from {len(chosen)} {trait_key} seeds"
            + (f" ({first.n_seeds_in_network} in the network)" if first else "")
            + f": {len(rows) - len(absent)} of {len(rows)} candidates scored",
            [row for row in rows if row.in_network],
            lambda row: (
                f"{row.gene_id} rank={row.rank or '-'} p={row.empirical_p if row.empirical_p is not None else 'n/a'} degree={row.degree}"
                + (" (is a seed; scored without itself)" if row.is_seed else "")
                + (f" nearest seeds: {', '.join(f'{seed.seed} {seed.share:.0%}' for seed in row.top_seeds)}" if row.top_seeds else "")
            ),
            evidence=True,
            notes=[
                f"seeds: {', '.join(f'{seed.gene_id} ({seed.reason})' for seed in chosen[:6])}" + (f" and {len(chosen) - 6} more" if len(chosen) > 6 else ""),
                *([f"not in the {network_name} network: {', '.join(absent)}"] if absent else []),
                *_gap(bundle, "coexpression" if network_name == "atted" else "network"),
            ],
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def get_regulation(
    gene_ids: list[str],
    config: RunnableConfig,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Return whether each gene is a transcription factor (PlantTFDB family, motifs) and its PlantRegMap targets and regulators.

    Args:
        gene_ids: Canonical-assembly gene ids.
        species: Registered species, e.g. soybean.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        rows = regulation.get_regulation(bundle, gene_ids)
        return _respond(
            config,
            "get_regulation",
            f"get_regulation {len(rows)} genes: {sum(1 for row in rows if row.is_tf)} transcription factors",
            [row for row in rows if row.is_tf or row.n_regulators],
            lambda row: (
                f"{row.gene_id} "
                + (f"TF {row.family}" + (f" motifs={','.join(row.motif_ids[:3])}" if row.motif_ids else "") + f" targets={row.n_targets}" if row.is_tf else "not a TF")
                + (f" (focus targets: {', '.join(row.focus_targets[:4])})" if row.focus_targets else "")
                + f" regulators={row.n_regulators}"
                + (" [" + ", ".join(" ".join(filter(None, (partner.gene_id, partner.family))) for partner in row.regulators[:3]) + "]" if row.regulators else "")
            ),
            evidence=True,
            notes=[
                *([f"not found in PlantTFDB/PlantRegMap: {', '.join(row.gene_id for row in rows if not row.is_tf and not row.n_regulators)}"] if any(not row.is_tf and not row.n_regulators for row in rows) else []),
                *_gap(bundle, "regulation"),
            ],
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def snp_in_tfbs_or_cns(
    snps: list[SnpPosition],
    config: RunnableConfig,
    species: str = "soybean",
    assembly: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Check whether SNPs fall in a promoter TF binding site (PlantRegMap FunTFBS) or a conserved element (phastCons), and whose promoter it is.

    Args:
        snps: SNP positions with labels (lead SNP ids).
        species: Registered species, e.g. soybean.
        assembly: Assembly of the positions; defaults to the canonical assembly.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        positions = _as(SnpPosition, snps)
        regions = [Region(label=snp.label or f"{snp.chrom}:{snp.pos}", chrom=snp.chrom, start=snp.pos, end=snp.pos, snp_pos=snp.pos, assembly=assembly) for snp in positions]
        rows = regulation.snp_in_tfbs_or_cns(bundle, regions, assembly)
        hit = {row.snp for row in rows}
        return _respond(
            config,
            "snp_in_tfbs_or_cns",
            f"snp_in_tfbs_or_cns {len(regions)} SNPs: {len(rows)} hits ({sum(1 for row in rows if row.kind == 'tfbs')} TFBS, {sum(1 for row in rows if row.kind == 'cns')} conserved elements)",
            rows,
            lambda row: (
                f"{row.snp} {row.chrom}:{row.pos} in {row.kind.upper()} {row.region_id} {row.start}-{row.end}"
                + (f" of TF {row.tf_gene_id} ({row.tf_family})" if row.tf_gene_id else "")
                + (f" -> {row.relation} of {row.gene_id}" if row.gene_id else " (no gene within 2 kb)")
            ),
            evidence=True,
            notes=[
                *([f"in no TFBS or conserved element: {', '.join(region.name for region in regions if region.name not in hit)}"] if len(hit) < len(regions) else []),
                *_gap(bundle, "tfbs_cns"),
            ],
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def get_pathways(
    gene_ids: list[str],
    config: RunnableConfig,
    trait: str | None = None,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Return the PMN SoyCyc and Plant Reactome pathways of each gene, marking pathways whose names match the trait (KEGG is not bundled).

    Args:
        gene_ids: Canonical-assembly gene ids.
        trait: Trait text; pathway names matching its keywords are marked.
        species: Registered species, e.g. soybean.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        profile = traits.map_trait(trait, bundle.species, bundle) if trait else None
        rows = pathways.get_pathways(bundle, gene_ids, profile)
        covered = {row.gene_id for row in rows}
        missing = [gene for gene in dict.fromkeys(gene_ids) if gene not in covered]
        return _respond(
            config,
            "get_pathways",
            f"get_pathways {len(covered)} of {len(set(gene_ids))} genes in {len({row.pathway_id for row in rows})} pathways"
            + (f"; {sum(1 for row in rows if row.matched)} match '{trait}'" if trait else ""),
            rows,
            lambda row: (
                f"{row.gene_id} [{row.database}] {row.pathway_id} {_clip(row.pathway_name, 70)}"
                + (f" match={','.join(row.matched)}" if row.matched else "")
                + (f" EC {','.join(row.ec[:2])}" if row.ec else "")
            ),
            evidence=True,
            notes=[*([f"in no bundled pathway: {', '.join(missing)}"] if missing else []), *_gap(bundle, "pathways")],
        )

    return await _run(work)


EXPRESSION_TOOLS: tuple[BaseTool, ...] = (trait_relevant_tissues, expression_profile, tissue_specificity)
NETWORK_TOOLS: tuple[BaseTool, ...] = (network_neighbors, coexpression_neighbors, seed_propagation)
REGULATION_TOOLS: tuple[BaseTool, ...] = (get_regulation, snp_in_tfbs_or_cns)
EXTENDED_TOOLS: tuple[BaseTool, ...] = (*EXPRESSION_TOOLS, *NETWORK_TOOLS, *REGULATION_TOOLS, get_pathways)
for _tool in EXTENDED_TOOLS:
    _tool.handle_tool_error = True
