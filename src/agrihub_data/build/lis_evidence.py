"""LIS evidence layer: QTL placed from their markers, GWAS hits, curated trait genes."""

import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from agrihub_data.build.context import BuildContext, BuildError, tsv_rows
from agrihub_data.build.lis_genome import lis_assembly
from agrihub_data.registry import Source, UnknownChromosomeError, normalize_chrom

Location = tuple[str, int, int]
_FULL_GENE_ID = re.compile(r"^[a-z]+\.Wm82\.gnm(\d+)\.ann\d+\.(\S+)$")


class MarkerIndex:
    """Marker names and aliases on one assembly; exact match first, then case-insensitive."""

    def __init__(self, ctx: BuildContext, assembly: str) -> None:
        """Load every marker of ``assembly``."""
        self.exact: dict[str, set[Location]] = defaultdict(set)
        self.folded: dict[str, set[Location]] = defaultdict(set)
        rows = ctx.connection.execute(
            'SELECT marker_id, alias, chrom, start, "end" FROM markers WHERE assembly = ?',
            [assembly],
        ).fetchall()
        for marker_id, alias, chrom, start, end in rows:
            location = (str(chrom), int(start), int(end))
            for name in (marker_id, alias):
                if name:
                    self.exact[str(name)].add(location)
                    self.folded[str(name).casefold()].add(location)

    def get(self, name: str) -> set[Location]:
        """Return the locations of a marker name, or an empty set."""
        return self.exact.get(name) or self.folded.get(name.casefold()) or set()


def build_lis_qtl(ctx: BuildContext, source: Source) -> None:
    """Load QTL studies and derive bp spans on the canonical assembly from markers."""
    assembly = ctx.registry.canonical_assembly
    index = MarkerIndex(ctx, assembly)
    base = ctx.base(source, assembly)
    qtl_rows: list[dict[str, Any]] = []
    trait_rows: list[dict[str, Any]] = []
    for study, folder in _entries(ctx, source):
        qtl_file = folder / f"glyma.{study}.qtl.tsv.gz"
        if not qtl_file.exists():
            ctx.count(source.id, "studies_without_qtl_file")
            continue
        ctx.count(source.id, "studies")
        readme = _readme(folder / f"README.{study}.yml")
        terms = _trait_terms(folder / f"glyma.{study}.obo.tsv.gz")
        markers: dict[str, list[str]] = defaultdict(list)
        marker_file = folder / f"glyma.{study}.qtlmrk.tsv.gz"
        if marker_file.exists():
            for fields in tsv_rows(marker_file):
                if len(fields) >= 4 and fields[3].strip():
                    markers[fields[0].strip()].append(fields[3].strip())
        seen: set[str] = set()
        for fields in tsv_rows(qtl_file):
            name = fields[0].strip()
            if not name or name in seen:
                continue
            seen.add(name)
            trait = _cell(fields, 1) or name
            group = _cell(fields, 3)
            placement = _place(ctx, index, markers.get(name, []), group, assembly)
            ctx.count(source.id, f"placement:{placement['placement']}")
            qtl_rows.append(
                {
                    **base,
                    "qtl_id": f"{study}|{name}",
                    "study_id": study,
                    "qtl_name": name,
                    "trait_name": trait,
                    "trait_terms": terms.get(trait, []),
                    "genetic_map": _cell(fields, 2),
                    "linkage_group": group,
                    "cm_start": _float(_cell(fields, 4)),
                    "cm_end": _float(_cell(fields, 5)),
                    "cm_peak": _float(_cell(fields, 6)),
                    "n_markers": len(dict.fromkeys(markers.get(name, []))),
                    "publication_doi": readme.get("publication_doi"),
                    "source_db": "LIS/SoyBase QTL",
                    **placement,
                }
            )
        trait_rows.extend(
            {
                **ctx.base(source, "none"),
                "trait_name": trait,
                "term_id": term,
                "study_id": study,
                "source_db": "LIS/SoyBase QTL",
            }
            for trait, trait_terms in sorted(terms.items())
            for term in trait_terms
        )
    ctx.count(source.id, "qtl", ctx.insert("qtl", qtl_rows))
    ctx.count(source.id, "trait_map", ctx.insert("trait_map", trait_rows))


def build_lis_gwas(ctx: BuildContext, source: Source) -> None:
    """Load LIS GWAS studies and place each hit through its marker on the canonical assembly."""
    assembly = ctx.registry.canonical_assembly
    index = MarkerIndex(ctx, assembly)
    base = ctx.base(source, assembly)
    hits: dict[str, dict[str, Any]] = {}
    trait_rows: list[dict[str, Any]] = []
    for study, folder in _entries(ctx, source):
        result = folder / f"glyma.{study}.result.tsv.gz"
        if not result.exists():
            ctx.count(source.id, "studies_without_results")
            continue
        ctx.count(source.id, "studies")
        readme = _readme(folder / f"README.{study}.yml")
        terms = _trait_terms(folder / f"glyma.{study}.obo.tsv.gz")
        for fields in tsv_rows(result):
            trait, marker = _cell(fields, 0), _cell(fields, 1)
            if not trait or not marker:
                continue
            p_value = _float(_cell(fields, 2))
            hit_id = f"{study}|{marker}|{trait}"
            previous = hits.get(hit_id)
            if previous is not None:
                if p_value is not None and (previous["p_value"] is None or p_value < previous["p_value"]):
                    previous["p_value"] = p_value
                continue
            locations = sorted(index.get(marker))
            chromosomes = {location[0] for location in locations}
            placed = locations[0] if len(chromosomes) == 1 else None
            ctx.count(source.id, "placed" if placed else "unplaced")
            hits[hit_id] = {
                **base,
                "hit_id": hit_id,
                "source_db": "LIS/SoyBase GWAS",
                "study_id": study,
                "trait_name": trait,
                "trait_terms": terms.get(trait, []),
                "marker": marker,
                "chrom": placed[0] if placed else None,
                "pos": placed[1] if placed else None,
                "p_value": p_value,
                "pmid": None,
                "doi": readme.get("publication_doi"),
                "reported_genes": [],
                "placement": "marker" if placed else "unplaced",
            }
        trait_rows.extend(
            {
                **ctx.base(source, "none"),
                "trait_name": trait,
                "term_id": term,
                "study_id": study,
                "source_db": "LIS/SoyBase GWAS",
            }
            for trait, trait_terms in sorted(terms.items())
            for term in trait_terms
        )
    ctx.count(source.id, "gwas_hits", ctx.insert("gwas_hits", hits.values()))
    ctx.count(source.id, "trait_map", ctx.insert("trait_map", trait_rows))


def build_lis_gene_functions(ctx: BuildContext, source: Source) -> None:
    """Load curated trait genes and map each gene model to the canonical assembly.

    Entries with no canonical counterpart (gene models new in a later
    assembly) stay on their source assembly with ``mapping = 'unmapped'``.
    """
    canonical = ctx.registry.canonical_assembly
    path = ctx.file(source, "glyma.traits.yml")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        documents = [document for document in yaml.safe_load_all(handle) if isinstance(document, dict)]
    for document in documents:
        ctx.count(source.id, "entries")
        full_id = str(document.get("gene_model_full_id") or "")
        match = _FULL_GENE_ID.match(full_id)
        if match is None:
            ctx.count(source.id, "unparsed_gene_model")
            continue
        source_assembly = lis_assembly(ctx, match.group(1))
        source_gene = match.group(2)
        targets = [
            (gene_id, mapping, canonical)
            for gene_id, mapping in _to_canonical(ctx, source_gene, source_assembly, canonical)
        ] or [(source_gene, "unmapped", source_assembly)]
        traits = [trait for trait in document.get("traits") or [] if isinstance(trait, dict)]
        references = [ref for ref in document.get("references") or [] if isinstance(ref, dict)]
        comments = " ".join(str(comment) for comment in document.get("comments") or [])
        synopsis = " ".join(
            part for part in (str(document.get("phenotype_synopsis") or ""), comments) if part
        )
        for gene_id, mapping, assembly in targets:
            ctx.count(source.id, f"mapping:{mapping}")
            rows.append(
                {
                    **ctx.base(source, assembly),
                    "gene_id": gene_id,
                    "source_gene_id": source_gene,
                    "source_assembly": source_assembly,
                    "mapping": mapping,
                    "symbols": [str(symbol) for symbol in document.get("gene_symbols") or []],
                    "symbol_long": document.get("gene_symbol_long"),
                    "synopsis": synopsis or None,
                    "trait_terms": [str(trait["entity"]) for trait in traits if trait.get("entity")],
                    "trait_names": [str(trait["entity_name"]) for trait in traits if trait.get("entity_name")],
                    "confidence": _int(document.get("confidence")),
                    "pmids": [str(ref["pmid"]) for ref in references if ref.get("pmid")],
                    "dois": [str(ref["doi"]) for ref in references if ref.get("doi")],
                    "weight": 1.0,
                    "source_db": "LIS gene_functions",
                }
            )
    ctx.count(source.id, "known_genes", ctx.insert("known_genes", rows))


def _to_canonical(
    ctx: BuildContext,
    gene_id: str,
    assembly: str,
    canonical: str,
) -> list[tuple[str, str]]:
    """Map one gene to canonical-assembly genes: ancestor chain, then pangene, then same id."""
    if assembly == canonical:
        return [(gene_id, "identical")] if _gene_exists(ctx, gene_id, canonical) else []
    current = {gene_id}
    current_assembly = assembly
    for _ in range(len(ctx.registry.assemblies)):
        rows = ctx.connection.execute(
            "SELECT DISTINCT to_id, to_assembly FROM id_map WHERE relation = 'ancestor' "
            f"AND assembly = ? AND from_id IN ({', '.join('?' for _ in current)})",
            [current_assembly, *sorted(current)],
        ).fetchall()
        if not rows:
            break
        next_assembly = str(rows[0][1])
        current = {str(row[0]) for row in rows if row[1] == next_assembly}
        current_assembly = next_assembly
        if current_assembly == canonical:
            return [(target, "ancestor") for target in sorted(current) if _gene_exists(ctx, target, canonical)]
    pangene = ctx.connection.execute(
        "SELECT DISTINCT b.from_id FROM id_map AS a JOIN id_map AS b ON a.to_id = b.to_id "
        "WHERE a.relation = 'pangene_member' AND b.relation = 'pangene_member' "
        "AND a.assembly = ? AND a.from_id = ? AND b.assembly = ? ORDER BY 1",
        [assembly, gene_id, canonical],
    ).fetchall()
    if pangene:
        return [(str(row[0]), "pangene") for row in pangene]
    if _gene_exists(ctx, gene_id, canonical):
        return [(gene_id, "same_id")]
    return []


def _gene_exists(ctx: BuildContext, gene_id: str, assembly: str) -> bool:
    return (
        ctx.connection.execute(
            "SELECT 1 FROM genes WHERE assembly = ? AND gene_id = ?",
            [assembly, gene_id],
        ).fetchone()
        is not None
    )


def _place(
    ctx: BuildContext,
    index: MarkerIndex,
    markers: list[str],
    group: str,
    assembly: str,
) -> dict[str, Any]:
    expected = _group_chromosome(ctx, group, assembly)
    located = {marker: index.get(marker) for marker in dict.fromkeys(markers)}
    candidates = [(marker, location) for marker, locations in located.items() for location in locations]
    unplaced = {"chrom": None, "start": None, "end": None, "span_bp": None, "n_markers_placed": 0}
    if not candidates:
        return {**unplaced, "placement": "unplaced"}
    if expected is None:
        tally = Counter(location[0] for _, location in candidates)
        expected = sorted(tally, key=lambda chrom: (-tally[chrom], chrom))[0]
    chosen = [(marker, location) for marker, location in candidates if location[0] == expected]
    if not chosen:
        return {**unplaced, "placement": "lg_conflict"}
    start = min(location[1] for _, location in chosen)
    end = max(location[2] for _, location in chosen)
    placed = len({marker for marker, _ in chosen})
    return {
        "chrom": expected,
        "start": start,
        "end": end,
        "span_bp": end - start + 1,
        "n_markers_placed": placed,
        "placement": "markers" if placed >= 2 else "single_marker",
    }


def _group_chromosome(ctx: BuildContext, group: str, assembly: str) -> str | None:
    if not group:
        return None
    by_group = ctx.registry.chromosome_for_linkage_group(group)
    if by_group is not None:
        return by_group
    try:
        return normalize_chrom(ctx.species, group, assembly)
    except UnknownChromosomeError:
        return None


def _entries(ctx: BuildContext, source: Source) -> Iterator[tuple[str, Path]]:
    listing = ctx.manifest.collections.get(source.id)
    if not listing:
        raise BuildError(f"{source.id}: no collection listing in the manifest; run fetch")
    root = ctx.paths.source_dir(source.id)
    for entry in listing["entries"]:
        yield str(entry), root / str(entry)


def _trait_terms(path: Path) -> dict[str, list[str]]:
    terms: dict[str, list[str]] = defaultdict(list)
    if path.exists():
        for fields in tsv_rows(path):
            if len(fields) >= 2 and fields[1].strip():
                terms[fields[0].strip()].append(fields[1].strip())
    return {trait: list(dict.fromkeys(values)) for trait, values in terms.items()}


def _readme(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _cell(fields: list[str], index: int) -> str:
    return fields[index].strip() if index < len(fields) else ""


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
