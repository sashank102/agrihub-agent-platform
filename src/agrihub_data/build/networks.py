"""Gene networks: STRING protein associations and ATTED-II co-expression.

STRING proteins map to canonical genes through their UniProt ORF names
(``GLYMA_18G092200``). The text-mining channel is dropped because literature
is scored separately; the combined score is recomputed from the remaining
channels as STRING does: each channel loses the prior ``p`` (0.041), the
channels combine as ``1 - prod(1 - s)``, and the prior is added back.
Pairs below ``STRING_MIN_SCORE`` are not stored.

ATTED-II ships one zip member per NCBI GeneID listing every other gene by
co-expression z-score, best first; the build keeps the first
``ATTED_TOP_PARTNERS`` partners at or above ``ATTED_MIN_Z``. GeneIDs map to
Wm82.a4.v1 locus tags through the xref file and then to the canonical
assembly.
"""

import io
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agrihub_data.build.context import BuildContext, open_text
from agrihub_data.build.idmap import canonical_map
from agrihub_data.registry import Source

STRING_PRIOR = 0.041
STRING_MIN_SCORE = 0.4
STRING_CHANNELS = ("neighborhood", "fusion", "cooccurence", "coexpression", "experimental", "database")
ATTED_TOP_PARTNERS = 50
ATTED_MIN_Z = 2.0
_LOCUS_TAG = re.compile(r"^GLYMA_(\d{2}G\d{6})(?:v\d+)?$")


def build_string(ctx: BuildContext, source: Source) -> None:
    """Load STRING links between canonical genes, scored without text mining."""
    canonical = ctx.registry.canonical_assembly
    links = ctx.file(source, "*.protein.links.detailed.*.txt.gz")
    aliases = ctx.file(source, "*.protein.aliases.*.txt.gz")
    connection = ctx.connection
    connection.execute(
        r"""
        CREATE TEMP TABLE string_map AS
        SELECT DISTINCT a.protein, g.gene_id
        FROM (
            SELECT column0 AS protein, 'Glyma.' || regexp_extract(column1, '^GLYMA_(\d{2}G\d{6})$', 1) AS gene_id
            FROM read_csv(?, delim = '\t', header = false, skip = 1, all_varchar = true, quote = '')
            WHERE regexp_matches(column1, '^GLYMA_\d{2}G\d{6}$')
        ) AS a
        JOIN genes AS g ON g.assembly = ? AND g.gene_id = a.gene_id
        """,
        [str(aliases), canonical],
    )
    mapped = connection.execute("SELECT count(DISTINCT protein), count(DISTINCT gene_id) FROM string_map").fetchone()
    ctx.count(source.id, "proteins_mapped", int(mapped[0]) if mapped else 0)
    ctx.count(source.id, "genes_mapped", int(mapped[1]) if mapped else 0)
    adjusted = " * ".join(f"(1 - greatest(0, ({channel} / 1000.0 - {STRING_PRIOR}) / (1 - {STRING_PRIOR})))" for channel in STRING_CHANNELS)
    channels = ", ".join(f"'{channel}': round({channel} / 1000.0, 3)" for channel in STRING_CHANNELS)
    inserted = connection.execute(
        f"""
        INSERT INTO edges (species, assembly, source_version, network, gene_a, gene_b, score, rank, channels, source_db)
        WITH links AS (
            SELECT protein1, protein2, {", ".join(STRING_CHANNELS)}, 1 - ({adjusted}) AS core
            FROM read_csv(?, delim = ' ', header = true)
            WHERE protein1 < protein2
        ),
        scored AS (
            SELECT least(a.gene_id, b.gene_id) AS gene_a, greatest(a.gene_id, b.gene_id) AS gene_b,
                   round(core + {STRING_PRIOR} * (1 - core), 4) AS score, to_json({{{channels}}}) AS channels
            FROM links
            JOIN string_map AS a ON a.protein = links.protein1
            JOIN string_map AS b ON b.protein = links.protein2
            WHERE a.gene_id <> b.gene_id
        )
        SELECT ?, ?, ?, 'string', gene_a, gene_b, max(score), NULL, arg_max(channels, score), 'STRING'
        FROM scored WHERE score >= ?
        GROUP BY gene_a, gene_b
        """,
        [str(links), ctx.species, canonical, ctx.source_version(source), STRING_MIN_SCORE],
    ).fetchone()
    ctx.count(source.id, "edges", int(inserted[0]) if inserted else 0)
    connection.execute("DROP TABLE string_map")


def build_atted(ctx: BuildContext, source: Source) -> None:
    """Load each canonical gene's top ATTED-II co-expression partners."""
    canonical = ctx.registry.canonical_assembly
    source_assembly = next((assembly for assembly in source.assemblies if assembly != canonical), canonical)
    mapping = canonical_map(ctx, source_assembly)
    genes: dict[str, str] = {}
    for fields in _xref_rows(ctx.file(source, "_gene_xref.tsv.zip")):
        if len(fields) < 2 or not fields[0].strip().isdigit():
            continue
        match = _LOCUS_TAG.match(fields[1].strip())
        if match is None:
            ctx.count(source.id, "xref_without_locus_tag")
            continue
        gene_id = mapping.get(f"Glyma.{match.group(1)}")
        if gene_id is None:
            ctx.count(source.id, "unmapped_source_genes")
            continue
        genes[fields[0].strip()] = gene_id
    ctx.count(source.id, "genes_mapped", len(set(genes.values())))
    archive = zipfile.ZipFile(ctx.file(source, ".subagging.z.d.zip"))
    base = ctx.base(source, canonical)

    def rows() -> Iterator[dict[str, Any]]:
        seen: set[str] = set()
        for info in archive.infolist():
            if info.is_dir():
                continue
            gene_a = genes.get(info.filename.rsplit("/", 1)[-1])
            if gene_a is None or gene_a in seen:
                continue
            seen.add(gene_a)
            for rank, (gene_b, z) in enumerate(_top_partners(archive, info, genes, gene_a), start=1):
                yield {**base, "network": "atted", "gene_a": gene_a, "gene_b": gene_b, "score": z, "rank": rank, "channels": None, "source_db": "ATTED-II"}

    ctx.count(source.id, "edges", ctx.insert("edges", rows()))


def _top_partners(archive: zipfile.ZipFile, info: zipfile.ZipInfo, genes: dict[str, str], gene_a: str) -> list[tuple[str, float]]:
    partners: list[tuple[str, float]] = []
    with archive.open(info) as handle:
        for raw in io.TextIOWrapper(handle, encoding="utf-8"):
            fields = raw.split("\t")
            if len(fields) < 2:
                continue
            try:
                z = float(fields[1])
            except ValueError:
                continue
            if z < ATTED_MIN_Z:
                break
            gene_b = genes.get(fields[0].strip())
            if gene_b is None or gene_b == gene_a or any(gene_b == known for known, _ in partners):
                continue
            partners.append((gene_b, z))
            if len(partners) >= ATTED_TOP_PARTNERS:
                break
    return partners


def _xref_rows(path: Path) -> Iterator[list[str]]:
    with open_text(path) as handle:
        for line in handle:
            if line.strip():
                yield line.rstrip("\n\r").split("\t")

