"""NCBI Gene bulk files: gene_info, gene2pubmed and gene2go for the bundle species and Arabidopsis.

The files cover every organism, so DuckDB filters them by tax_id while
reading. Soybean locus tags (``GLYMA_18G092200v4``) name a Wm82.a4 gene; they
are mapped to the canonical assembly through the a4 GFF ancestors, or kept
as the same id when the canonical bundle has it. Arabidopsis locus tags are
AGIs. ``genes_per_pmid`` is counted over the whole gene2pubmed file.
"""

import re
from typing import Any

from agrihub_data.build.context import BuildContext, BuildError
from agrihub_data.registry import Source

ARABIDOPSIS_TAXON = 3702
ARABIDOPSIS = ("arabidopsis", "TAIR10")
_SOY_LOCUS = re.compile(r"^GLYMA_(\d{2})G(\d{6})(?:v(\d))?$")
_AGI = re.compile(r"^AT[1-5CM]G\d{5}$", re.IGNORECASE)
_LOCUS_ASSEMBLY = {None: "Wm82.a2.v1", "2": "Wm82.a2.v1", "4": "Wm82.a4.v1", "6": "Wm82.a6.v1"}
_INFO_COLUMNS = (
    "tax_id",
    "gene",
    "symbol",
    "locus_tag",
    "synonyms",
    "db_xrefs",
    "chromosome",
    "map_location",
    "description",
    "gene_type",
    "nomenclature_symbol",
    "nomenclature_name",
    "nomenclature_status",
    "designations",
    "modified",
    "feature_type",
)


def build_ncbi_gene(ctx: BuildContext, source: Source) -> None:
    """Load NCBI gene records, their PubMed links and GO annotations."""
    taxa = {ctx.registry.taxon_id: ctx.species, ARABIDOPSIS_TAXON: ARABIDOPSIS[0]}
    version = ctx.source_version(source)
    records = _gene_records(ctx, source, taxa, version)
    ctx.count(source.id, "genes", ctx.insert("ncbi_genes", records))
    ctx.count(source.id, "genes_mapped", sum(1 for row in records if row["gene_id"]))
    ctx.connection.execute(
        "CREATE TEMP TABLE ncbi_map AS SELECT DISTINCT species, assembly, ncbi_gene_id, gene_id FROM ncbi_genes"
    )
    try:
        _load_pubmed(ctx, source, version, list(taxa))
        _load_go(ctx, source, version, list(taxa))
    finally:
        ctx.connection.execute("DROP TABLE ncbi_map")


def _gene_records(ctx: BuildContext, source: Source, taxa: dict[int, str], version: str) -> list[dict[str, Any]]:
    path = ctx.file(source, "gene_info.gz")
    placeholders = ", ".join("?" for _ in taxa)
    rows = ctx.connection.execute(
        f"""
        SELECT tax_id, gene, symbol, locus_tag, synonyms, description, gene_type, designations
        FROM read_csv(?, delim = '\t', header = true, quote = '', escape = '', all_varchar = true,
                      names = [{", ".join(f"'{name}'" for name in _INFO_COLUMNS)}])
        WHERE TRY_CAST(tax_id AS INTEGER) IN ({placeholders})
        """,
        [str(path), *taxa],
    ).fetchall()
    if not rows:
        raise BuildError(f"{source.id}: gene_info has no rows for tax_id {sorted(taxa)}")
    resolve = _soybean_resolver(ctx)
    records = []
    for tax_id, gene, symbol, locus_tag, synonyms, description, gene_type, designations in rows:
        species = taxa[int(tax_id)]
        locus = _value(locus_tag)
        if species == ARABIDOPSIS[0]:
            assembly = ARABIDOPSIS[1]
            gene_id = locus.upper() if locus and _AGI.match(locus) else None
            mapping = "locus_tag" if gene_id else "unmapped"
        else:
            assembly, gene_id, mapping = resolve(locus)
        records.append(
            {
                "species": species,
                "assembly": assembly,
                "source_version": version,
                "ncbi_gene_id": str(gene),
                "tax_id": int(tax_id),
                "gene_id": gene_id,
                "mapping": mapping,
                "symbol": str(symbol),
                "locus_tag": locus,
                "synonyms": _split(synonyms),
                "description": _value(description),
                "designations": _split(designations),
                "gene_type": _value(gene_type),
                "source_db": "NCBI Gene",
            }
        )
    return records


def _soybean_resolver(ctx: BuildContext) -> Any:
    canonical = ctx.registry.canonical_assembly
    ancestors = {
        (str(assembly), str(from_id)): str(to_id)
        for assembly, from_id, to_id in ctx.connection.execute(
            "SELECT assembly, from_id, to_id FROM id_map WHERE relation = 'ancestor' AND to_assembly = ?",
            [canonical],
        ).fetchall()
    }
    canonical_genes = {
        str(row[0]) for row in ctx.connection.execute("SELECT gene_id FROM genes WHERE assembly = ?", [canonical]).fetchall()
    }

    def resolve(locus: str | None) -> tuple[str, str | None, str]:
        match = _SOY_LOCUS.match(locus or "")
        if match is None:
            return canonical, None, "unmapped"
        gene_id = f"Glyma.{match.group(1)}G{match.group(2)}"
        assembly = _LOCUS_ASSEMBLY.get(match.group(3), canonical)
        if assembly == canonical:
            return (canonical, gene_id, "locus_tag") if gene_id in canonical_genes else (canonical, None, "unmapped")
        ancestor = ancestors.get((assembly, gene_id))
        if ancestor is not None:
            return canonical, ancestor, "ancestor"
        if gene_id in canonical_genes:
            return canonical, gene_id, "same_id"
        return assembly, None, "unmapped"

    return resolve


def _load_pubmed(ctx: BuildContext, source: Source, version: str, taxa: list[int]) -> None:
    path = str(ctx.file(source, "gene2pubmed.gz"))
    placeholders = ", ".join("?" for _ in taxa)
    inserted = ctx.connection.execute(
        f"""
        INSERT INTO ncbi_gene_pubmed
        WITH raw AS (
            SELECT TRY_CAST(tax_id AS INTEGER) AS tax_id, gene, pmid
            FROM read_csv(?, delim = '\t', header = true, quote = '', all_varchar = true,
                          names = ['tax_id', 'gene', 'pmid'])
        ),
        hubs AS (SELECT pmid, count(DISTINCT gene) AS genes FROM raw GROUP BY pmid)
        SELECT DISTINCT m.species, m.assembly, ?, raw.gene, m.gene_id, raw.pmid, hubs.genes, 'NCBI gene2pubmed'
        FROM raw
        JOIN hubs USING (pmid)
        JOIN ncbi_map AS m ON m.ncbi_gene_id = raw.gene
        WHERE raw.tax_id IN ({placeholders})
        """,
        [path, version, *taxa],
    ).fetchone()
    ctx.count(source.id, "pubmed_links", int(inserted[0]) if inserted else 0)


def _load_go(ctx: BuildContext, source: Source, version: str, taxa: list[int]) -> None:
    path = str(ctx.file(source, "gene2go.gz"))
    placeholders = ", ".join("?" for _ in taxa)
    inserted = ctx.connection.execute(
        f"""
        INSERT INTO ncbi_gene_go
        WITH raw AS (
            SELECT gene, go_id, evidence, qualifier, go_term, pubmed, category
            FROM read_csv(?, delim = '\t', header = true, quote = '', all_varchar = true,
                          names = ['tax_id', 'gene', 'go_id', 'evidence', 'qualifier', 'go_term', 'pubmed', 'category'])
            WHERE TRY_CAST(tax_id AS INTEGER) IN ({placeholders})
        )
        SELECT m.species, m.assembly, ?, raw.gene, m.gene_id, raw.go_id, nullif(raw.go_term, '-'), raw.evidence,
               nullif(raw.qualifier, '-'), nullif(raw.category, '-'),
               list_filter(string_split(coalesce(raw.pubmed, '-'), '|'), pmid -> pmid <> '-' AND pmid <> ''),
               'NCBI gene2go'
        FROM raw
        JOIN ncbi_map AS m ON m.ncbi_gene_id = raw.gene
        """,
        [path, *taxa, version],
    ).fetchone()
    ctx.count(source.id, "go_annotations", int(inserted[0]) if inserted else 0)


def _value(text: Any) -> str | None:
    value = str(text or "").strip()
    return None if value in {"", "-"} else value


def _split(text: Any) -> list[str]:
    value = _value(text)
    return [item.strip() for item in value.split("|") if item.strip() and item.strip() != "-"] if value else []
