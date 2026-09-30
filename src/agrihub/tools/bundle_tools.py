"""LangChain tools over the offline species bundles.

Every tool is async. Its bundle queries, evidence writes and rendering run in
one worker thread (``asyncio.to_thread``), so a large query never blocks the
event loop that serves run streams and parallel specialists.

Tools accept lists (batch-first). Evidence tools store one ``EvidenceItem``
per fact in the run's evidence store and return compact text: a header, at
most ``MAX_ROWS`` lines each starting with that row's ``E<n>`` aliases, and a
footer with ``truncated`` and ``output_ref``. The full rows are kept in the
store under ``output_ref``. The tool artifact repeats ``evidence_ids``,
``aliases``, ``truncated``, ``output_ref`` and ``total`` for run events.
"""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import BaseModel, Field

from agrihub.evidence_store import EvidenceStore
from agrihub.state import EvidenceItem
from agrihub.tools.store_tools import store_for_config
from agrihub_data.bundle import Bundle, BundleMissingError, open_bundle
from agrihub_data.query import annotation, ids, loci, orthology, overlap, traits
from agrihub_data.query.common import AssemblyMismatchError, Region
from agrihub_data.registry import (
    UnknownAssemblyError,
    UnknownChromosomeError,
    UnknownSpeciesError,
    load_species,
)
from agrihub_data.registry import (
    normalize_chrom as registry_normalize_chrom,
)

MAX_ROWS = 40
DEFINE_TEXT = 80
Row = TypeVar("Row", bound=BaseModel)
_EXPECTED_ERRORS = (
    AssemblyMismatchError,
    BundleMissingError,
    UnknownAssemblyError,
    UnknownChromosomeError,
    UnknownSpeciesError,
    ValueError,
)


class WindowInput(BaseModel):
    """A labeled window; ``snp_pos`` enables distances to the lead SNP."""

    chrom: str = Field(description="Chromosome in any registered spelling, e.g. Gm18, Chr18 or 18.")
    start: int = Field(ge=1, description="1-based inclusive start.")
    end: int = Field(ge=1, description="1-based inclusive end.")
    label: str | None = Field(default=None, description="Locus id such as L1; used as the evidence gene_id for locus facts.")
    snp_pos: int | None = Field(default=None, ge=1, description="Lead SNP position inside the window.")
    assembly: str | None = Field(default=None, description="Assembly of these coordinates; defaults to the canonical one.")

    def region(self) -> Region:
        """Return the query-layer region."""
        return Region(
            label=self.label,
            chrom=self.chrom,
            start=self.start,
            end=self.end,
            snp_pos=self.snp_pos,
            assembly=self.assembly,
        )


class SnpPosition(BaseModel):
    """A SNP position to turn into a fixed window."""

    chrom: str
    pos: int = Field(ge=1)
    label: str | None = None


class NormalizedChrom(BaseModel):
    """One chromosome name and its canonical form, or why it has none."""

    query: str
    chrom: str | None
    error: str | None = None


class DefinedLocus(BaseModel):
    """A labeled fixed window."""

    label: str
    window: loci.LocusWindow


def _as(model: type[Row], values: Sequence[Any] | None) -> list[Row]:
    return [value if isinstance(value, model) else model.model_validate(value) for value in values or []]


def _species(species: str) -> str:
    return load_species(species).species


def _optional_store(config: RunnableConfig) -> EvidenceStore | None:
    if not (config.get("configurable") or {}).get("run_id"):
        return None
    return store_for_config(config)


def _alias_ranges(aliases: list[str]) -> str:
    """Render ``E3, E4, E5, E9`` as ``E3..E5,E9``."""
    numbers = sorted({int(alias[1:]) for alias in aliases if alias and alias[1:].isdigit()})
    parts: list[str] = []
    index = 0
    while index < len(numbers):
        end = index
        while end + 1 < len(numbers) and numbers[end + 1] == numbers[end] + 1:
            end += 1
        parts.append(f"E{numbers[index]}" if end == index else f"E{numbers[index]}..E{numbers[end]}")
        index = end + 1
    return ",".join(parts) or "-"


def _clip(text: str | None, limit: int = DEFINE_TEXT) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _respond(
    config: RunnableConfig,
    tool_name: str,
    header: str,
    rows: Sequence[BaseModel],
    line: Callable[[Any], str],
    *,
    evidence: bool,
    notes: Sequence[str] = (),
) -> tuple[str, dict[str, Any]]:
    """Store evidence and the full result, then render at most ``MAX_ROWS`` lines."""
    store = store_for_config(config) if evidence else _optional_store(config)
    per_row: list[list[EvidenceItem]] = [list(getattr(row, "evidence")()) for row in rows] if evidence else []
    aliases: list[list[str]] = [[] for _ in rows]
    evidence_ids: list[str] = []
    if evidence and store is not None:
        saved = store.put_items(item for items in per_row for item in items)
        offset = 0
        for index, items in enumerate(per_row):
            chunk = saved[offset : offset + len(items)]
            offset += len(items)
            aliases[index] = [str(item.alias) for item in chunk]
            evidence_ids.extend(str(item.evidence_id) for item in chunk)
    output_ref = None
    if store is not None:
        output_ref = store.put_output(
            tool_name,
            {"header": header, "rows": [row.model_dump(mode="json") for row in rows]},
        )
    shown = rows[:MAX_ROWS]
    truncated = len(rows) > MAX_ROWS
    lines = [header, *notes]
    for index, row in enumerate(shown):
        prefix = f"{_alias_ranges(aliases[index])} " if evidence else ""
        lines.append(prefix + line(row))
    lines.append(
        f"truncated: {str(truncated).lower()} (showing {len(shown)} of {len(rows)}); "
        f"output_ref: {output_ref or 'none'}"
    )
    artifact = {
        "evidence_ids": list(dict.fromkeys(evidence_ids)),
        "aliases": list(dict.fromkeys(alias for row_aliases in aliases for alias in row_aliases)),
        "output_ref": output_ref,
        "truncated": truncated,
        "total": len(rows),
    }
    return "\n".join(lines), artifact


async def _run(work: Callable[[], tuple[str, dict[str, Any]]]) -> tuple[str, dict[str, Any]]:
    try:
        return await asyncio.to_thread(work)
    except _EXPECTED_ERRORS as exc:
        raise ToolException(str(exc)) from exc
    except KeyError as exc:
        raise ToolException(str(exc.args[0]) if exc.args else str(exc)) from exc


def _bundle(species: str) -> Bundle:
    return open_bundle(_species(species))


def _profile(bundle: Bundle, trait: str | None) -> traits.TraitProfile | None:
    return traits.map_trait(trait, bundle.species, bundle) if trait else None


def _windows_or_genes(
    bundle: Bundle,
    windows: Sequence[Any] | None,
    gene_ids: Sequence[str] | None,
    assembly: str | None,
    flank_bp: int = 0,
) -> list[Region]:
    regions = [window.region() for window in _as(WindowInput, windows)]
    if gene_ids:
        registry = load_species(bundle.species)
        target = registry.assembly(assembly).id
        wanted = list(dict.fromkeys(gene_ids))
        rows = bundle.rows(
            'SELECT gene_id, chrom, start, "end" FROM genes WHERE assembly = ? '
            f"AND gene_id IN ({', '.join('?' for _ in wanted)})",
            [target, *wanted],
        )
        found = {row["gene_id"]: row for row in rows}
        missing = [gene for gene in wanted if gene not in found]
        if missing:
            raise ValueError(f"not genes on {target}: {', '.join(missing[:10])}; use map_gene_ids first")
        regions.extend(
            Region(
                label=gene,
                chrom=found[gene]["chrom"],
                start=max(1, int(found[gene]["start"]) - flank_bp),
                end=int(found[gene]["end"]) + flank_bp,
                assembly=target,
            )
            for gene in wanted
        )
    if not regions:
        raise ValueError("give windows or gene_ids")
    return regions


@tool(response_format="content_and_artifact", parse_docstring=True)
async def normalize_chrom(
    chroms: list[str],
    config: RunnableConfig,
    species: str = "soybean",
    assembly: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Map chromosome names from any source (Gm18, Chr18, chr18, 18, glyma.Wm82.gnm2.Gm18) to canonical names.

    Args:
        chroms: Chromosome names to normalize.
        species: Registered species, e.g. soybean.
        assembly: Assembly the names belong to; defaults to the canonical assembly.
    """

    def work() -> tuple[str, dict[str, Any]]:
        name = _species(species)
        rows = []
        for chrom in chroms:
            try:
                rows.append(NormalizedChrom(query=chrom, chrom=registry_normalize_chrom(name, chrom, assembly)))
            except UnknownChromosomeError as exc:
                rows.append(NormalizedChrom(query=chrom, chrom=None, error=str(exc)))
        target = load_species(name).assembly(assembly).id
        return _respond(
            config,
            "normalize_chrom",
            f"normalize_chrom {name} {target}: {len(rows)} names",
            rows,
            lambda row: f"{row.query} -> {row.chrom}" if row.chrom else f"{row.query} -> ERROR {row.error}",
            evidence=False,
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def resolve_marker(
    markers: list[str],
    config: RunnableConfig,
    species: str = "soybean",
    assembly_hint: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Place SNP or marker ids (S18_9263941, Chr18:9263941, 18 9263941, ss715..., BARC_..., Satt...) on assemblies.

    BARC SNP names embed Wm82.a1 positions; those placements are flagged
    a1_embedded_position and must not be compared with a2 coordinates.

    Args:
        markers: Marker or positional SNP ids.
        species: Registered species, e.g. soybean.
        assembly_hint: Assembly for positional ids; defaults to the canonical assembly.
    """

    def work() -> tuple[str, dict[str, Any]]:
        name = _species(species)
        try:
            bundle: Bundle | None = open_bundle(name)
        except BundleMissingError:
            bundle = None
        rows: list[ids.MarkerHit] = []
        unresolved = []
        for marker in markers:
            hits = ids.resolve_marker(name, marker, assembly_hint, bundle)
            rows.extend(hits)
            if not hits:
                unresolved.append(marker)
        notes = [f"not found: {', '.join(unresolved)}"] if unresolved else []
        return _respond(
            config,
            "resolve_marker",
            f"resolve_marker {name}: {len(markers)} ids, {len(rows)} placements",
            rows,
            lambda hit: (
                f"{hit.query} -> {hit.assembly} {hit.chrom}:{hit.pos}"
                + (f" [{hit.marker_id}]" if hit.marker_id != hit.query else "")
                + (f" set={hit.marker_set}" if hit.marker_set else "")
                + (f" flags={','.join(hit.flags)}" if hit.flags else "")
            ),
            evidence=False,
            notes=notes,
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def map_gene_ids(
    gene_ids: list[str],
    config: RunnableConfig,
    species: str = "soybean",
    to_assembly: str | None = None,
    from_assembly: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Map gene ids between assemblies and namespaces (GLYMA_, glyma.Wm82.gnmN.ann1., Glyma18g..., transcripts).

    Args:
        gene_ids: Gene or transcript ids in any supported namespace.
        species: Registered species, e.g. soybean.
        to_assembly: Target assembly; defaults to the canonical assembly.
        from_assembly: Assembly of bare Glyma.NNGNNNNNN ids; defaults to the canonical assembly.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        rows = ids.map_gene_ids(bundle, gene_ids, to_assembly, from_assembly=from_assembly)
        return _respond(
            config,
            "map_gene_ids",
            f"map_gene_ids -> {rows[0].to_assembly if rows else to_assembly}: {len(rows)} mappings",
            rows,
            lambda row: f"{row.query} ({row.from_assembly}) -> {row.to_id or 'none'} via {row.relation}"
            + ("" if row.exists or row.to_id is None else " (not in bundle)"),
            evidence=False,
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def liftover(
    windows: list[WindowInput],
    from_assembly: str,
    to_assembly: str,
    config: RunnableConfig,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Lift windows between assemblies through pangene/ancestor gene anchors, with marker anchors as fallback.

    Args:
        windows: Windows on from_assembly.
        from_assembly: Assembly of the input coordinates.
        to_assembly: Assembly to lift to.
        species: Registered species, e.g. soybean.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        rows = ids.liftover(bundle, [window.region() for window in _as(WindowInput, windows)], from_assembly, to_assembly)
        return _respond(
            config,
            "liftover",
            f"liftover {from_assembly} -> {to_assembly}: {len(rows)} windows",
            rows,
            lambda row: (
                f"{row.source.name} {row.source.chrom}:{row.source.start}-{row.source.end} -> "
                + (f"{row.chrom}:{row.start}-{row.end}" if row.chrom else "not lifted")
                + f" method={row.method} anchors={row.n_anchors} genes={len(row.genes)}"
                + (f" ({row.note})" if row.note else "")
            ),
            evidence=False,
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def define_locus(
    snps: list[SnpPosition],
    config: RunnableConfig,
    species: str = "soybean",
    flank_bp: int | None = None,
    assembly: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Turn SNP positions into fixed pos ± flank windows clamped to the chromosome (species default flank).

    Args:
        snps: SNP positions.
        species: Registered species, e.g. soybean.
        flank_bp: Flank on each side; defaults to the species window (soybean 250000).
        assembly: Assembly of the positions; defaults to the canonical assembly.
    """

    def work() -> tuple[str, dict[str, Any]]:
        name = _species(species)
        rows = [
            DefinedLocus(
                label=snp.label or f"{snp.chrom}:{snp.pos}",
                window=loci.define_locus(name, snp.chrom, snp.pos, "fixed", flank_bp, assembly),
            )
            for snp in _as(SnpPosition, snps)
        ]
        return _respond(
            config,
            "define_locus",
            f"define_locus {name}: {len(rows)} windows",
            rows,
            lambda row: (
                f"{row.label} {row.window.assembly} {row.window.chrom}:{row.window.start}-{row.window.end} "
                f"(±{row.window.flank_bp}{', clamped' if row.window.clamped else ''})"
                + (f" WARNING {row.window.warning}" if row.window.warning else "")
            ),
            evidence=False,
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def genes_in_window(
    windows: list[WindowInput],
    config: RunnableConfig,
    species: str = "soybean",
    assembly: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """List genes overlapping each window with distance to the SNP, SNP-in-gene flag and defline.

    Args:
        windows: Windows to scan; set snp_pos for distances.
        species: Registered species, e.g. soybean.
        assembly: Assembly of the windows; defaults to the canonical assembly.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        rows = [
            gene
            for window in _as(WindowInput, windows)
            for gene in loci.genes_in_window(bundle, window.region(), assembly)
        ]
        many = len(windows) > 1
        return _respond(
            config,
            "genes_in_window",
            f"genes_in_window {len(windows)} windows: {len(rows)} genes",
            rows,
            lambda gene: (
                (f"[{gene.window}] " if many else "")
                + f"{gene.gene_id} {gene.chrom}:{gene.start}-{gene.end}({gene.strand})"
                + (f" dist={gene.dist_to_snp}" if gene.dist_to_snp is not None else "")
                + (" SNP-in-gene" if gene.overlaps_snp else "")
                + f" | {_clip(gene.defline)}"
            ),
            evidence=True,
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def gene_annotation(
    gene_ids: list[str],
    config: RunnableConfig,
    species: str = "soybean",
    assembly: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return defline, symbols, Pfam/PANTHER/KOG/InterPro, GO and Arabidopsis/rice best hits per gene.

    Args:
        gene_ids: Gene ids on the assembly.
        species: Registered species, e.g. soybean.
        assembly: Assembly of the ids; defaults to the canonical assembly.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        rows = annotation.gene_annotation(bundle, gene_ids, assembly)
        missing = [row.gene_id for row in rows if not row.found]
        return _respond(
            config,
            "gene_annotation",
            f"gene_annotation {len(rows) - len(missing)} of {len(rows)} genes found",
            [row for row in rows if row.found],
            _annotation_line,
            evidence=True,
            notes=[f"not found: {', '.join(missing)}"] if missing else [],
        )

    return await _run(work)


def _annotation_line(row: annotation.GeneAnnotation) -> str:
    parts = [row.gene_id]
    if row.symbols:
        parts.append("/".join(row.symbols[:3]))
    parts.append(f"| {_clip(row.defline, 70)}")
    for kind in ("pfam", "panther", "interpro"):
        terms = row.domains.get(kind) or []
        if terms:
            parts.append(f"{kind}:" + ",".join(term.id for term in terms[:3]))
    if row.go:
        parts.append("GO:" + ",".join(f"{term.id}({_clip(term.name, 30)})" for term in row.go[:4]))
    if row.arabidopsis_best_hit:
        parts.append(f"At:{row.arabidopsis_best_hit.id} {_clip(row.arabidopsis_best_hit.label, 40)}")
    return " ".join(parts)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def get_orthologs(
    gene_ids: list[str],
    config: RunnableConfig,
    species: str = "soybean",
    targets: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return Compara + PLAZA + BLAST-best-hit consensus orthologs with relation, n_methods and confidence.

    Args:
        gene_ids: Canonical-assembly gene ids.
        species: Registered species, e.g. soybean.
        targets: Target species; defaults to arabidopsis.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        rows = orthology.get_orthologs(bundle, gene_ids, targets)
        return _respond(
            config,
            "get_orthologs",
            f"get_orthologs {len(gene_ids)} genes: {len(rows)} calls",
            rows,
            lambda call: (
                f"{call.gene_id} -> {call.target_gene_id}"
                + (f" ({call.target_symbol})" if call.target_symbol else "")
                + f" {call.relation} {call.confidence} n_methods={call.n_methods} [{','.join(call.methods)}]"
                + (f" id={call.identity:.0f}%" if call.identity is not None else "")
            ),
            evidence=True,
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def arabidopsis_knowledge(
    ids_or_genes: list[str],
    config: RunnableConfig,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Return TAIR descriptions, mutant phenotypes and experimental GO for AGIs or for soybean genes via their best ortholog.

    Args:
        ids_or_genes: AGI ids (AT1G80840) or soybean gene ids.
        species: Registered species whose bundle holds the TAIR tables.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        rows = orthology.arabidopsis_knowledge(bundle, ids_or_genes)
        reached = {row.gene_id for row in rows}
        skipped = [item for item in ids_or_genes if item not in reached and item.upper() not in reached]
        notes = [f"no medium/high-confidence Arabidopsis ortholog: {', '.join(skipped)}"] if skipped else []
        return _respond(
            config,
            "arabidopsis_knowledge",
            f"arabidopsis_knowledge {len(rows)} genes",
            rows,
            lambda row: (
                f"{row.gene_id}"
                + (f" via {row.agi} ({row.via.confidence}, n_methods={row.via.n_methods})" if row.via else "")
                + (f" {'/'.join(row.symbols[:3])}" if row.symbols else "")
                + f": {_clip(row.curator_summary or row.short_description or row.computational_description, 100)}"
                + f" | phenotypes={row.n_phenotypes} expGO={len(row.go)} papers={row.n_publications}"
            ),
            evidence=True,
            notes=notes,
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def map_trait(
    trait: str,
    config: RunnableConfig,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Map trait text to ontology terms (TO, SOY, CO_336, PPTO, GO, PO), keywords and seed gene families.

    Args:
        trait: Free trait text, e.g. plant height.
        species: Registered species, e.g. soybean.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        profile = traits.map_trait(trait, bundle.species, bundle)
        notes = [f"keywords: {', '.join(profile.keywords)}"]
        if profile.seed_families:
            notes.append(f"seed families: {', '.join(profile.seed_families)}")
        return _respond(
            config,
            "map_trait",
            f"map_trait '{trait}' -> profile {profile.key}: {len(profile.terms)} terms, "
            f"{len(profile.expanded_ids)} with descendants",
            profile.terms,
            lambda term: f"{term.term_id} {term.name} [{term.origin} {term.score:g}]",
            evidence=False,
            notes=notes,
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def annotation_relevance(
    gene_ids: list[str],
    trait: str,
    config: RunnableConfig,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """Score how each gene's GO terms and descriptions match the trait profile (experimental GO 1.0, IEA 0.3, keyword 0.5).

    Args:
        gene_ids: Canonical-assembly gene ids.
        trait: Trait text; mapped with map_trait.
        species: Registered species, e.g. soybean.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        profile = traits.map_trait(trait, bundle.species, bundle)
        rows = annotation.annotation_relevance(bundle, gene_ids, profile)
        return _respond(
            config,
            "annotation_relevance",
            f"annotation_relevance '{trait}' ({profile.key}): {sum(1 for row in rows if row.matches)} of {len(rows)} genes match",
            rows,
            lambda row: f"{row.gene_id} score={row.score:g} "
            + (
                " ".join(
                    f"{match.kind}:{match.term}" + (f"({_clip(match.label, 30)})" if match.label else "")
                    for match in row.matches[:6]
                )
                or "no match"
            ),
            evidence=True,
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def qtl_overlap(
    config: RunnableConfig,
    windows: list[WindowInput] | None = None,
    gene_ids: list[str] | None = None,
    trait: str | None = None,
    species: str = "soybean",
    assembly: str | None = None,
    trait_only: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Find marker-placed QTLs overlapping windows or genes, with overlap type, span and trait match.

    Args:
        windows: Windows (label them with locus ids); evidence is keyed to the label.
        gene_ids: Genes to test instead of or besides windows; evidence is keyed to the gene.
        trait: Trait text; flags QTLs whose ontology terms or names match.
        species: Registered species, e.g. soybean.
        assembly: Assembly of the windows; other assemblies are refused, never compared.
        trait_only: Return only trait-matching QTLs.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        profile = _profile(bundle, trait)
        rows = [
            hit
            for region in _windows_or_genes(bundle, windows, gene_ids, assembly)
            for hit in overlap.qtl_overlap(bundle, region, profile, assembly=assembly, trait_only=trait_only)
        ]
        matching = sum(1 for row in rows if row.trait_match != "none")
        return _respond(
            config,
            "qtl_overlap",
            f"qtl_overlap: {len(rows)} QTLs, {matching} match '{trait}'" if trait else f"qtl_overlap: {len(rows)} QTLs",
            rows,
            lambda hit: (
                f"[{hit.label}] {hit.qtl_name} '{hit.trait_name}' {hit.chrom}:{hit.start}-{hit.end} "
                f"span={hit.span_bp / 1e6:.2f}Mb {hit.overlap_type} markers={hit.n_markers_placed}/{hit.n_markers}"
                + f" match={hit.trait_match}"
                + (" WIDE" if hit.wide else "")
                + f" study={hit.study_id}"
            ),
            evidence=True,
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def gwas_catalog_overlap(
    config: RunnableConfig,
    windows: list[WindowInput] | None = None,
    gene_ids: list[str] | None = None,
    trait: str | None = None,
    max_p: float | None = None,
    species: str = "soybean",
    assembly: str | None = None,
    trait_only: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Find published GWAS hits (LIS/SoyBase, SoyBase GWAS, GWAS Atlas) inside windows or genes.

    GWAS Atlas is academic-use only; cite it as such.

    Args:
        windows: Windows (label them with locus ids); evidence is keyed to the label.
        gene_ids: Genes to test; evidence is keyed to the gene.
        trait: Trait text; flags hits whose trait terms or names match.
        max_p: Drop hits with a larger p-value; hits without p-values are kept.
        species: Registered species, e.g. soybean.
        assembly: Assembly of the windows; other assemblies are refused, never compared.
        trait_only: Return only trait-matching hits.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        profile = _profile(bundle, trait)
        rows = [
            hit
            for region in _windows_or_genes(bundle, windows, gene_ids, assembly)
            for hit in overlap.gwas_catalog_overlap(
                bundle, region, profile, max_p=max_p, assembly=assembly, trait_only=trait_only
            )
        ]
        matching = sum(1 for row in rows if row.trait_match != "none")
        return _respond(
            config,
            "gwas_catalog_overlap",
            f"gwas_catalog_overlap: {len(rows)} hits, {matching} match '{trait}'" if trait else f"gwas_catalog_overlap: {len(rows)} hits",
            rows,
            lambda hit: (
                f"[{hit.label}] {hit.source_db} '{hit.trait_name}' {hit.chrom}:{hit.pos}"
                + (f" p={hit.p_value:.2g}" if hit.p_value is not None else "")
                + (f" dist={hit.distance_to_snp}" if hit.distance_to_snp is not None else "")
                + f" match={hit.trait_match} study={_clip(hit.study_id, 40)}"
                + (f" PMID:{hit.pmid}" if hit.pmid else "")
            ),
            evidence=True,
        )

    return await _run(work)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def known_trait_genes(
    config: RunnableConfig,
    trait: str | None = None,
    windows: list[WindowInput] | None = None,
    gene_ids: list[str] | None = None,
    species: str = "soybean",
    assembly: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """List curated trait genes (LIS glyma.traits.yml) matching a trait, inside windows, or among given genes.

    Args:
        trait: Trait text; required when no windows or gene_ids are given.
        windows: Windows to search for curated genes.
        gene_ids: Genes to look up.
        species: Registered species, e.g. soybean.
        assembly: Assembly of the windows; other assemblies are refused.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        profile = _profile(bundle, trait)
        if profile is None and not windows and not gene_ids:
            raise ValueError("give a trait, windows or gene_ids")
        if windows:
            rows = [
                hit
                for window in _as(WindowInput, windows)
                for hit in overlap.known_trait_genes(bundle, profile, region=window.region(), assembly=assembly)
            ]
        else:
            rows = overlap.known_trait_genes(bundle, profile, gene_ids=list(gene_ids) if gene_ids else None, assembly=assembly)
        canonical = load_species(bundle.species).canonical_assembly
        return _respond(
            config,
            "known_trait_genes",
            f"known_trait_genes: {len(rows)} curated genes" + (f" for '{trait}'" if trait else ""),
            rows,
            lambda hit: (
                (f"[{hit.label}] " if hit.label else "")
                + f"{hit.gene_id} {'/'.join(hit.symbols[:3])}"
                + (f" {hit.chrom}:{hit.start}-{hit.end}" if hit.chrom else "")
                + ("" if hit.assembly == canonical else f" (only on {hit.assembly}; no {canonical} model)")
                + (f" dist={hit.distance_to_snp}" if hit.distance_to_snp is not None else "")
                + f" match={hit.trait_match}"
                + (f"({','.join(hit.matched[:3])})" if hit.matched else "")
                + (f" PMID:{hit.pmids[0]}" if hit.pmids else "")
                + f" | {_clip(hit.synopsis, 70)}"
            ),
            evidence=True,
        )

    return await _run(work)


BUNDLE_TOOLS: tuple[BaseTool, ...] = (
    normalize_chrom,
    resolve_marker,
    map_gene_ids,
    liftover,
    define_locus,
    genes_in_window,
    gene_annotation,
    get_orthologs,
    arabidopsis_knowledge,
    map_trait,
    annotation_relevance,
    qtl_overlap,
    gwas_catalog_overlap,
    known_trait_genes,
)
for _tool in BUNDLE_TOOLS:
    _tool.handle_tool_error = True
