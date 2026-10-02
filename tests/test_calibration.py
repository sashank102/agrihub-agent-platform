"""Rubric calibration on the real soybean bundle.

Housekeeping genes must not earn specificity from a low tau, the SNP-containing
gene must stay ahead of same-locus genes whose only advantage is that
non-specific expression, and a seed-specific oil gene keeps the full
specificity band.
"""

import pytest

from agent_platform.core.settings import get_data_paths
from agrihub import scoring
from agrihub.evidence_store import evidence_id_for
from agrihub.state import CandidateGene, EvidenceItem
from agrihub_data.bundle import close_bundles, open_bundle
from agrihub_data.query import annotation, expression, loci
from agrihub_data.query.common import Region
from agrihub_data.query.traits import map_trait
from agrihub_data.registry import load_species

BUNDLE = get_data_paths().data_dir / "soybean" / "bundle.duckdb"
HOUSEKEEPING = ("Glyma.18G227500", "Glyma.05G033800", "Glyma.05G033200")
WRKY = "Glyma.18G092200"
FAD2 = "Glyma.10G278000"
OTHER_CATEGORIES = ("B", "C", "D", "F", "G")

pytestmark = [
    pytest.mark.bundle,
    pytest.mark.skipif(not BUNDLE.exists(), reason=f"no soybean bundle at {BUNDLE}"),
]


@pytest.fixture
def bundle():
    opened = open_bundle("soybean")
    yield opened
    close_bundles()


def _stamp(items: list[EvidenceItem]) -> list[EvidenceItem]:
    return [item.model_copy(update={"evidence_id": item.evidence_id or evidence_id_for(item)}) for item in items]


def _expression_items(bundle, gene_ids: list[str], trait: str) -> list[EvidenceItem]:
    profile = map_trait(trait, "soybean", bundle)
    selection = expression.trait_relevant_tissues("soybean", profile, bundle)
    rows = [
        *expression.expression_profile(bundle, gene_ids, selection),
        *expression.tissue_specificity(bundle, gene_ids, selection),
    ]
    domains = []
    for record in annotation.gene_annotation(bundle, gene_ids):
        domains.extend(item for item in record.evidence() if item.subtype.split(":", 1)[0] in {"pfam", "panther", "defline"})
    return _stamp([* (item for row in rows for item in row.evidence()), *domains])


def test_housekeeping_genes_score_at_most_two_expression_points_for_plant_height(bundle):
    items = _expression_items(bundle, list(HOUSEKEEPING), "plant height")
    genes = []
    for gene_id in HOUSEKEEPING:
        defline = next((item.quote or "" for item in items if item.gene_id == gene_id and item.subtype == "defline"), "")
        genes.append(CandidateGene(gene_id=gene_id, locus_id="L", chrom="Gm18", start=1, end=2, distance_bp=10_000, defline=defline))
    scores = scoring.score_candidates(genes, items, profile=map_trait("plant height", "soybean", bundle), ld_kb=150, available={"A", "E"})
    for gene_id in HOUSEKEEPING:
        assert scores.genes[gene_id].categories["E"].points <= 2, gene_id
        assert scores.genes[gene_id].tier == "T4"


def test_wrky_ranks_at_or_above_neighbours_that_only_differ_by_nonspecific_expression(bundle):
    profile = map_trait("plant height", "soybean", bundle)
    placed = loci.genes_in_window(
        bundle,
        Region(chrom="18", start=9_263_941 - 250_000, end=9_263_941 + 250_000, assembly="Wm82.a2.v1", snp_pos=9_263_941),
    )
    genes = [
        CandidateGene(
            gene_id=row.gene_id,
            locus_id="L18",
            symbol=None,
            chrom=row.chrom,
            start=row.start,
            end=row.end,
            strand=row.strand if row.strand in {"+", "-", "."} else ".",
            distance_bp=int(row.dist_to_snp or 0),
            overlaps_snp=row.overlaps_snp,
            nearest_snp="S18_9263941",
            defline=row.defline or "",
        )
        for row in placed
    ]
    items = _expression_items(bundle, [gene.gene_id for gene in genes], "plant height")
    items.extend(_stamp(item for row in placed for item in row.evidence()))
    scores = scoring.score_candidates(genes, items, profile=profile, ld_kb=load_species("soybean").typical_ld_kb, available={"A", "E"})
    wrky = scores.genes[WRKY]
    assert wrky.categories["A"].points == 20
    for gene in scores.ranked("L18"):
        if gene.gene_id == WRKY:
            continue
        only_expression = all(gene.categories[code].points <= wrky.categories[code].points + 1e-6 for code in OTHER_CATEGORIES)
        nonspecific = gene.categories["E"].points <= max(band.points for band in scoring.load_rubric().expression.trait_tpm)
        if only_expression and nonspecific and gene.categories["E"].points > wrky.categories["E"].points:
            assert wrky.rank_in_locus <= gene.rank_in_locus, (gene.gene_id, gene.score, gene.points(), wrky.points())
    cap = max(band.points for band in scoring.load_rubric().expression.trait_tpm)
    peers = [
        gene
        for gene in scores.ranked("L18")
        if all(gene.categories[code].points == 0 for code in OTHER_CATEGORIES) and gene.categories["E"].points <= cap
    ]
    assert WRKY in {gene.gene_id for gene in peers}
    assert wrky.score >= max(gene.score for gene in peers)
    assert wrky.rank_in_locus == min(gene.rank_in_locus for gene in peers)


def test_fad2_1a_keeps_full_expression_specificity_for_seed_oil(bundle):
    items = _expression_items(bundle, [FAD2], "seed oil")
    gene = CandidateGene(gene_id=FAD2, locus_id="L", chrom="Gm10", start=1, end=2, distance_bp=0, defline="fatty acid desaturase 2")
    scores = scoring.score_candidates([gene], items, profile=map_trait("seed oil", "soybean", bundle), ld_kb=150, available={"A", "E"})
    assert scores.genes[FAD2].categories["E"].points >= scoring.load_rubric().expression.specific
