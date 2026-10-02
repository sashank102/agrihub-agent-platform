"""LangChain tools over the heavy tier: variant annotation, LD, homeologs and haplotypes.

Missing binaries or caches never fail a call: ``annotate_variants`` always
returns the gene-model location class and says why consequences are
missing, and ``ld_with_lead`` reports an "unavailable" gap per lead.
"""

from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from agrihub.tools.bundle_tools import _as, _bundle, _clip, _respond, _run
from agrihub.tools.extended_tools import _gap
from agrihub_data.query import duplication, ld
from agrihub_data.query import variants as variant_query
from agrihub_data.registry import load_species


class LeadSnp(BaseModel):
    """A lead SNP to compute LD for."""

    label: str = Field(description="SNP id such as S18_9263941; used as the evidence key of the LD window.")
    chrom: str = Field(description="Chromosome in any registered spelling.")
    pos: int = Field(ge=1, description="Position on the canonical assembly.")


@tool(response_format="content_and_artifact", parse_docstring=True)
async def annotate_variants(
    variants: list[variant_query.VariantInput],
    config: RunnableConfig,
    species: str = "soybean",
    assembly: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Classify variants against gene models (CDS, splice, UTR, intron, upstream 2 kb, downstream, intergenic) and, when REF/ALT are given, predict consequences with VEP (SnpEff fallback).

    Args:
        variants: Variants with id, chrom and pos; add ref and alt for consequences.
        species: Registered species, e.g. soybean.
        assembly: Assembly of the positions; defaults to the canonical assembly.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        rows, notes = variant_query.annotate_variants(bundle, _as(variant_query.VariantInput, variants), assembly)
        return _respond(
            config,
            "annotate_variants",
            f"annotate_variants {len(variants)} variants: {len(rows)} gene relations"
            + (f"; consequences by {rows[0].method}" if rows and rows[0].method != "gene_models" else ""),
            rows,
            lambda row: (
                f"{row.variant} {row.chrom}:{row.pos} "
                + (f"{row.gene_id} {row.location_class}" if row.gene_id else f"intergenic ({row.note})")
                + (f" {row.distance_bp} bp" if row.distance_bp and row.gene_id else "")
                + (f" | {','.join(row.consequences[0].terms)} {row.impact}" if row.consequences else "")
                + (f" {row.consequences[0].amino_acids}" if row.consequences and row.consequences[0].amino_acids else "")
            ),
            evidence=True,
            notes=[*notes, *_gap(bundle, "variant_consequence")],
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def ld_with_lead(
    leads: list[LeadSnp],
    config: RunnableConfig,
    vcf_ref: str | None = None,
    window_kb: int = ld.DEFAULT_WINDOW_KB,
    r2_min: float = ld.DEFAULT_R2,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Compute LD (PLINK2 r2) between each lead SNP and the panel SNPs within window_kb: the LD window and each gene's best r2 with the lead.

    A lead that is not a panel site is tested through the nearest common panel SNP within 50 kb, which is reported.

    Args:
        leads: Lead SNPs (label, chrom, pos) on the canonical assembly.
        vcf_ref: A study VCF in the species genotypes directory, or panel:<name>; default the bundled SoySNP50K panel.
        window_kb: Maximum distance from the lead, in kb.
        r2_min: Minimum r2 that counts as linked.
        species: Registered species, e.g. soybean.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        canonical = load_species(species).canonical_assembly
        rows: list[BaseModel] = []
        notes: list[str] = []
        for lead in _as(LeadSnp, leads):
            try:
                window = ld.ld_window(bundle, lead.label, lead.chrom, lead.pos, vcf_ref=vcf_ref, window_kb=window_kb, r2=r2_min)
            except ld.LdUnavailableError as exc:
                notes.append(f"unavailable for {lead.label}: {exc}")
                continue
            rows.append(window)
            genes = [
                (str(gene_id), int(start), int(end))
                for gene_id, start, end in bundle.rows_raw(
                    'SELECT gene_id, start, "end" FROM genes WHERE assembly = ? AND chrom = ? AND "end" >= ? AND start <= ? ORDER BY start',
                    [canonical, window.chrom, window.start, window.end],
                )
            ]
            rows.extend(sorted(ld.gene_ld(window, genes), key=lambda row: -row.r2))
            if window.note:
                notes.append(f"{lead.label}: {window.note}")
        return _respond(
            config,
            "ld_with_lead",
            f"ld_with_lead {len(leads)} leads, r2 >= {r2_min:g} within {window_kb} kb",
            rows,
            _ld_line,
            evidence=True,
            notes=[*notes, *_gap(bundle, "ld")],
        )

    return await _run(work)


def _ld_line(row: Any) -> str:
    if isinstance(row, ld.LdWindow):
        return (
            f"{row.lead} LD window {row.chrom}:{row.start}-{row.end} ({row.span_bp / 1000:.0f} kb; LD {row.ld_start}-{row.ld_end}, "
            f"{row.n_partners} partners on {row.genotypes}, tested {row.tested_variant})"
        )
    return f"{row.gene_id} r2={row.r2:g} with {row.lead}" + (f" via {row.via}" if row.via else " (tested SNP in gene)")


@tool(response_format="content_and_artifact", parse_docstring=True)
async def homeologs(
    gene_ids: list[str],
    config: RunnableConfig,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Return each gene's homeolog from the recent soybean genome duplication (synteny block plus shared PANTHER family).

    Args:
        gene_ids: Canonical-assembly gene ids.
        species: Registered species, e.g. soybean.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        rows = duplication.homeologs(bundle, gene_ids)
        found = {row.gene_id for row in rows}
        return _respond(
            config,
            "homeologs",
            f"homeologs {len(found)} of {len(set(gene_ids))} genes have a recent-duplication homeolog",
            rows,
            lambda row: f"{row.gene_id} <-> {row.homeolog_id} ({row.homeolog_chrom}) {row.family} Ks={row.median_ks} offset={row.offset_bp} | {_clip(row.homeolog_defline, 60)}",
            evidence=True,
            notes=[*([f"no homeolog in a recent-duplication block: {', '.join(gene for gene in dict.fromkeys(gene_ids) if gene not in found)}"] if len(found) < len(set(gene_ids)) else []), *_gap(bundle, "homeologs")],
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def gene_haplotypes(
    gene_ids: list[str],
    config: RunnableConfig,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Return GmHapMap haplotypes per gene (SNPs defining them) and the gene's non-synonymous SNPs with their allele frequency.

    Args:
        gene_ids: Canonical-assembly gene ids.
        species: Registered species, e.g. soybean.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        rows = duplication.gene_haplotypes(bundle, gene_ids)
        found = {row.gene_id for row in rows}
        return _respond(
            config,
            "gene_haplotypes",
            f"gene_haplotypes {len(found)} of {len(set(gene_ids))} genes in GmHapMap",
            rows,
            lambda row: (
                f"{row.gene_id} {len(row.haplotypes)} haplotypes ({','.join(row.haplotypes[:6])}) from {row.n_snps} SNPs; "
                f"{len(row.nonsynonymous)} non-synonymous"
                + (f" ({', '.join(f'{snp.variant_id}@{snp.pos} {snp.ref}>{snp.alt} AF={snp.alt_freq}' for snp in row.nonsynonymous[:3])})" if row.nonsynonymous else "")
            ),
            evidence=True,
            notes=[*([f"not in GmHapMap: {', '.join(gene for gene in dict.fromkeys(gene_ids) if gene not in found)}"] if len(found) < len(set(gene_ids)) else []), *_gap(bundle, "haplotypes")],
        )

    return await _run(work)


VARIANT_TOOLS: tuple[BaseTool, ...] = (annotate_variants, ld_with_lead, homeologs, gene_haplotypes)
for _tool in VARIANT_TOOLS:
    _tool.handle_tool_error = True
