-- AgriHub species bundle schema.
--
-- Every data table carries species, assembly and source_version. assembly is
-- a registered assembly id (the species' own or a reference assembly such as
-- TAIR10), or 'none' for rows not tied to a genome. Coordinates are 1-based
-- and inclusive, as in GFF3, on the row's assembly. Chromosome names are the
-- registry's canonical names.
--
-- Core tables come first, then the extended tier (expression, samples,
-- edges, tf, regulation, regulatory_regions, pathways) and the heavy tier
-- (homeologs, gene_haplotypes, variants, resources). A bundle built for a
-- lower tier has the higher tiers' tables, empty.

CREATE TABLE bundle_info (
    key VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);

CREATE TABLE sources (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    source_id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    license VARCHAR NOT NULL,
    academic_only BOOLEAN NOT NULL,
    homepage VARCHAR,
    citation VARCHAR,
    files INTEGER NOT NULL,
    bytes BIGINT NOT NULL
);

CREATE TABLE genes (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    gene_id VARCHAR NOT NULL,
    chrom VARCHAR NOT NULL,
    start BIGINT NOT NULL,
    "end" BIGINT NOT NULL,
    strand VARCHAR NOT NULL,
    defline VARCHAR,
    ancestor_id VARCHAR,
    source_db VARCHAR NOT NULL,
    PRIMARY KEY (assembly, gene_id)
);

-- Coding and untranslated parts of every transcript in the gene models: part
-- is CDS, five_prime_UTR or three_prime_UTR. Exons are their union per
-- transcript; introns are the gaps between them inside the gene.
CREATE TABLE gene_parts (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    gene_id VARCHAR NOT NULL,
    transcript_id VARCHAR NOT NULL,
    part VARCHAR NOT NULL,
    chrom VARCHAR NOT NULL,
    start BIGINT NOT NULL,
    "end" BIGINT NOT NULL,
    strand VARCHAR NOT NULL,
    source_db VARCHAR NOT NULL
);

-- Cross-namespace and cross-assembly identifiers. relation is one of
-- pangene_member (to_id is a pangene id, to_assembly 'none'), ancestor
-- (GFF ancestorIdentifier), synonym (older gene id) or transcript.
CREATE TABLE id_map (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    from_id VARCHAR NOT NULL,
    to_id VARCHAR NOT NULL,
    to_assembly VARCHAR NOT NULL,
    relation VARCHAR NOT NULL,
    source_db VARCHAR NOT NULL
);

-- Gene-level annotation, one row per (gene, kind, value). kind is pfam,
-- panther, kog, ec, ko, interpro, arabidopsis_best_hit, rice_best_hit, or for
-- reference genes symbol, full_name, short_description, curator_summary and
-- computational_description.
CREATE TABLE annotation (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    gene_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    value VARCHAR NOT NULL,
    label VARCHAR,
    source_db VARCHAR NOT NULL
);

CREATE TABLE go_annot (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    gene_id VARCHAR NOT NULL,
    go_id VARCHAR NOT NULL,
    evidence_code VARCHAR NOT NULL,
    qualifier VARCHAR,
    reference VARCHAR,
    source_db VARCHAR NOT NULL
);

-- One row per (gene, target gene, method). Consensus is computed at query
-- time; relation is the method's own cardinality when it reports one.
CREATE TABLE orthologs (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    gene_id VARCHAR NOT NULL,
    source_gene_id VARCHAR NOT NULL,
    target_species VARCHAR NOT NULL,
    target_assembly VARCHAR NOT NULL,
    target_gene_id VARCHAR NOT NULL,
    method VARCHAR NOT NULL,
    relation VARCHAR,
    identity DOUBLE,
    high_confidence BOOLEAN,
    source_db VARCHAR NOT NULL
);

-- QTL placement: bp spans come from the QTL's markers on the row assembly.
-- placement is markers, single_marker, lg_conflict or unplaced; unplaced
-- rows keep cM only and have NULL chrom/start/end.
CREATE TABLE qtl (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    qtl_id VARCHAR PRIMARY KEY,
    study_id VARCHAR NOT NULL,
    qtl_name VARCHAR NOT NULL,
    trait_name VARCHAR NOT NULL,
    trait_terms VARCHAR[] NOT NULL,
    genetic_map VARCHAR,
    linkage_group VARCHAR,
    cm_start DOUBLE,
    cm_end DOUBLE,
    cm_peak DOUBLE,
    chrom VARCHAR,
    start BIGINT,
    "end" BIGINT,
    span_bp BIGINT,
    n_markers INTEGER NOT NULL,
    n_markers_placed INTEGER NOT NULL,
    placement VARCHAR NOT NULL,
    publication_doi VARCHAR,
    source_db VARCHAR NOT NULL
);

CREATE TABLE gwas_hits (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    hit_id VARCHAR PRIMARY KEY,
    source_db VARCHAR NOT NULL,
    study_id VARCHAR NOT NULL,
    trait_name VARCHAR NOT NULL,
    trait_terms VARCHAR[] NOT NULL,
    marker VARCHAR,
    chrom VARCHAR,
    pos BIGINT,
    p_value DOUBLE,
    pmid VARCHAR,
    doi VARCHAR,
    reported_genes VARCHAR[] NOT NULL,
    placement VARCHAR NOT NULL
);

-- Curated trait genes mapped to the canonical assembly. mapping records how
-- source_gene_id on source_assembly became gene_id.
CREATE TABLE known_genes (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    gene_id VARCHAR NOT NULL,
    source_gene_id VARCHAR NOT NULL,
    source_assembly VARCHAR NOT NULL,
    mapping VARCHAR NOT NULL,
    symbols VARCHAR[] NOT NULL,
    symbol_long VARCHAR,
    synopsis VARCHAR,
    trait_terms VARCHAR[] NOT NULL,
    trait_names VARCHAR[] NOT NULL,
    confidence INTEGER,
    pmids VARCHAR[] NOT NULL,
    dois VARCHAR[] NOT NULL,
    weight DOUBLE NOT NULL,
    source_db VARCHAR NOT NULL
);

-- marker_id is the source Name; alias is the id behind it (ss number) when
-- the source provides one.
CREATE TABLE markers (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    marker_id VARCHAR NOT NULL,
    alias VARCHAR,
    marker_set VARCHAR NOT NULL,
    chrom VARCHAR NOT NULL,
    start BIGINT NOT NULL,
    "end" BIGINT NOT NULL,
    alleles VARCHAR,
    source_db VARCHAR NOT NULL
);

CREATE TABLE ontology_terms (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    ontology VARCHAR NOT NULL,
    term_id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    namespace VARCHAR,
    definition VARCHAR,
    synonyms VARCHAR[] NOT NULL,
    parents VARCHAR[] NOT NULL,
    is_obsolete BOOLEAN NOT NULL,
    source_db VARCHAR NOT NULL
);

-- Source trait names and the ontology terms their curators assigned.
CREATE TABLE trait_map (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    trait_name VARCHAR NOT NULL,
    term_id VARCHAR NOT NULL,
    study_id VARCHAR,
    source_db VARCHAR NOT NULL
);

CREATE TABLE phenotypes (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    gene_id VARCHAR NOT NULL,
    germplasm VARCHAR,
    phenotype VARCHAR NOT NULL,
    pmid VARCHAR,
    source_db VARCHAR NOT NULL
);

CREATE TABLE gene_publications (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    gene_id VARCHAR NOT NULL,
    pmid VARCHAR NOT NULL,
    year INTEGER,
    source_db VARCHAR NOT NULL
);

-- NCBI Gene records of the bundle species and Arabidopsis. gene_id is the
-- registry gene id on assembly, mapped from the locus tag (mapping: ancestor,
-- same_id or locus_tag); NULL when the locus tag maps to no bundle gene.
CREATE TABLE ncbi_genes (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    ncbi_gene_id VARCHAR NOT NULL,
    tax_id INTEGER NOT NULL,
    gene_id VARCHAR,
    mapping VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    locus_tag VARCHAR,
    synonyms VARCHAR[] NOT NULL,
    description VARCHAR,
    designations VARCHAR[] NOT NULL,
    gene_type VARCHAR,
    source_db VARCHAR NOT NULL
);

-- gene2pubmed links; genes_per_pmid counts the GeneIDs linked to the PMID
-- across every species, so genome and other hub papers can be dropped.
CREATE TABLE ncbi_gene_pubmed (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    ncbi_gene_id VARCHAR NOT NULL,
    gene_id VARCHAR,
    pmid VARCHAR NOT NULL,
    genes_per_pmid INTEGER NOT NULL,
    source_db VARCHAR NOT NULL
);

CREATE TABLE ncbi_gene_go (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    ncbi_gene_id VARCHAR NOT NULL,
    gene_id VARCHAR,
    go_id VARCHAR NOT NULL,
    go_term VARCHAR,
    evidence_code VARCHAR NOT NULL,
    qualifier VARCHAR,
    category VARCHAR,
    pmids VARCHAR[] NOT NULL,
    source_db VARCHAR NOT NULL
);

-- ---------------------------------------------------------------- extended

-- Sample groups of an expression dataset: replicates of one condition are
-- averaged into one sample; tissue and stage are normalized labels.
CREATE TABLE samples (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    dataset VARCHAR NOT NULL,
    sample VARCHAR NOT NULL,
    tissue VARCHAR NOT NULL,
    stage VARCHAR,
    condition VARCHAR,
    description VARCHAR,
    n_replicates INTEGER NOT NULL,
    source_db VARCHAR NOT NULL,
    PRIMARY KEY (dataset, sample)
);

-- Long-form expression: one row per (gene, dataset, sample), value is the
-- mean over the sample's replicates. source_gene_id on source_assembly is
-- the id the atlas used before mapping to the row's assembly.
CREATE TABLE expression (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    gene_id VARCHAR NOT NULL,
    source_gene_id VARCHAR NOT NULL,
    source_assembly VARCHAR NOT NULL,
    dataset VARCHAR NOT NULL,
    sample VARCHAR NOT NULL,
    tissue VARCHAR NOT NULL,
    stage VARCHAR,
    value DOUBLE NOT NULL,
    unit VARCHAR NOT NULL,
    source_db VARCHAR NOT NULL
);

-- Gene-gene networks. network string: STRING associations stored once with
-- gene_a < gene_b, score in 0-1 recomputed without text mining, channels as
-- JSON. network atted: gene_b is among gene_a's top ATTED-II co-expression
-- partners; score is the z-score and rank its position.
CREATE TABLE edges (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    network VARCHAR NOT NULL,
    gene_a VARCHAR NOT NULL,
    gene_b VARCHAR NOT NULL,
    score DOUBLE NOT NULL,
    rank INTEGER,
    channels VARCHAR,
    source_db VARCHAR NOT NULL
);

CREATE TABLE tf (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    gene_id VARCHAR NOT NULL,
    family VARCHAR NOT NULL,
    motif_ids VARCHAR[] NOT NULL,
    motif_sources VARCHAR[] NOT NULL,
    source_db VARCHAR NOT NULL
);

-- Transcription factor -> target links; evidence is the PlantRegMap method
-- (motif, FunTFBS, motif_CE, ...).
CREATE TABLE regulation (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    tf_gene_id VARCHAR NOT NULL,
    target_gene_id VARCHAR NOT NULL,
    evidence VARCHAR NOT NULL,
    source_db VARCHAR NOT NULL
);

-- Regulatory intervals: kind tfbs (a FunTFBS site of tf_gene_id in a
-- promoter, score its PlantRegMap score) or cns (a phastCons conserved
-- element, score its LOD).
CREATE TABLE regulatory_regions (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    region_id VARCHAR NOT NULL,
    chrom VARCHAR NOT NULL,
    start BIGINT NOT NULL,
    "end" BIGINT NOT NULL,
    strand VARCHAR NOT NULL,
    tf_gene_id VARCHAR,
    score DOUBLE,
    source_db VARCHAR NOT NULL
);

CREATE TABLE pathways (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    gene_id VARCHAR NOT NULL,
    pathway_id VARCHAR NOT NULL,
    pathway_name VARCHAR NOT NULL,
    reaction_id VARCHAR,
    ec VARCHAR,
    source_gene_id VARCHAR NOT NULL,
    source_db VARCHAR NOT NULL
);

-- ------------------------------------------------------------------- heavy

-- Homeolog pairs from recent-duplication synteny blocks: homeolog_id lies in
-- the region block_id pairs with gene_id's region, shares its family, and is
-- the closest such gene to gene_id's projected position (offset_bp).
CREATE TABLE homeologs (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    gene_id VARCHAR NOT NULL,
    homeolog_id VARCHAR NOT NULL,
    block_id VARCHAR NOT NULL,
    median_ks DOUBLE,
    family VARCHAR NOT NULL,
    offset_bp BIGINT NOT NULL,
    source_db VARCHAR NOT NULL
);

-- GmHapMap haplotypes by gene: one row per SNP of the gene with the allele
-- of each haplotype (haplotypes[i] carries genotypes[i]).
CREATE TABLE gene_haplotypes (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    gene_id VARCHAR NOT NULL,
    snp_id VARCHAR NOT NULL,
    chrom VARCHAR NOT NULL,
    pos BIGINT NOT NULL,
    alleles VARCHAR,
    haplotypes VARCHAR[] NOT NULL,
    genotypes VARCHAR[] NOT NULL,
    source_db VARCHAR NOT NULL
);

-- Variant sites of genotype panels (LD reference panels, GmHapMap
-- non-synonymous SNPs) with the alternate-allele frequency over the panel.
CREATE TABLE variants (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    panel VARCHAR NOT NULL,
    variant_id VARCHAR NOT NULL,
    chrom VARCHAR NOT NULL,
    pos BIGINT NOT NULL,
    ref VARCHAR NOT NULL,
    alt VARCHAR NOT NULL,
    alt_freq DOUBLE,
    source_db VARCHAR NOT NULL
);

-- Files a heavy-tier build unpacks next to the bundle (VEP cache, LD panel);
-- path is relative to the species directory, details is JSON.
CREATE TABLE resources (
    species VARCHAR NOT NULL,
    assembly VARCHAR NOT NULL,
    source_version VARCHAR NOT NULL,
    resource_id VARCHAR PRIMARY KEY,
    kind VARCHAR NOT NULL,
    path VARCHAR NOT NULL,
    details VARCHAR NOT NULL,
    source_db VARCHAR NOT NULL
);
