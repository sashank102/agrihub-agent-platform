"""Ortholog calls from Ensembl Compara and PLAZA integrative orthology.

Both files are large, so they are filtered to the target species inside
DuckDB. Every row is keyed by a canonical-assembly gene; PLAZA dicots'
soybean Wm82.a4 ids are mapped through the a4 GFF ancestors first and
pangenes second, PLAZA monocots' ids through the registry's gene namespaces.

An ``ensembl_compara`` source names its Ensembl genome in
``params.ensembl_species`` (``glycine_max``, ``oryza_sativa``); Ensembl gene
ids become canonical ids through the registry's gene namespaces.
"""

from agrihub_data.build.context import BuildContext, BuildError
from agrihub_data.build.resolve import GeneResolver
from agrihub_data.registry import Source

ENSEMBL_TARGETS = {"arabidopsis_thaliana": ("arabidopsis", "TAIR10")}
PLAZA_TARGETS = {"ath": ("arabidopsis", "TAIR10")}
PLAZA_METHODS = ("TROG", "BHIF", "ORTHO", "anchor_point")


def build_ensembl_compara(ctx: BuildContext, source: Source) -> None:
    """Load Compara orthologs between the bundle species and the target species."""
    canonical = ctx.registry.canonical_assembly
    version = ctx.source_version(source)
    files = ctx.files(source)
    if not files:
        raise BuildError(f"{source.id}: nothing fetched")
    genome = str(source.params.get("ensembl_species") or "")
    if not genome:
        raise BuildError(f"{source.id}: params.ensembl_species names the Ensembl genome")
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
            WHERE species = ? AND homology_species IN ({placeholders})
              AND homology_type LIKE 'ortholog%'
            UNION ALL
            SELECT homology_gene_stable_id, gene_stable_id, species, homology_type,
                   homology_identity, is_high_confidence, homology_id
            FROM raw
            WHERE homology_species = ? AND species IN ({placeholders})
              AND homology_type LIKE 'ortholog%'
            """,
            [str(path), genome, *targets, genome, *targets],
        )
    resolver = GeneResolver.load(ctx)
    ensembl_ids = [str(row[0]) for row in ctx.connection.execute("SELECT DISTINCT gene FROM compara_pairs").fetchall()]
    mapped = {gene: resolver.get(gene) for gene in ensembl_ids}
    ctx.count(source.id, "unmapped_genes", sum(1 for gene in mapped.values() if gene is None))
    ctx.temp_table(
        "compara_genes",
        {"ensembl_id": "VARCHAR", "gene_id": "VARCHAR"},
        ({"ensembl_id": ensembl, "gene_id": gene} for ensembl, gene in mapped.items() if gene is not None),
    )
    for ensembl_species, (species, assembly) in ENSEMBL_TARGETS.items():
        inserted = ctx.connection.execute(
            """
            INSERT INTO orthologs
            SELECT DISTINCT ?, ?, ?, m.gene_id, p.gene, ?, ?, p.target, 'compara',
                   replace(p.homology_type, 'ortholog_', ''), TRY_CAST(p.identity AS DOUBLE),
                   p.high_confidence = '1', 'Ensembl Compara'
            FROM (
                SELECT DISTINCT ON (homology_id) * FROM compara_pairs WHERE target_species = ?
            ) AS p
            JOIN compara_genes AS m ON m.ensembl_id = p.gene
            """,
            [
                ctx.species,
                canonical,
                version,
                species,
                assembly,
                ensembl_species,
            ],
        ).fetchone()
        ctx.count(source.id, f"orthologs:{species}", int(inserted[0]) if inserted else 0)
    ctx.connection.execute("DROP TABLE compara_pairs")
    ctx.connection.execute("DROP TABLE compara_genes")


def build_plaza_orthology(ctx: BuildContext, source: Source) -> None:
    """Load PLAZA integrative orthology, one row per supporting method.

    The source's first registered assembly is the one its gene ids are on.
    Ids on the canonical assembly (rice RAP, maize v5, sorghum Sobic) resolve
    through the registry's gene namespaces; ids on another assembly (soybean
    Wm82.a4 in PLAZA dicots) map through GFF ancestors first and pangenes
    second.
    """
    canonical = ctx.registry.canonical_assembly
    own = {assembly.id for assembly in ctx.registry.assemblies}
    plaza_assembly = next((assembly for assembly in source.assemblies if assembly in own), None)
    if plaza_assembly is None:
        raise BuildError(f"{source.id} must name the {ctx.species} assembly its gene ids use")
    path = ctx.file(source, ".tsv.gz")
    if plaza_assembly != canonical:
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
    if plaza_assembly == canonical:
        resolver = GeneResolver.load(ctx)
        query_genes = [str(row[0]) for row in ctx.connection.execute("SELECT DISTINCT query_gene FROM plaza_pairs").fetchall()]
        ctx.temp_table(
            "plaza_map",
            {"source_gene": "VARCHAR", "gene_id": "VARCHAR"},
            ({"source_gene": gene, "gene_id": mapped} for gene in query_genes if (mapped := resolver.get(gene))),
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
