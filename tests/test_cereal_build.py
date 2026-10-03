"""Rice, maize and sorghum parsers on small synthetic files, built through the real registry and build path."""

import gzip
import io
import json
import sqlite3
import tarfile
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
import yaml

from agrihub_data.availability import clear_cache, domain_status
from agrihub_data.build import build
from agrihub_data.build.chain import ChainLiftover
from agrihub_data.bundle import close_bundles, open_bundle
from agrihub_data.fetch import Manifest, disk_budget, sha256_file
from agrihub_data.paths import species_paths
from agrihub_data.query.ids import map_gene_ids
from agrihub_data.registry import (
    load_species,
    normalize_chrom,
    register_species,
    unregister_species,
)
from agrihub_data.verify import verify

GWAS_HEADER = (
    "#GaP_id\tChr\tPos\tRef ver\tStudyId\tEnvironment\tSampling spot\tSampling year\tSampling condition\t"
    "Species/Population\tSample size\tTissue\tTrait\tTrait accession\tPenotype type\tGenotype technology\t"
    "GWAS model\tReported location\tEffect allele\tEffect allele frequence\tP-value\tR2(%)\tReported gene(S)\t"
    "Gene symbol\tQTL\tPMID\tJournal\tTitle\tFirst author\tCorresponding author\tPublished time"
)


def _gwas_row(gap: str, chrom: str, pos: int, ref: str, study: str, trait: str, term: str, p_value: str, genes: str) -> str:
    fields = [gap, chrom, str(pos), ref, study, *[""] * 7, trait, term, *[""] * 6, p_value, "", genes, *[""] * 8]
    return "\t".join(fields)


def _gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


def _tar(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in members.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _stage(species: str, data_dir: Path, files: dict[str, dict[str, bytes]]) -> None:
    """Register only the sources in ``files`` and record their bytes as fetched."""
    registry = load_species(species)
    sources = [source for source in registry.sources if source.id in files]
    register_species(registry.model_copy(update={"sources": sources}))
    paths = species_paths(species, data_dir)
    manifest = Manifest(paths.manifest, species)
    for source in sources:
        for name, content in files[source.id].items():
            target = paths.source_dir(source.id) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            manifest.record(
                f"{source.id}/{name}",
                {"source_id": source.id, "url": "test", "path": name, "status": "present", "size": len(content),
                 "sha256": sha256_file(target), "version": source.version, "license": source.license},
            )


@pytest.fixture
def species_dir(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path
    for species in ("rice", "maize", "sorghum"):
        unregister_species(species)
    close_bundles()
    clear_cache()


RICE_LOCUS = """##gff-version 3
chr01\tirgsp1_locus\tgene\t38382382\t38385504\t.\t+\t.\tID=Os01g0883800;Name=Os01g0883800;Note=Gibberellin 20 oxidase 2%2C regulation of plant height
chr01\tirgsp1_locus\tgene\t1000\t2000\t.\t-\t.\tID=Os01g0100100;Name=Os01g0100100;Note=Conserved hypothetical protein
"""
RICE_TRANSCRIPTS = """##gff-version 3
chr01\tirgsp1_rep\tmRNA\t38382382\t38385504\t.\t+\t.\tID=Os01t0883800-02;Locus_id=Os01g0883800
chr01\tirgsp1_rep\tCDS\t38382500\t38383000\t.\t+\t0\tParent=Os01t0883800-02
chr01\tirgsp1_rep\tfive_prime_UTR\t38382382\t38382499\t.\t+\t.\tParent=Os01t0883800-02
chr01\tirgsp1_rep\tmRNA\t1000\t2000\t.\t-\t.\tID=Os01t0100100-01;Locus_id=Os01g0100100
"""
ORYZABASE = (
    "Trait Gene Id\tCGSNL Gene Symbol\tGene symbol synonym(s)\tCGSNL Gene Name\tGene name synonym(s)\tGene name synonym(s)\t"
    "Protein Name\tAllele\tChromosome No.\tExplanation Ja\tExplanation En\tTrait Class\tRAP ID\tMSU ID\tGramene ID\tArm\t"
    "Locate(cM)\tGene Ontology\tTrait Ontology\tPlant Ontology\r\n"
    "470\tSD1\tsd1, OsGA20ox2\tSEMIDWARF 1\t低脚烏尖\tsemidwarf-1\tGA20OX2\tsd1\t1\t半矮性\tGreen revolution gene.\t\t"
    "Os01g0883800\tLOC_Os01g66100.1\t\t\t73.0\t\tTO:0000207 - plant height, TO:0000576 - stem length\t\r\n"
    "471\tXX1\t\tEXAMPLE 1\t\t\t\t\t1\t\t\t\t\tLOC_Os01g01010.1\t\t\t\t\t\t\r\n"
)


def test_rice_parsers_load_rap_models_msu_map_curated_genes_qtl_and_gwas(species_dir: Path):
    gwas = "\n".join(
        [
            GWAS_HEADER,
            _gwas_row("1", "chr1", 38383000, "IRGSP-1.0", "ST1", "plant height", "PPTO:0000126", "1e-8", "LOC_Os01g66100"),
            _gwas_row("1", "chr1", 38384000, "1", "ST2", "plant height", "PPTO:0000126", "1e-5", "Os01g0883800"),
            _gwas_row("", "chr1", 1500, "IRGSP 1.0", "ST3", "grain length", "", "1e-4", ""),
            _gwas_row("", "chr1", 1500, "IRGSP 1.0", "ST3", "grain length", "", "1e-9", ""),
            _gwas_row("9", "chr1", 1500, "MSU6", "ST4", "grain length", "", "1e-4", ""),
        ]
    ) + "\n"
    _stage(
        "rice",
        species_dir,
        {
            "rapdb_gene_models": {"IRGSP-1.0_representative_2026-02-05.tar.gz": _tar({"IRGSP-1.0_representative/locus.gff": RICE_LOCUS, "IRGSP-1.0_representative/transcripts.gff": RICE_TRANSCRIPTS})},
            "rapdb_msu_map": {"RAP-MSU_2026-02-05.txt.gz": _gz("Os01g0883800\tLOC_Os01g66100.1,LOC_Os01g66100.2\nOs01g0100100\tNone\n")},
            "rapdb_curated_genes": {
                "curated_genes.json": json.dumps([{"locus": "Os01g0883800", "gene_symbols": "SD1, sd1, _", "gene_names": "SEMIDWARF 1", "to": ["TO:0000207 - plant height"], "references": {"12077303": {}}}]).encode(),
                "agri_genes.json": json.dumps([{"locus": "Os01g0883800", "gene_symbols": "SD1", "gene_names": "", "to": ["TO:0000576 - stem length"], "references": {}}]).encode(),
            },
            "oryzabase_genes": {"OryzabaseGeneList.txt": ORYZABASE.encode("cp932")},
            "gramene_rice_qtl": {"Rice_QTL.dat": b"qtl_accession_id\tqtl_name\tpublished_symbol\tto_accession\ttrait_category\ttrait_name\ttrait_symbol\tchromosome\tstart\tend\nAQY001\tRM1\tqPH1\tTO:0000207\tVigor\tplant height\tPH\tChr. 1\t38000000\t39000000\n"},
            "gwas_atlas": {"gwas_association_result_for_rice.txt.gz": _gz(gwas)},
        },
    )
    report = build("rice", "core", data_dir=species_dir)
    assert report.tables["genes"] == 2 and report.tables["gene_parts"] == 2
    stats = report.stats
    assert stats["rapdb_msu_map"]["rap_loci_with_msu"] == 1 and stats["rapdb_msu_map"]["rap_loci_none"] == 1
    assert stats["oryzabase_genes"]["encoding:cp932"] == 1
    assert stats["gramene_rice_qtl"] == {"checked_on:IRGSP-1.0": 1, "outside_chromosome_ends": 0, "qtl": 1, "trait_map": 1}
    assert stats["gwas_atlas"]["skipped_assembly:MSU6"] == 1 and stats["gwas_atlas"]["reported_genes:synonym"] == 1
    assert verify("rice", data_dir=species_dir).ok

    connection = duckdb.connect(str(report.bundle), read_only=True)
    try:
        assert connection.execute("SELECT defline FROM genes WHERE gene_id = 'Os01g0883800'").fetchone() == ("Gibberellin 20 oxidase 2, regulation of plant height",)
        assert connection.execute("SELECT to_id FROM id_map WHERE relation = 'no_counterpart'").fetchall() == [("None",)]
        known = connection.execute("SELECT source_db, symbols, trait_terms, pmids FROM known_genes WHERE gene_id = 'Os01g0883800' ORDER BY 1").fetchall()
        assert known[0] == ("Oryzabase", ["SD1", "sd1", "OsGA20ox2"], ["TO:0000207", "TO:0000576"], [])
        assert known[1] == ("RAP-DB curated genes", ["SD1", "sd1"], ["TO:0000207", "TO:0000576"], ["12077303"])
        hits = connection.execute("SELECT hit_id, assembly, p_value, reported_genes FROM gwas_hits ORDER BY hit_id").fetchall()
        assert [hit[0] for hit in hits] == ["GWASAtlas|ST1|1", "GWASAtlas|ST2|1", "GWASAtlas|ST3|chr01:1500:grain length"]
        assert hits[0][3] == ["Os01g0883800"] and hits[2][2] == 1e-9 and {hit[1] for hit in hits} == {"IRGSP-1.0"}
    finally:
        connection.close()
    mapped = map_gene_ids(open_bundle("rice", species_dir), ["LOC_Os01g66100.1", "Os01t0883800-02"])
    assert [(item.to_id, item.relation, item.exists) for item in mapped] == [("Os01g0883800", "synonym", True), ("Os01g0883800", "identical", True)]


def test_gramene_qtl_on_another_assembly_fails_the_bounds_check(species_dir: Path):
    rows = "".join(f"AQ{index}\tm\t\tTO:0000207\tVigor\tplant height\tPH\tChr. 1\t44000000\t45000000\n" for index in range(3))
    _stage(
        "rice",
        species_dir,
        {
            "rapdb_gene_models": {"IRGSP-1.0_representative_2026-02-05.tar.gz": _tar({"x/locus.gff": RICE_LOCUS, "x/transcripts.gff": RICE_TRANSCRIPTS})},
            "gramene_rice_qtl": {"Rice_QTL.dat": rows.encode()},
        },
    )
    with pytest.raises(Exception, match="fall past the IRGSP-1.0 chromosome ends"):
        build("rice", "core", data_dir=species_dir)


def test_chain_liftover_maps_aligned_blocks_and_refuses_gaps(tmp_path: Path):
    chain = tmp_path / "v4_to_v5.chain"
    chain.write_text(
        "chain 100 1 300000000 + 1000 3000 1 310000000 + 5000 7000 1\n1000 500 500\n500\n\n"
        "chain 50 2 240000000 + 0 100 2 240000000 - 0 100 2\n100\n",
        encoding="utf-8",
    )
    liftover = ChainLiftover.read(chain, lambda name: f"chr{name}", lambda name: f"chr{name}")
    assert liftover.lift("chr1", 1001) == ("chr1", 5001)
    assert liftover.lift("chr1", 2000) == ("chr1", 6000)
    assert liftover.lift("chr1", 2200) is None
    assert liftover.lift("chr1", 2501) == ("chr1", 6501)
    assert liftover.lift("chr2", 1) == ("chr2", 240000000)


MAIZE_GFF = """##gff-version 3
chr1\tNAM\tgene\t5001\t6500\t.\t+\t.\tID=Zm00001eb000010;biotype=protein_coding
chr1\tNAM\tmRNA\t5001\t6500\t.\t+\t.\tID=Zm00001eb000010_T001;Parent=Zm00001eb000010
chr1\tNAM\tCDS\t5100\t5200\t.\t+\t0\tParent=Zm00001eb000010_T001
scaf_100\tNAM\tgene\t1\t100\t.\t+\t.\tID=Zm00001eb999990
"""


def test_maize_gwas_atlas_rows_are_lifted_from_v4_through_the_chain(species_dir: Path):
    chain = "chain 100 1 307041717 + 1000 3000 1 308452471 + 5000 7000 1\n1000 500 500\n500\n"
    gwas = "\n".join(
        [
            GWAS_HEADER,
            _gwas_row("1", "1", 1200, "B73_RefGenV4", "ST7", "plant height", "PPTO:0000126", "1e-7", "Zm00001d027230"),
            _gwas_row("2", "1", 2200, "AGPv4", "ST7", "plant height", "PPTO:0000126", "1e-6", ""),
        ]
    ) + "\n"
    fulldata = "1\tZm00001eb000010\tZm-B73-REFERENCE-NAM-5.0\tZm00001eb.1\t1\tZm00001eb000010_T001\t0\tchr1\t5001\t6500\td8\tdwarf plant8\tDELLA\t\n"
    wallace = "chr1\tWallace_2014\tSNP\t5050\t5149\t.\t+\t.\ttrait=Plant_height;RS=rs1\n"
    _stage(
        "maize",
        species_dir,
        {
            "maizegdb_gene_models": {"Zm-B73-REFERENCE-NAM-5.0_Zm00001eb.1.gff3.gz": _gz(MAIZE_GFF)},
            "maizegdb_v4_v5_xref": {"B73v4_to_B73v5.tsv": b"Zm00001d027230\tZm00001eb000010\n"},
            "maizegdb_v4_v5_chain": {"B73_RefGen_v4_to_Zm-B73-REFERENCE-NAM-5.0.chain": chain.encode()},
            "maizegdb_classical_genes": {"Zm00001eb.1.fulldata.txt.gz": _gz(fulldata)},
            "gwas_atlas": {"gwas_association_result_for_maize.txt.gz": _gz(gwas)},
            "maizegdb_wallace_gwas": {"B73v5_Wallace_2015_SNPs.gff": wallace.encode()},
        },
    )
    report = build("maize", "core", data_dir=species_dir)
    assert report.stats["gwas_atlas"]["lifted:B73_RefGen_v4->Zm-B73-REFERENCE-NAM-5.0"] == 1
    assert report.stats["gwas_atlas"]["lift_failed:B73_RefGen_v4"] == 1
    assert report.stats["maizegdb_gene_models"]["genes_off_chromosomes"] == 1
    connection = duckdb.connect(str(report.bundle), read_only=True)
    try:
        assert connection.execute("SELECT assembly, chrom, pos, placement, reported_genes FROM gwas_hits WHERE source_db = 'GWAS Atlas'").fetchall() == [
            ("Zm-B73-REFERENCE-NAM-5.0", "chr1", 5200, "lifted:B73_RefGen_v4:chr1:1200", ["Zm00001eb000010"])
        ]
        assert connection.execute("SELECT symbols, symbol_long, weight FROM known_genes").fetchall() == [(["d8"], "dwarf plant8", 0.6)]
        assert connection.execute("SELECT trait_name, pos FROM gwas_hits WHERE source_db LIKE 'Wallace%'").fetchall() == [("Plant height", 5050)]
    finally:
        connection.close()
    assert verify("maize", data_dir=species_dir).ok


CURATED_SORGHUM = Path(__file__).resolve().parents[1] / "src" / "agrihub_data" / "curated" / "sorghum.known_genes.yaml"


def _sorghum_gff() -> str:
    lines = ["##gff-version 3"]
    for gene_id, chrom, start, end in _curated_sorghum_genes():
        lines.append(f"{chrom}\tena\tgene\t{start}\t{end}\t.\t-\t.\tID={gene_id};Name={gene_id};biotype=protein_coding")
        lines.append(f"{chrom}\tena\tmRNA\t{start}\t{end}\t.\t-\t.\tID={gene_id}.1;Parent={gene_id}")
        lines.append(f"{chrom}\tena\tCDS\t{start}\t{start + 99}\t.\t-\t0\tParent={gene_id}.1")
    lines.append("7\tena\tgene\t100\t200\t.\t+\t.\tID=EPlSBIG00000001;biotype=ncRNA")
    return "\n".join(lines) + "\n"


def _curated_sorghum_genes() -> list[tuple[str, str, int, int]]:
    loaded = yaml.safe_load(CURATED_SORGHUM.read_text(encoding="utf-8"))
    return [
        (gene["gene_id"].replace("Sobic.", "SORBI_3"), gene["location"]["chrom"].removeprefix("Chr0").removeprefix("Chr"), gene["location"]["start"], gene["location"]["end"])
        for gene in loaded["genes"]
    ]


def test_sorghum_sobic_ids_resolve_manual_sources_are_optional_and_an_atlas_export_loads(species_dir: Path):
    gwas = "\n".join(
        [
            GWAS_HEADER,
            _gwas_row("1", "chr7", 59825000, "Sorbi3.0", "ST9", "plant height", "PPTO:0000126", "1e-9", "Sobic.007G163800"),
            _gwas_row("2", "chr7", 58000000, "Sbi1.4", "ST9", "plant height", "PPTO:0000126", "1e-9", ""),
        ]
    ) + "\n"
    curated = CURATED_SORGHUM.read_bytes()
    files = {
        "sorghumbase_gene_models": {"Sorghum_bicolor.Sorghum_bicolor_NCBIv3.gff3.gz": _gz(_sorghum_gff())},
        "sorghum_major_genes": {"sorghum.known_genes.yaml": curated},
        "gwas_atlas": {"gwas_association_result_for_sorghum.txt.gz": _gz(gwas)},
        "phytozome_annotation_info": {},
        "sorghum_qtl_atlas": {},
    }
    _stage("sorghum", species_dir, files)
    report = build("sorghum", "core", data_dir=species_dir)
    assert report.stats["sorghum_major_genes"] == {"known_genes": 8, "location_matches_citation": 8, "mapping:identical": 8}
    assert report.stats["gwas_atlas"]["skipped_assembly:Sbi1.4"] == 1
    assert report.stats["gwas_atlas"]["reported_gene_distance:inside"] == 1
    assert report.stats["phytozome_annotation_info"] == {"manual_download_not_provided": 1}
    assert report.stats["sorghumbase_gene_models"]["genes_skipped_namespace:gene"] == 1
    assert verify("sorghum", data_dir=species_dir).ok

    atlas = species_paths("sorghum", species_dir).source_dir("sorghum_qtl_atlas") / "SorghumQtlAtlas.db"
    atlas.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(atlas)
    database.execute('CREATE TABLE atlas ("QTL Id" TEXT, "Publication" TEXT, "Population" TEXT, "Trait Description" TEXT, "LG:Start-End (v3.0)" TEXT, "Genes Under QTL (v3.0)" TEXT, "Type" TEXT)')
    database.executemany(
        "INSERT INTO atlas VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("Dw3", "Multani 2003", "", "Plant height", "7:59821905-59829921", "Sobic.007G163800", "Major effect gene"),
            ("QPH7.1", "Study A", "BTx623 x IS3620C", "Plant height", "7:59000000-60000000", "", "QTL"),
        ],
    )
    database.commit()
    database.close()
    Manifest(species_paths("sorghum", species_dir).manifest, "sorghum").record(
        "sorghum_qtl_atlas/SorghumQtlAtlas.db",
        {"source_id": "sorghum_qtl_atlas", "url": "manual", "path": "SorghumQtlAtlas.db", "status": "present", "size": atlas.stat().st_size, "sha256": sha256_file(atlas), "manual": True},
    )
    report = build("sorghum", "core", data_dir=species_dir)
    assert report.stats["sorghum_qtl_atlas"]["major_genes"] == 1 and report.stats["sorghum_qtl_atlas"]["qtl"] == 1
    connection = duckdb.connect(str(report.bundle), read_only=True)
    try:
        assert connection.execute("SELECT gene_id FROM known_genes WHERE source_db = 'Sorghum QTL Atlas'").fetchall() == [("SORBI_3007G163800",)]
        assert connection.execute("SELECT chrom, start, \"end\", placement FROM qtl").fetchall() == [("Chr07", 59000000, 60000000, "reported_low_precision")]
    finally:
        connection.close()


def test_registry_aliases_namespaces_and_known_gaps():
    assert {normalize_chrom("rice", name) for name in ("chr01", "Chr1", "1", "Chr01")} == {"chr01"}
    assert {normalize_chrom("maize", name) for name in ("chr1", "1", "Chr1")} == {"chr1"}
    assert {normalize_chrom("sorghum", name) for name in ("Chr01", "1", "chr1")} == {"Chr01"}
    assert load_species("sorghum").canonical_gene_id("Sobic.007G163800.1") == "SORBI_3007G163800"
    assert load_species("rice").canonical_gene_id("Os01t0883800-02") == "Os01g0883800"
    assert load_species("soybean").canonical_gene_id("GLYMA_18G092200") == "Glyma.18G092200"
    assert load_species("rice").source_assembly(load_species("rice").source("gwas_atlas"), "1") == "IRGSP-1.0"
    maize = domain_status("maize")["qtl"]
    assert not maize.available and maize.reason is not None and maize.reason.startswith("known gap: no registered maize source provides qtl")
    sorghum = domain_status("sorghum")["expression"]
    assert sorghum.reason is not None and "known gap" in sorghum.reason and "MOROKOSHI is down" in sorghum.reason


def test_disk_budget_sums_registry_sizes_per_tier():
    budgets = {budget.tier: budget for budget in disk_budget("sorghum")}
    assert budgets["core"].bytes > 100_000_000 and not budgets["core"].unknown
    assert budgets["core"].manual == ["phytozome_annotation_info/Sbicolor_454_v3.1.1.annotation_info.txt", "sorghum_qtl_atlas/SorghumQtlAtlas.db"]
    assert budgets["heavy"].bytes > budgets["extended"].bytes


def test_an_omitted_window_takes_the_species_default_flank():
    from agrihub.nodes.intake import check_study

    def flank(species: str, assembly: str, **extra: object) -> int:
        study = {"mode": "snps", "species": species, "assembly": assembly, "trait_text": "plant height", "snps": [{"raw": "x", "chrom": "1", "pos": 1_000_000}], **extra}
        checked = check_study(study).study
        assert checked is not None
        return checked.window.flank_bp

    assert flank("maize", "Zm-B73-REFERENCE-NAM-5.0") == 50_000
    assert flank("rice", "IRGSP-1.0") == 150_000
    assert flank("soybean", "Wm82.a2.v1") == 250_000
    assert flank("maize", "Zm-B73-REFERENCE-NAM-5.0", window={"mode": "fixed", "flank_bp": 20_000}) == 20_000
