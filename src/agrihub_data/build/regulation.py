"""Transcription factors, their targets and regulatory intervals (PlantTFDB, PlantRegMap).

Both resources use Gmax_275 Wm82.a2.v1 gene ids and ``Chr01``-style
chromosome names. Ids without a canonical-assembly gene (legacy or retired
models) are counted and dropped, never guessed.
"""

from collections import defaultdict
from collections.abc import Iterator
from typing import Any

from agrihub_data.build.context import BuildContext, tsv_rows
from agrihub_data.build.gff import features
from agrihub_data.registry import Source


def canonical_genes(ctx: BuildContext) -> set[str]:
    """Return the gene ids loaded for the canonical assembly."""
    rows = ctx.connection.execute("SELECT gene_id FROM genes WHERE assembly = ?", [ctx.registry.canonical_assembly]).fetchall()
    return {str(row[0]) for row in rows}


def build_planttfdb(ctx: BuildContext, source: Source) -> None:
    """Load TF families and the binding motifs PlantTFDB assigns to them."""
    known = canonical_genes(ctx)
    motifs: dict[str, list[tuple[str, str]]] = defaultdict(list)
    information = ctx.optional_file(source, "_TF_binding_motifs_information.txt")
    if information is not None:
        for fields in tsv_rows(information):
            if len(fields) >= 7 and fields[0] != "Gene_id":
                motifs[fields[0].strip()].append((fields[2].strip(), f"{fields[4].strip()} {fields[6].strip()}".strip()))
    families: dict[str, str] = {}
    for fields in tsv_rows(ctx.file(source, "_TF_list.txt.gz")):
        if len(fields) < 3 or fields[0] == "TF_ID":
            continue
        gene_id = fields[1].strip()
        if gene_id not in known:
            ctx.count(source.id, "unmapped_genes")
            continue
        families.setdefault(gene_id, fields[2].strip())
    base = ctx.base(source, ctx.registry.canonical_assembly)
    ctx.count(
        source.id,
        "tf",
        ctx.insert(
            "tf",
            (
                {
                    **base,
                    "gene_id": gene_id,
                    "family": family,
                    "motif_ids": [motif for motif, _ in motifs.get(gene_id, [])],
                    "motif_sources": [origin for _, origin in motifs.get(gene_id, [])],
                    "source_db": "PlantTFDB",
                }
                for gene_id, family in sorted(families.items())
            ),
        ),
    )
    ctx.count(source.id, "tf_with_motifs", sum(1 for gene_id in families if gene_id in motifs))


def build_plantregmap(ctx: BuildContext, source: Source) -> None:
    """Load TF-target links, promoter TFBS and conserved elements."""
    known = canonical_genes(ctx)
    canonical = ctx.registry.canonical_assembly
    base = ctx.base(source, canonical)

    def links() -> Iterator[dict[str, Any]]:
        seen: set[tuple[str, str, str]] = set()
        for fields in tsv_rows(ctx.file(source, "regulation_merged_*.txt")):
            if len(fields) < 3:
                continue
            tf_gene, target, evidence = fields[0].strip(), fields[2].strip(), (fields[4].strip() if len(fields) > 4 else "merged")
            if tf_gene not in known or target not in known:
                ctx.count(source.id, "links_with_unmapped_genes")
                continue
            key = (tf_gene, target, evidence)
            if key in seen:
                continue
            seen.add(key)
            yield {**base, "tf_gene_id": tf_gene, "target_gene_id": target, "evidence": evidence, "source_db": "PlantRegMap"}

    ctx.count(source.id, "regulation", ctx.insert("regulation", links()))

    def sites() -> Iterator[dict[str, Any]]:
        for feature in features(ctx.file(source, "TFBS_from_*_Gma.gff"), None):
            chrom = ctx.chrom(canonical, feature.seqid)
            tf_gene = feature.attributes.get("Name", "")
            if chrom is None or not ctx.in_bounds(canonical, chrom, feature.start, feature.end):
                ctx.count(source.id, "tfbs_unplaced")
                continue
            if tf_gene not in known:
                ctx.count(source.id, "tfbs_unmapped_tf")
                continue
            yield {
                **base,
                "kind": "tfbs",
                "region_id": feature.attributes.get("ID") or f"{tf_gene}@{chrom}:{feature.start}",
                "chrom": chrom,
                "start": feature.start,
                "end": feature.end,
                "strand": feature.strand,
                "tf_gene_id": tf_gene,
                "score": _number(feature.attributes.get("pvalue")),
                "source_db": "PlantRegMap FunTFBS",
            }

    ctx.count(source.id, "tfbs", ctx.insert("regulatory_regions", sites()))
    elements = ctx.optional_file(source, "CE_*.gtf.gz")
    if elements is not None:
        _load_conserved_elements(ctx, source, elements)


def _load_conserved_elements(ctx: BuildContext, source: Source, path: Any) -> None:
    canonical = ctx.registry.canonical_assembly
    connection = ctx.connection
    connection.execute(
        "CREATE TEMP TABLE ce_raw AS SELECT column0 AS seqid, column3 AS start, column4 AS stop, column5 AS lod, column6 AS strand, "
        "regexp_extract(column8, 'Element_ID \"([^\"]+)\"', 1) AS element FROM read_csv(?, delim = '\t', header = false, "
        "columns = {'column0': 'VARCHAR', 'column1': 'VARCHAR', 'column2': 'VARCHAR', 'column3': 'BIGINT', 'column4': 'BIGINT', "
        "'column5': 'DOUBLE', 'column6': 'VARCHAR', 'column7': 'VARCHAR', 'column8': 'VARCHAR'}, quote = '')",
        [str(path)],
    )
    names = [str(row[0]) for row in connection.execute("SELECT DISTINCT seqid FROM ce_raw ORDER BY 1").fetchall()]
    ctx.temp_table(
        "ce_chroms",
        {"seqid": "VARCHAR", "chrom": "VARCHAR", "length": "BIGINT"},
        (
            {"seqid": name, "chrom": chrom, "length": _length(ctx, canonical, chrom)}
            for name in names
            for chrom in [ctx.chrom(canonical, name)]
            if chrom is not None
        ),
    )
    inserted = connection.execute(
        """
        INSERT INTO regulatory_regions (species, assembly, source_version, kind, region_id, chrom, start, "end", strand, tf_gene_id, score, source_db)
        SELECT ?, ?, ?, 'cns', 'CE:' || r.element, c.chrom, r.start, r.stop, CASE WHEN r.strand IN ('+', '-') THEN r.strand ELSE '.' END,
               NULL, r.lod, 'PlantRegMap phastCons'
        FROM ce_raw AS r JOIN ce_chroms AS c ON c.seqid = r.seqid
        WHERE r.start >= 1 AND r.stop >= r.start AND (c.length IS NULL OR r.stop <= c.length)
        """,
        [ctx.species, canonical, ctx.source_version(source)],
    ).fetchone()
    ctx.count(source.id, "cns", int(inserted[0]) if inserted else 0)
    connection.execute("DROP TABLE ce_raw")
    connection.execute("DROP TABLE ce_chroms")


def _length(ctx: BuildContext, assembly: str, chrom: str) -> int | None:
    chromosome = ctx.registry.assembly(assembly).chromosome(chrom)
    return chromosome.length if chromosome else None


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None
