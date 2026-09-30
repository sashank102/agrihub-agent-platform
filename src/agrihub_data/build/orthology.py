"""Ortholog calls from Ensembl Compara and PLAZA integrative orthology.

Both files are large, so they are filtered to the target species inside
DuckDB. Every row is keyed by a canonical-assembly gene; PLAZA's Wm82.a4 ids
are mapped through the a4 GFF ancestors first and pangenes second.
"""

from agrihub_data.build.context import BuildContext, BuildError
from agrihub_data.registry import Source

ENSEMBL_TARGETS = {"arabidopsis_thaliana": ("arabidopsis", "TAIR10")}
PLAZA_TARGETS = {"ath": ("arabidopsis", "TAIR10")}
PLAZA_METHODS = ("TROG", "BHIF", "ORTHO", "anchor_point")
_ENSEMBL_GENE = r"^GLYMA_(\d{2}G\d{6})$"


def build_ensembl_compara(ctx: BuildContext, source: Source) -> None:
    """Load Compara orthologs between soybean and the target species."""
    canonical = ctx.registry.canonical_assembly
    version = ctx.source_version(source)
    files = ctx.files(source)
    if not files:
        raise BuildError(f"{source.id}: nothing fetched")
    ctx.connection.execute(
        "CREATE TEMP TABLE compara_pairs (gene VARCHAR, target VARCHAR, target_species VARCHAR, "
        "homology_type VARCHAR, identity VARCHAR, high_confidence VARCHAR, homology_id VARCHAR)"
    )
    targets = sorted(ENSEMBL_TARGETS)
    placeholders = ", ".join("?" for _ in targets)
    for path in files:
        ctx.connection.execute(
            f"""
            INSERT INTO compara_pairs
            WITH raw AS (
                SELECT * FROM read_csv(?, delim = '\t', header = true, all_varchar = true, quote = '')
            )
            SELECT gene_stable_id, homology_gene_stable_id, homology_species, homology_type,
                   identity, is_high_confidence, homology_id
            FROM raw
            WHERE species = 'glycine_max' AND homology_species IN ({placeholders})
              AND homology_type LIKE 'ortholog%'
            UNION ALL
            SELECT homology_gene_stable_id, gene_stable_id, species, homology_type,
                   homology_identity, is_high_confidence, homology_id
            FROM raw
            WHERE homology_species = 'glycine_max' AND species IN ({placeholders})
              AND homology_type LIKE 'ortholog%'
            """,
            [str(path), *targets, *targets],
        )
    for ensembl_species, (species, assembly) in ENSEMBL_TARGETS.items():
        inserted = ctx.connection.execute(
            """
            INSERT INTO orthologs
            SELECT DISTINCT ?, ?, ?, g.gene_id, p.gene, ?, ?, p.target, 'compara',
                   replace(p.homology_type, 'ortholog_', ''), TRY_CAST(p.identity AS DOUBLE),
                   p.high_confidence = '1', 'Ensembl Compara'
            FROM (
                SELECT DISTINCT ON (homology_id) * FROM compara_pairs WHERE target_species = ?
            ) AS p
            JOIN genes AS g
              ON g.assembly = ? AND g.gene_id = 'Glyma.' || regexp_extract(p.gene, ?, 1)
            """,
            [
                ctx.species,
                canonical,
                version,
                species,
                assembly,
                ensembl_species,
                canonical,
                _ENSEMBL_GENE,
            ],
        ).fetchone()
        ctx.count(source.id, f"orthologs:{species}", int(inserted[0]) if inserted else 0)
    ctx.connection.execute("DROP TABLE compara_pairs")


def build_plaza_orthology(ctx: BuildContext, source: Source) -> None:
    """Load PLAZA integrative orthology, one row per supporting method."""
    canonical = ctx.registry.canonical_assembly
    plaza_assembly = next((assembly for assembly in source.assemblies if assembly.startswith("Wm82")), None)
    if plaza_assembly is None:
        raise BuildError(f"{source.id} must name the soybean assembly its gene ids use")
    path = ctx.file(source, ".tsv.gz")
    ctx.connection.execute(
        """
        CREATE TEMP TABLE plaza_map AS
        WITH ancestor AS (
            SELECT DISTINCT from_id AS source_gene, to_id AS gene_id
            FROM id_map WHERE relation = 'ancestor' AND assembly = ? AND to_assembly = ?
        ),
        pangene AS (
            SELECT DISTINCT a.from_id AS source_gene, b.from_id AS gene_id
            FROM id_map AS a JOIN id_map AS b ON a.to_id = b.to_id
            WHERE a.relation = 'pangene_member' AND b.relation = 'pangene_member'
              AND a.assembly = ? AND b.assembly = ?
        )
        SELECT * FROM ancestor
        UNION ALL
        SELECT * FROM pangene WHERE source_gene NOT IN (SELECT source_gene FROM ancestor)
        """,
        [plaza_assembly, canonical, plaza_assembly, canonical],
    )
    targets = sorted(PLAZA_TARGETS)
    ctx.connection.execute(
        f"""
        CREATE TEMP TABLE plaza_pairs AS
        SELECT * FROM read_csv(
            ?, delim = '\t', header = false, comment = '#', quote = '', escape = '', auto_detect = false,
            columns = {{'query_gene': 'VARCHAR', 'query_species': 'VARCHAR',
                        'ortholog_gene': 'VARCHAR', 'ortholog_species': 'VARCHAR',
                        'TROG': 'VARCHAR', 'BHIF': 'VARCHAR', 'ORTHO': 'VARCHAR',
                        'anchor_point': 'VARCHAR'}}
        )
        WHERE ortholog_species IN ({", ".join("?" for _ in targets)})
        """,
        [str(path), *targets],
    )
    unmapped = ctx.connection.execute(
        "SELECT count(DISTINCT query_gene) FROM plaza_pairs "
        "WHERE query_gene NOT IN (SELECT source_gene FROM plaza_map)"
    ).fetchone()
    ctx.count(source.id, "unmapped_source_genes", int(unmapped[0]) if unmapped else 0)
    for plaza_species, (species, assembly) in PLAZA_TARGETS.items():
        for method in PLAZA_METHODS:
            inserted = ctx.connection.execute(
                f"""
                INSERT INTO orthologs
                SELECT DISTINCT ?, ?, ?, m.gene_id, p.query_gene, ?, ?, p.ortholog_gene,
                       ?, NULL, NULL, NULL, 'PLAZA 5.0'
                FROM plaza_pairs AS p JOIN plaza_map AS m ON m.source_gene = p.query_gene
                WHERE p.ortholog_species = ? AND p."{method}" = '1'
                """,
                [
                    ctx.species,
                    canonical,
                    ctx.source_version(source),
                    species,
                    assembly,
                    f"plaza_{method}",
                    plaza_species,
                ],
            ).fetchone()
            ctx.count(source.id, f"{species}:{method}", int(inserted[0]) if inserted else 0)
    ctx.connection.execute("DROP TABLE plaza_pairs")
    ctx.connection.execute("DROP TABLE plaza_map")
