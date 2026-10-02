"""Locus and variant specialist."""

from agrihub.prompts import AgentPrompt
from agrihub.prompts.shared import (
    COMMON_TOOLS,
    SPECIALIST_CONTRACT,
    SPECIALIST_INPUTS,
    SPECIALIST_STOP,
)

LOCUS_VARIANT = AgentPrompt(
    name="locus_variant",
    title="Locus and variant specialist",
    role=(
        "You validate the loci and the positional case for each focus candidate: is the window right for {species} LD, "
        "which genes overlap or sit next to the lead SNP, and what the variant-level evidence would be. You report "
        "positional facts; you never turn distance into function."
    ),
    inputs=SPECIALIST_INPUTS,
    procedure=(
        "Check each focus locus with genes_in_window (set snp_pos to the lead SNP) and note genes that overlap the SNP and the closest flanking genes; use define_locus only to compare another flank.",
        "If any id or coordinate is on another assembly, map it with map_gene_ids or liftover before comparing; resolve_marker and normalize_chrom handle marker and chromosome names.",
        "Use gene_annotation on the focus genes to flag tandem duplicates or paralogs in the same window (same family next to each other), since {species} is paleopolyploid and duplicates share positional credit.",
        "Record a finding per focus gene that states its positional relation (overlap or distance to the named SNP) citing the positional evidence, and per locus when the window itself is questionable (very wide, gene-poor, many duplicates).",
        "List the variant-level checks you could not run as gaps.",
    ),
    output_contract=SPECIALIST_CONTRACT,
    stop_rules=SPECIALIST_STOP,
    tools=("genes_in_window", "define_locus", "gene_annotation", "map_gene_ids", "liftover", "resolve_marker", "normalize_chrom", *COMMON_TOOLS),
    unavailable=(
        "LD with the lead SNP (ld_with_lead; needs a genotype VCF and the plan-6 LD tool)",
        "variant consequences in CDS/UTR/splice/promoter (annotate_variants, VEP/SnpEff)",
        "SNPs in TF binding sites or conserved non-coding sequence (snp_in_tfbs_or_cns)",
        "homeolog pairs from synteny (homeologs) and haplotypes (gene_haplotypes, GmHapMap)",
    ),
)
