"""Pathway membership from PMN (SoyCyc, OryzaCyc, CornCyc, SorghumBicolorCyc) and Plant Reactome.

PMN names genes in a Gene-name column (``Glyma.08G181200``,
``Os02g0194700``, ``Sobic.010G193600``) and an upper-case Gene-id column;
Plant Reactome uses Ensembl ids (``GLYMA_08G181200``, ``SORBI_3001G021900``,
``Zm00001eb433310``) or, for rice, UniProt accessions. Every id is resolved
through the registry's gene namespaces and id_map synonyms
(:mod:`agrihub_data.build.resolve`). Plant Reactome's file covers every
species, so only rows of the registry's scientific name (or the source's
``params.species_names``) are kept. KEGG is never bundled.

``params`` of a ``plant_reactome`` source:

- ``species_names``: species labels to keep, in preference order (maize v5
  rows are labelled ``Zea mays ver5``).
- ``uniprot_file``: an Ensembl ``*.uniprot.tsv.gz`` xref file of the same
  source that maps UniProt accessions to gene ids.
"""

from collections import defaultdict
from collections.abc import Iterator
from typing import Any

from agrihub_data.build.context import BuildContext, tsv_rows
from agrihub_data.build.resolve import GeneResolver
from agrihub_data.registry import Source


def build_pmn_pathways(ctx: BuildContext, source: Source) -> None:
    """Load PMN pathway -> reaction -> gene rows."""
    resolver = GeneResolver.load(ctx)
    base = ctx.base(source, ctx.registry.canonical_assembly)
    label = str(source.params.get("source_db") or "PMN")

    def rows() -> Iterator[dict[str, Any]]:
        seen: set[tuple[str, str, str]] = set()
        for fields in tsv_rows(ctx.file(source, "*pathways*")):
            if len(fields) < 8 or fields[0] == "Pathway-id":
                continue
            gene_id = resolver.get(fields[7]) or resolver.get(fields[6])
            if gene_id is None:
                ctx.count(source.id, "unmapped_genes")
                continue
            key = (gene_id, fields[0].strip(), fields[2].strip())
            if key in seen:
                continue
            seen.add(key)
            yield {
                **base,
                "gene_id": gene_id,
                "pathway_id": fields[0].strip(),
                "pathway_name": fields[1].strip(),
                "reaction_id": fields[2].strip() or None,
                "ec": fields[3].strip().removeprefix("EC-") or None,
                "source_gene_id": fields[6].strip(),
                "source_db": label,
            }

    ctx.count(source.id, "pathway_genes", ctx.insert("pathways", rows()))


def build_plant_reactome(ctx: BuildContext, source: Source) -> None:
    """Load Plant Reactome pathway membership of this species' genes."""
    resolver = GeneResolver.load(ctx)
    base = ctx.base(source, ctx.registry.canonical_assembly)
    names = [str(name) for name in source.params.get("species_names") or [ctx.registry.scientific_name]]
    uniprot = _uniprot_map(ctx, source, resolver)
    kept = next(
        (
            name
            for name in names
            if any(len(fields) >= 4 and fields[2].strip() == name for fields in tsv_rows(ctx.file(source, "gene_ids_by_pathway_and_species.tab")))
        ),
        names[0],
    )
    if len(names) > 1:
        ctx.count(source.id, f"species:{kept}")

    def rows() -> Iterator[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        for fields in tsv_rows(ctx.file(source, "gene_ids_by_pathway_and_species.tab")):
            if len(fields) < 4 or fields[2].strip() != kept:
                continue
            raw = fields[3].strip()
            genes = [gene] if (gene := resolver.get(raw)) else sorted(uniprot.get(raw, ()))
            if not genes:
                ctx.count(source.id, "unmapped_genes")
                continue
            for gene_id in genes:
                key = (gene_id, fields[0].strip())
                if key in seen:
                    continue
                seen.add(key)
                yield {
                    **base,
                    "gene_id": gene_id,
                    "pathway_id": fields[0].strip(),
                    "pathway_name": fields[1].strip(),
                    "reaction_id": None,
                    "ec": None,
                    "source_gene_id": raw,
                    "source_db": "Plant Reactome",
                }

    ctx.count(source.id, "pathway_genes", ctx.insert("pathways", rows()))


def _uniprot_map(ctx: BuildContext, source: Source, resolver: GeneResolver) -> dict[str, set[str]]:
    """Return UniProt accession -> canonical genes from an Ensembl ``uniprot.tsv`` xref file."""
    name = source.params.get("uniprot_file")
    if not name:
        return {}
    found: dict[str, set[str]] = defaultdict(set)
    for fields in tsv_rows(ctx.file(source, str(name))):
        if len(fields) < 4 or fields[0] == "gene_stable_id":
            continue
        gene = resolver.get(fields[0])
        if gene is not None:
            found[fields[3].strip()].add(gene)
    ctx.count(source.id, "uniprot_accessions", len(found))
    return dict(found)
