"""Positioned QTL tables: Gramene ``Rice_QTL.dat`` and Sorghum QTL Atlas exports.

Neither table states its assembly in the file, so both are checked before
loading: every interval must lie on the registered chromosome lengths of the
assembly the source names, and the build fails when more than
``params.max_out_of_bounds`` (2%) do not. Positions are projected from
flanking markers by the curators, so rows carry ``placement =
'reported_low_precision'``.

- ``gramene_qtl``: tab-separated ``qtl_accession_id, qtl_name,
  published_symbol, to_accession, trait_category, trait_name, trait_symbol,
  chromosome ("Chr. 1"), start, end``. ``study_id`` is the accession prefix
  (one Gramene study per prefix, ``AQGJ``).
- ``sorghum_qtl_atlas``: a manual export. Either the ``SorghumQtlAtlas.db``
  SQLite file ``jlboat/query_qtl_atlas`` builds (table ``atlas``) or a
  tab/comma-separated export with the same columns: ``QTL Id``,
  ``Publication``, ``Population``, ``Trait Description``, ``LG:Start-End
  (v3.0)`` and optionally ``Genes Under QTL (v3.0)`` and ``Type``. Rows whose
  type or id names a major effect gene become ``known_genes`` through the
  genes under them; the others become QTL.
"""

import csv
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agrihub_data.build.context import BuildContext, BuildError, open_text, tsv_rows
from agrihub_data.build.resolve import GeneResolver
from agrihub_data.registry import Source

_GRAMENE_CHROM = re.compile(r"^Chr\.\s*(\d+)$")
_ATLAS_LOCATION = re.compile(r"^\s*(\d+)\D+(\d+)\D+(\d+)\s*$")
_STUDY_PREFIX = re.compile(r"^([A-Z]+)")
_MAJOR = re.compile(r"major", re.IGNORECASE)


def build_gramene_qtl(ctx: BuildContext, source: Source) -> None:
    """Load Gramene rice QTL with their Trait Ontology terms, after an assembly bounds check."""
    assembly = source.assemblies[0] if source.assemblies else ctx.registry.canonical_assembly
    base = ctx.base(source, assembly)
    rows: list[dict[str, Any]] = []
    traits: set[tuple[str, str]] = set()
    total = outside = 0
    for fields in tsv_rows(ctx.file(source, "*QTL*")):
        if len(fields) < 10 or fields[0] == "qtl_accession_id":
            continue
        match = _GRAMENE_CHROM.match(fields[7].strip())
        chrom = ctx.chrom(assembly, match.group(1)) if match else None
        if chrom is None or not fields[8].strip().isdigit() or not fields[9].strip().isdigit():
            ctx.count(source.id, "unplaced")
            continue
        total += 1
        start, end = sorted((int(fields[8]), int(fields[9])))
        if not ctx.in_bounds(assembly, chrom, start, end):
            outside += 1
            continue
        accession, term = fields[0].strip(), fields[3].strip()
        trait = fields[5].strip() or fields[6].strip()
        if term:
            traits.add((trait, term))
        study = _STUDY_PREFIX.match(accession)
        rows.append(
            {
                **base,
                "qtl_id": f"Gramene|{accession}",
                "study_id": study.group(1) if study else accession,
                "qtl_name": fields[2].strip() or fields[1].strip() or accession,
                "trait_name": trait,
                "trait_terms": [term] if term.startswith("TO:") else [],
                "genetic_map": None,
                "linkage_group": None,
                "cm_start": None,
                "cm_end": None,
                "cm_peak": None,
                "chrom": chrom,
                "start": start,
                "end": end,
                "span_bp": end - start + 1,
                "n_markers": 0,
                "n_markers_placed": 0,
                "placement": "reported_low_precision",
                "publication_doi": None,
                "source_db": "Gramene QTL",
            }
        )
    _check_bounds(ctx, source, assembly, total, outside)
    ctx.count(source.id, "qtl", ctx.insert("qtl", rows))
    ctx.count(source.id, "trait_map", ctx.insert("trait_map", _trait_rows(ctx, source, traits, "Gramene QTL")))


def build_sorghum_qtl_atlas(ctx: BuildContext, source: Source) -> None:
    """Load a manual Sorghum QTL Atlas export (v3 coordinates): QTL, GWAS loci and major genes."""
    files = ctx.files(source)
    if not files:
        ctx.count(source.id, "manual_export_not_provided")
        return
    assembly = ctx.registry.canonical_assembly
    resolver = GeneResolver.load(ctx)
    base = ctx.base(source, assembly)
    qtl_rows: list[dict[str, Any]] = []
    gene_rows: list[dict[str, Any]] = []
    total = outside = 0
    for record in (row for path in files for row in _atlas_records(path)):
        location = _ATLAS_LOCATION.match(str(record.get("LG:Start-End (v3.0)") or ""))
        chrom = ctx.chrom(assembly, location.group(1)) if location else None
        if location is None or chrom is None:
            ctx.count(source.id, "unplaced")
            continue
        total += 1
        start, end = sorted((int(location.group(2)), int(location.group(3))))
        if not ctx.in_bounds(assembly, chrom, start, end):
            outside += 1
            continue
        qtl_id = str(record.get("QTL Id") or f"{chrom}:{start}-{end}").strip()
        trait = str(record.get("Trait Description") or "").strip()
        kind = str(record.get("Type") or record.get("QTL/GWAS/Major effect gene") or "")
        if _MAJOR.search(kind) or _MAJOR.search(qtl_id):
            genes = [gene for raw in re.split(r"[\s,;]+", str(record.get("Genes Under QTL (v3.0)") or "")) if raw and (gene := resolver.get(raw))]
            ctx.count(source.id, "major_genes" if genes else "major_genes_without_gene")
            for gene in dict.fromkeys(genes):
                gene_rows.append(
                    {
                        **base,
                        "gene_id": gene,
                        "source_gene_id": qtl_id,
                        "source_assembly": assembly,
                        "mapping": "identical",
                        "symbols": [qtl_id],
                        "symbol_long": None,
                        "synopsis": f"Sorghum QTL Atlas major effect gene: {trait}" if trait else None,
                        "trait_terms": [],
                        "trait_names": [trait] if trait else [],
                        "confidence": None,
                        "pmids": [],
                        "dois": [],
                        "weight": 1.0,
                        "source_db": "Sorghum QTL Atlas",
                    }
                )
            continue
        qtl_rows.append(
            {
                **base,
                "qtl_id": f"SQA|{qtl_id}",
                "study_id": str(record.get("Publication") or "Sorghum QTL Atlas").strip(),
                "qtl_name": qtl_id,
                "trait_name": trait,
                "trait_terms": [],
                "genetic_map": str(record.get("Population") or "").strip() or None,
                "linkage_group": None,
                "cm_start": None,
                "cm_end": None,
                "cm_peak": None,
                "chrom": chrom,
                "start": start,
                "end": end,
                "span_bp": end - start + 1,
                "n_markers": 0,
                "n_markers_placed": 0,
                "placement": "reported_low_precision",
                "publication_doi": None,
                "source_db": "Sorghum QTL Atlas",
            }
        )
    _check_bounds(ctx, source, assembly, total, outside)
    ctx.count(source.id, "qtl", ctx.insert("qtl", qtl_rows))
    ctx.count(source.id, "known_genes", ctx.insert("known_genes", gene_rows))


def _atlas_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("rb") as handle:
        magic = handle.read(16)
    if magic.startswith(b"SQLite format 3"):
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield from (dict(row) for row in connection.execute("SELECT * FROM atlas"))
        finally:
            connection.close()
        return
    with open_text(path) as handle:
        sample = handle.read(4096)
        handle.seek(0)
        dialect = csv.excel_tab if sample.count("\t") >= sample.count(",") else csv.excel
        yield from csv.DictReader(handle, dialect=dialect)


def _check_bounds(ctx: BuildContext, source: Source, assembly: str, total: int, outside: int) -> None:
    ctx.count(source.id, f"checked_on:{assembly}", total)
    ctx.count(source.id, "outside_chromosome_ends", outside)
    limit = float(source.params.get("max_out_of_bounds", 0.02))
    if total and outside / total > limit:
        raise BuildError(
            f"{source.id}: {outside} of {total} intervals fall past the {assembly} chromosome ends; "
            "the table is on another assembly, remap it before loading"
        )


def _trait_rows(ctx: BuildContext, source: Source, traits: set[tuple[str, str]], label: str) -> Iterator[dict[str, Any]]:
    for trait, term in sorted(traits):
        yield {**ctx.base(source, "none"), "trait_name": trait, "term_id": term, "study_id": None, "source_db": label}
