"""Gene anchors that lift positions from a non-reference assembly to its ``lift_to`` assembly.

An anchor is a gene on the source assembly (Lee) and a gene on the target
(Wm82.a2) that one pangene links one-to-one: the pangene has exactly one
member on each assembly, and neither gene belongs to another pangene. Pairs
on different chromosome numbers are dropped as likely paralog calls.
"""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agrihub_data.build.context import BuildContext, BuildError, tsv_rows
from agrihub_data.registry import Source


def build_lis_lift_anchors(ctx: BuildContext, source: Source) -> None:
    """Load ``lift_anchors`` for the one assembly the source names."""
    if len(source.assemblies) != 1:
        raise BuildError(f"{source.id} must name exactly one assembly")
    from_assembly = source.assemblies[0]
    target = ctx.registry.assembly(from_assembly).lift_to
    if target is None:
        raise BuildError(f"{source.id}: {from_assembly} has no lift_to assembly in the registry")
    bed = ctx.file(source, ".gene_models_main.bed.gz")
    genes = _bed_genes(ctx, source, from_assembly, bed)
    ctx.count(source.id, "source_genes", len(genes))
    ctx.temp_table(
        "lift_source_genes",
        {"gene_id": "VARCHAR", "chrom": "VARCHAR", "start": "BIGINT", "stop": "BIGINT", "strand": "VARCHAR"},
        ({**gene, "stop": gene.pop("end")} for gene in genes.values()),
    )
    try:
        rows = ctx.connection.execute(
            """
            WITH source_members AS (
                SELECT from_id, to_id FROM id_map
                WHERE relation = 'pangene_member' AND assembly = ?
            ),
            target_members AS (
                SELECT from_id, to_id FROM id_map
                WHERE relation = 'pangene_member' AND assembly = ?
            ),
            source_single AS (
                SELECT to_id AS pangene, min(from_id) AS gene FROM source_members
                GROUP BY to_id HAVING count(*) = 1
            ),
            target_single AS (
                SELECT to_id AS pangene, min(from_id) AS gene FROM target_members
                GROUP BY to_id HAVING count(*) = 1
            ),
            source_unique AS (SELECT from_id FROM source_members GROUP BY from_id HAVING count(*) = 1),
            target_unique AS (SELECT from_id FROM target_members GROUP BY from_id HAVING count(*) = 1)
            SELECT s.pangene, l.gene_id, l.chrom, l.start, l.stop, l.strand,
                   g.gene_id, g.chrom, g.start, g."end", g.strand
            FROM source_single AS s
            JOIN target_single AS t USING (pangene)
            JOIN source_unique AS su ON su.from_id = s.gene
            JOIN target_unique AS tu ON tu.from_id = t.gene
            JOIN lift_source_genes AS l ON l.gene_id = s.gene
            JOIN genes AS g ON g.assembly = ? AND g.gene_id = t.gene
            ORDER BY l.chrom, l.start
            """,
            [from_assembly, target, target],
        ).fetchall()
    finally:
        ctx.connection.execute("DROP TABLE lift_source_genes")
    base = ctx.base(source, target)

    def anchors() -> Iterator[dict[str, Any]]:
        for pangene, from_gene, from_chrom, from_start, from_end, from_strand, gene_id, chrom, start, end, strand in rows:
            if from_chrom != chrom:
                ctx.count(source.id, "pairs_on_other_chromosome")
                continue
            yield {
                **base,
                "from_assembly": from_assembly,
                "from_gene_id": from_gene,
                "from_chrom": from_chrom,
                "from_start": from_start,
                "from_end": from_end,
                "from_strand": from_strand,
                "gene_id": gene_id,
                "chrom": chrom,
                "start": start,
                "end": end,
                "strand": strand,
                "pangene": pangene,
                "source_db": "LIS pangenes",
            }

    ctx.count(source.id, "anchors", ctx.insert("lift_anchors", anchors()))


def _bed_genes(ctx: BuildContext, source: Source, assembly: str, path: Path) -> dict[str, dict[str, Any]]:
    """Return gene spans from a LIS ``gene_models_main.bed``: the union of each gene's transcripts.

    Column 7 holds the full gene id (``glyma.Lee.gnm2.ann1.Gm_00001``); the
    pangene table uses the part after the annotation prefix.
    """
    registered = ctx.registry.assembly(assembly)
    prefix = re.compile(rf"^{re.escape(ctx.registry.abbrev)}\.[^.]+\.gnm\d+\.ann\d+\.")
    genes: dict[str, dict[str, Any]] = {}
    for fields in tsv_rows(path):
        if len(fields) < 7:
            ctx.count(source.id, "bed_rows_short")
            continue
        chrom = ctx.chrom(assembly, fields[0])
        if chrom is None or registered.chromosome(chrom) is None:
            ctx.count(source.id, "genes_off_chromosomes")
            continue
        gene_id = prefix.sub("", fields[6].strip())
        start, end = int(fields[1]) + 1, int(fields[2])
        if not ctx.in_bounds(assembly, chrom, start, end):
            ctx.count(source.id, "genes_out_of_bounds")
            continue
        gene = genes.get(gene_id)
        if gene is None:
            genes[gene_id] = {"gene_id": gene_id, "chrom": chrom, "start": start, "end": end, "strand": fields[5].strip() or "."}
        elif gene["chrom"] == chrom:
            gene["start"] = min(gene["start"], start)
            gene["end"] = max(gene["end"], end)
    return genes
