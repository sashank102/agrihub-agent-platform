"""Pathway membership from PMN (SoyCyc) and Plant Reactome.

SoyCyc names genes ``Glyma.08G181200`` (Gene-name) or ``GLYMA.08G181200``
(Gene-id); Plant Reactome uses Ensembl ``GLYMA_08G181200`` ids. Both are on
Wm82.a2.v1. Plant Reactome's file covers every species, so only rows of the
registry's scientific name are kept. KEGG is never bundled.
"""

import re
from collections.abc import Iterator
from typing import Any

from agrihub_data.build.context import BuildContext, tsv_rows
from agrihub_data.build.regulation import canonical_genes
from agrihub_data.registry import Source

_GENE = re.compile(r"^GLYMA[._](\d{2}G\d{6})$", re.IGNORECASE)


def soybean_gene(value: str) -> str | None:
    """Return ``Glyma.NNGNNNNNN`` for any spelling of a soybean gene id, or ``None``."""
    match = _GENE.match(value.strip())
    return f"Glyma.{match.group(1).upper()}" if match else None


def build_pmn_pathways(ctx: BuildContext, source: Source) -> None:
    """Load PMN pathway -> reaction -> gene rows."""
    known = canonical_genes(ctx)
    base = ctx.base(source, ctx.registry.canonical_assembly)

    def rows() -> Iterator[dict[str, Any]]:
        seen: set[tuple[str, str, str]] = set()
        for fields in tsv_rows(ctx.file(source, "*pathways*")):
            if len(fields) < 8 or fields[0] == "Pathway-id":
                continue
            gene_id = soybean_gene(fields[7]) or soybean_gene(fields[6])
            if gene_id is None or gene_id not in known:
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
                "source_db": "PMN SoyCyc",
            }

    ctx.count(source.id, "pathway_genes", ctx.insert("pathways", rows()))


def build_plant_reactome(ctx: BuildContext, source: Source) -> None:
    """Load Plant Reactome pathway membership of this species' genes."""
    known = canonical_genes(ctx)
    base = ctx.base(source, ctx.registry.canonical_assembly)
    species_name = ctx.registry.scientific_name

    def rows() -> Iterator[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        for fields in tsv_rows(ctx.file(source, "gene_ids_by_pathway_and_species.tab")):
            if len(fields) < 4 or fields[2].strip() != species_name:
                continue
            gene_id = soybean_gene(fields[3])
            if gene_id is None or gene_id not in known:
                ctx.count(source.id, "unmapped_genes")
                continue
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
                "source_gene_id": fields[3].strip(),
                "source_db": "Plant Reactome",
            }

    ctx.count(source.id, "pathway_genes", ctx.insert("pathways", rows()))
