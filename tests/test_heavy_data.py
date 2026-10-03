"""Heavy tier on fixture data: variant location classes, VEP/SnpEff/PLINK2 parsers, LD windows, homeologs, haplotypes."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from agrihub_fixtures import FixtureBundle

from agrihub.evidence_store import evidence_id_for
from agrihub.tools import variant_tools
from agrihub_data import external
from agrihub_data.bundle import open_bundle
from agrihub_data.query import duplication, ld, variants
from agrihub_data.query.loci import define_locus
from agrihub_data.registry import load_species

PLINK2 = external.plink2_path()
VEP_TAB = """## ENSEMBL VARIANT EFFECT PREDICTOR v116.2
## Output produced at 2026-10-02 20:52:51
#Uploaded_variation\tLocation\tAllele\tGene\tFeature\tConsequence\tIMPACT\tAmino_acids\tCodons\tProtein_position\tSTRAND
S18_9263941\t18:9263941\tG\tGLYMA_18G092200\tKRG98705\tmissense_variant\tMODERATE\tA/T\tGcc/Acc\t120\t-1
S18_9263941\t18:9263941\tG\tGLYMA_18G092200\tKRG98706\tintron_variant\tMODIFIER\t-\t-\t-\t-1
S18_9300000\t18:9300000\tC\t-\t-\tintergenic_variant\tMODIFIER\t-\t-\t-\t-
"""
VCOR = """#CHROM_A\tPOS_A\tID_A\tCHROM_B\tPOS_B\tID_B\tUNPHASED_R2
glyma.Wm82.gnm2.Gm18\t9262600\tss3\tglyma.Wm82.gnm2.Gm18\t9200000\tss1\t1
glyma.Wm82.gnm2.Gm18\t9262600\tss3\tglyma.Wm82.gnm2.Gm18\t9240000\tss2\t0.98
glyma.Wm82.gnm2.Gm18\t9262600\tss3\tglyma.Wm82.gnm2.Gm18\t9500000\tss5\t0.643
glyma.Wm82.gnm2.Gm18\t9262600\tss3\tglyma.Wm82.gnm2.Gm18\t9300000\tss4\t0.21
glyma.Wm82.gnm2.Gm18\t9262600\tss3\tglyma.Wm82.gnm2.Gm19\t9300000\tss9\t0.9
"""


@pytest.fixture
def bundle(heavy_env: FixtureBundle, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_VEP_IMAGE", "agrihub-test/no-such-vep:0")
    monkeypatch.delenv("AGRIHUB_VEP", raising=False)
    external.reset_probes()
    yield open_bundle("soybean")
    external.reset_probes()


def _tool(tool: Any, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    config = {"configurable": {"run_id": "run-heavy"}, "metadata": {"agrihub_agent_id": "call_test"}}
    message = asyncio.run(tool.ainvoke({"name": tool.name, "args": args, "id": "call-1", "type": "tool_call"}, config))
    return str(message.content), dict(message.artifact or {})


def _unique_ids(rows: list[Any]) -> None:
    items = [item for row in rows for item in row.evidence()]
    assert len({evidence_id_for(item) for item in items}) == len(items)


def test_heavy_builds_list_resources_and_pair_homeologs(heavy_bundle: FixtureBundle):
    stats = heavy_bundle.build_report.stats
    assert stats["lis_synteny_hxny"] == {"blocks": 2, "blocks_unplaced": 1, "homeolog_pairs": 2}
    assert stats["ensembl_vep_cache"] == {"cache_chromosomes": 1}
    assert stats["lis_panel_song_hyten_2015"]["sites"] == 6
    assert stats["gmhapmap"] == {"haplotype_snps": 2, "nonsyn_sites": 1, "rows_without_gene": 1}
    bundle = open_bundle("soybean", heavy_bundle.data_dir)
    resources = {row["kind"]: row["path"] for row in bundle.rows("SELECT kind, path FROM resources")}
    assert resources["vep_cache"] == "vep/glycine_max/63_Glycine_max_v2.1"
    assert resources["ld_panel"].endswith((".pgen", ".vcf.gz"))
    assert (heavy_bundle.data_dir / "soybean" / resources["vep_cache"] / "info.txt").exists()
    nonsyn = bundle.rows("SELECT variant_id, chrom, alt_freq FROM variants WHERE panel = 'GmHapMap_NonSyn'")
    assert nonsyn == [{"variant_id": "A18.0001", "chrom": "Gm18", "alt_freq": 0.5}]


@pytest.mark.parametrize(
    ("pos", "expected"),
    [
        (9_263_941, ("Glyma.18G092200", "CDS", 0)),
        (9_263_500, ("Glyma.18G092200", "intron", 0)),
        (9_263_005, ("Glyma.18G092200", "splice", 0)),
        (9_266_800, ("Glyma.18G092200", "five_prime_UTR", 0)),
        (9_262_450, ("Glyma.18G092200", "three_prime_UTR", 0)),
        (9_268_000, ("Glyma.18G092200", "upstream", 992)),
        (9_262_000, ("Glyma.18G092200", "downstream", 392)),
        (9_250_000, (None, "intergenic", 8_495)),
    ],
)
def test_location_classes_follow_gene_models_and_strand(bundle, pos: int, expected: tuple[Any, ...]):
    location = variants.location_classes(bundle, [variants.VariantInput(id="snp", chrom="18", pos=pos)])[0]
    assert (location.gene_id, location.location_class, location.distance_bp) == expected
    _unique_ids([location])


def test_annotate_variants_without_alleles_or_vep_reports_the_gap(bundle):
    rows, notes = variants.annotate_variants(bundle, [variants.VariantInput(id="S18_9263941", chrom="Gm18", pos=9_263_941)])
    assert rows[0].location_class == "CDS" and notes == ["consequences need REF and ALT alleles; reported the gene-model location class only"]
    rows, notes = variants.annotate_variants(bundle, [variants.VariantInput(id="S18_9263941", chrom="Gm18", pos=9_263_941, ref="A", alt="G")])
    assert rows[0].method == "gene_models" and "Ensembl VEP is not installed" in notes and notes[-1].startswith("consequences unavailable")
    content, artifact = _tool(variant_tools.annotate_variants, {"variants": [{"id": "S18_9263941", "chrom": "18", "pos": 9_263_941, "ref": "A", "alt": "G"}]})
    assert "S18_9263941 Gm18:9263941 Glyma.18G092200 CDS" in content and "unavailable: variant consequences" in content
    assert len(artifact["evidence_ids"]) == 1


def test_vep_and_snpeff_outputs_are_parsed_most_severe_first():
    parsed = variants.parse_vep_tab(VEP_TAB, load_species("soybean"))
    lead = parsed["S18_9263941"]
    assert [(item.gene_id, item.terms, item.impact) for item in lead] == [
        ("Glyma.18G092200", ["missense_variant"], "MODERATE"),
        ("Glyma.18G092200", ["intron_variant"], "MODIFIER"),
    ]
    assert lead[0].amino_acids == "A/T" and parsed["S18_9300000"][0].gene_id is None
    snpeff = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n18\t9263941\tS18_9263941\tA\tG\t.\t.\tANN=G|intron_variant|MODIFIER|Glyma.18G092200|GLYMA_18G092200|transcript|KRG98706|protein_coding|2/3|c.100+5A>G||,G|stop_gained|HIGH|Glyma.18G092200|GLYMA_18G092200|transcript|KRG98705|protein_coding|3/4|c.300A>G|p.Lys100*|\n"
    parsed_snpeff = variants.parse_snpeff_vcf(snpeff, load_species("soybean"))["S18_9263941"]
    assert [(item.terms, item.impact) for item in parsed_snpeff] == [(["stop_gained"], "HIGH"), (["intron_variant"], "MODIFIER")]
    assert variants.most_severe_impact(parsed_snpeff) == "HIGH"


def test_vep_input_uses_ensembl_chromosome_names(heavy_env: FixtureBundle):
    text = variants.vcf_text([variants.VariantInput(id="b", chrom="Gm18", pos=9, ref="A", alt="G"), variants.VariantInput(id="a", chrom="5", pos=3, ref="C", alt="T")], "soybean")
    assert text.splitlines()[2:] == ["5\t3\ta\tC\tT\t.\t.\t.", "18\t9\tb\tA\tG\t.\t.\t."]


def test_plink_vcor_parser_and_ld_window_bounds(heavy_env: FixtureBundle):
    partners = ld.parse_vcor(VCOR, "soybean")
    assert [(partner.variant_id, partner.chrom, partner.r2) for partner in partners][:2] == [("ss1", "Gm18", 1.0), ("ss2", "Gm18", 0.98)]
    assert ld.window_from_partners("Gm18", 9_263_941, 9_262_600, partners, 1_000, 0.2) == (9_200_000, 9_500_000)
    assert ld.window_from_partners("Gm18", 9_263_941, 9_262_600, partners, 1_000, 0.7) == (9_200_000, 9_263_941)
    assert ld.window_from_partners("Gm18", 9_263_941, 9_262_600, partners, 100, 0.2) == (9_200_000, 9_300_000)
    assert ld.window_from_partners("Gm18", 9_263_941, 9_262_600, [], 1_000, 0.2) == (9_262_600, 9_263_941)
    plink19 = " CHR_A BP_A SNP_A CHR_B BP_B SNP_B R2\n 18 9262600 ss3 18 9240000 ss2 0.98\n"
    assert ld.parse_vcor(plink19, "soybean")[0].pos == 9_240_000


def test_ld_window_extends_to_gene_boundaries(bundle):
    start, end, genes = ld.extend_to_genes(bundle, "Wm82.a2.v1", "Gm18", 9_238_000, 9_263_941)
    assert (start, end, genes) == (9_236_520, 9_267_008, ["Glyma.18G092000", "Glyma.18G092200"])


def test_ld_is_an_unavailable_gap_without_plink2(bundle, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_PLINK2", "/nonexistent/plink2")
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(ld.LdUnavailableError, match="PLINK2 is not installed"):
        ld.ld_window(bundle, "S18_9263941", "18", 9_263_941)
    content, artifact = _tool(variant_tools.ld_with_lead, {"leads": [{"label": "S18_9263941", "chrom": "18", "pos": 9_263_941}]})
    assert "unavailable for S18_9263941: PLINK2 is not installed" in content and artifact["evidence_ids"] == []
    with pytest.raises(ld.LdUnavailableError):
        define_locus("soybean", "18", 9_263_941, "ld", bundle=bundle)
    content, _ = _tool(variant_tools.homeologs, {"gene_ids": ["Glyma.18G092000"]})
    assert "Glyma.18G092000 <-> Glyma.18G092200" in content


@pytest.mark.skipif(PLINK2 is None, reason="PLINK2 is not installed (scripts/setup_heavy_tools.sh)")
def test_ld_window_on_the_fixture_panel(bundle, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_PLINK2", str(PLINK2))
    window = ld.ld_window(bundle, "S18_9263941", "18", 9_263_941)
    assert (window.tested_variant, window.proxy_distance_bp) == ("ss3", 1_341)
    assert (window.ld_start, window.ld_end, window.start, window.end) == (9_200_000, 9_500_000, 9_200_000, 9_500_000)
    assert {partner.variant_id for partner in window.partners} == {"ss1", "ss2", "ss5"}
    genes = ld.gene_ld(window, [("Glyma.18G092200", 9_262_392, 9_267_008), ("Glyma.18G092000", 9_236_520, 9_241_505), ("Glyma.18G092300", 9_278_545, 9_280_545)])
    assert [(gene.gene_id, gene.r2, gene.via) for gene in genes] == [("Glyma.18G092200", 1.0, None), ("Glyma.18G092000", 1.0, "ss2")]
    locus = define_locus("soybean", "18", 9_263_941, "ld", bundle=bundle, label="S18_9263941")
    assert (locus.mode, locus.start, locus.end) == ("ld", 9_200_000, 9_500_000) and locus.ld is not None
    content, artifact = _tool(variant_tools.ld_with_lead, {"leads": [{"label": "S18_9263941", "chrom": "18", "pos": 9_263_941}]})
    assert "S18_9263941 LD window Gm18:9200000-9500000" in content and "Glyma.18G092000 r2=1 with S18_9263941 via ss2" in content
    assert len(artifact["evidence_ids"]) == 3
    _unique_ids([window, *genes])


def test_study_vcf_refs_cannot_leave_the_genotypes_directory(heavy_env: FixtureBundle):
    with pytest.raises(ValueError, match="not a path"):
        ld.genotype_source("soybean", "../soybean/raw/x.vcf.gz")
    with pytest.raises(ld.LdUnavailableError, match="no genotype VCF"):
        ld.genotype_source("soybean", "mine.vcf.gz")
    assert ld.genotype_source("soybean").name == "Song_Hyten_2015"
    genotypes = Path(heavy_env.data_dir) / "soybean" / "genotypes"
    genotypes.mkdir(exist_ok=True)
    (genotypes / "mine.vcf").write_text("##contig=<ID=chr18,length=58018742>\n#CHROM\tPOS\n")
    source = ld.genotype_source("soybean", "mine.vcf")
    assert source.kind == "vcf" and ld.vcf_chrom(source.path, "soybean", "Gm18") == "chr18"


def test_homeologs_and_haplotypes(bundle):
    pairs = duplication.homeologs(bundle, ["Glyma.18G092200", "Glyma.19G194300"])
    assert [(pair.gene_id, pair.homeolog_id, pair.family, pair.median_ks) for pair in pairs] == [("Glyma.18G092200", "Glyma.18G092000", "PTHR31429", 0.13)]
    haplotypes = duplication.gene_haplotypes(bundle, ["Glyma.18G092200", "Glyma.05G032200"])
    assert len(haplotypes) == 1
    record = haplotypes[0]
    assert (record.n_snps, record.haplotypes, record.snps[0].genotypes) == (2, ["A", "B", "C"], ["AA", "GG", "AG"])
    assert [(snp.variant_id, snp.alt_freq) for snp in record.nonsynonymous] == [("A18.0001", 0.5)]
    _unique_ids(pairs)
    _unique_ids(haplotypes)
    content, artifact = _tool(variant_tools.gene_haplotypes, {"gene_ids": ["Glyma.18G092200", "Glyma.05G032200"]})
    assert "3 haplotypes (A,B,C) from 2 SNPs; 1 non-synonymous" in content and "not in GmHapMap: Glyma.05G032200" in content
    assert len(artifact["evidence_ids"]) == 1
