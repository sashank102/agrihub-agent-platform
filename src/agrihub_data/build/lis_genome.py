"""LIS genome layer: gene models, functional annotation, id maps and markers."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agrihub_data.build.context import BuildContext, BuildError, split_list, tsv_rows
from agrihub_data.build.gff import features, load_marker_gff, strip_id_prefix
from agrihub_data.registry import Source, UnknownAssemblyError

GENE_PARTS = ("CDS", "five_prime_UTR", "three_prime_UTR")

_A1_ANCESTOR = re.compile(r"^(Glyma\d{2}g\d{5})\.v1(?:\.\d+)?$")
_WM82_ANCESTOR = re.compile(r"^(Glyma\.\d{2}G\d{6})\.(Wm82\.a\d\.v1)$")
_TRANSCRIPT = re.compile(r"^(.+?)\.\d+$")
_INTERPRO = re.compile(r"(IPR\d{6})(?: \(([^)]*)\))?")
_LIS_MARKER_FILE = re.compile(r"^glyma\.(Wm82\.gnm(\d+)\.mrk\.[^.]+)\.gff3\.gz$")
_A1 = "Wm82.a1.v1"
_INFO_COLUMNS = {
    "pfam": ("Pfam",),
    "panther": ("Panther",),
    "kog": ("KOG",),
    "ec": ("KEGG/ec", "ec"),
    "ko": ("KO",),
}


def lis_assembly(ctx: BuildContext, number: str, genotype: str = "Wm82") -> str:
    """Return the assembly whose LIS namespace is ``glyma.<genotype>.gnm<number>.``."""
    namespace = f"{ctx.registry.abbrev}.{genotype}.gnm{number}."
    for assembly in ctx.registry.assemblies:
        if assembly.chrom_namespace == namespace:
            return assembly.id
    raise UnknownAssemblyError(f"no registered assembly uses the LIS namespace {namespace}")


def build_lis_annotation(ctx: BuildContext, source: Source) -> None:
    """Load genes, annotation, GO, synonyms and ancestors of one LIS annotation."""
    if len(source.assemblies) != 1:
        raise BuildError(f"{source.id} must name exactly one assembly")
    assembly = source.assemblies[0]
    parser = source.id
    base = ctx.base(source, assembly)
    genes = _genes(ctx, source, assembly)
    ctx.insert("genes", ({**base, **gene} for gene in genes.values()))
    ctx.count(parser, "genes", len(genes))
    ctx.count(parser, "gene_parts", ctx.insert("gene_parts", _gene_parts(ctx, source, assembly, genes)))

    ancestors = [
        {
            **ctx.base(source, assembly),
            "from_id": gene_id,
            "to_id": target,
            "to_assembly": target_assembly,
            "relation": "ancestor",
            "source_db": "LIS",
        }
        for gene_id, gene in genes.items()
        if gene["ancestor_id"]
        for target, target_assembly in [_parse_ancestor(ctx, str(gene["ancestor_id"]))]
        if target is not None
    ]
    ctx.count(parser, "ancestor_links", ctx.insert("id_map", ancestors))

    info = ctx.optional_file(source, ".info_annot.txt.gz") or ctx.optional_file(
        source, ".annotation_info.txt.gz"
    )
    if info is not None:
        _load_info_annot(ctx, source, assembly, info, set(genes))
    gene_annot = ctx.optional_file(source, ".info_gene_annot.txt.gz")
    if gene_annot is not None:
        _load_interpro(ctx, source, assembly, gene_annot, set(genes))
    for suffix in (".synonym.txt.gz", ".info_synonyms.txt.gz"):
        synonyms = ctx.optional_file(source, suffix)
        if synonyms is not None:
            _load_synonyms(ctx, source, assembly, synonyms, set(genes))
    markers = ctx.optional_file(source, ".markers.gff3.gz")
    if markers is not None:
        annotation = ctx.registry.assembly(assembly).annotation or source.version
        load_marker_gff(
            ctx,
            markers,
            assembly=assembly,
            marker_set=f"{annotation}.markers",
            source_version=ctx.source_version(source),
            parser=parser,
        )


def build_lis_pangenes(ctx: BuildContext, source: Source) -> None:
    """Load pangene membership for every assembly column the source names."""
    path = ctx.file(source, ".table_ref_lines.tsv.gz")
    with_header = tsv_rows(path, skip_comments=False)
    header = next(with_header)
    columns: dict[int, str] = {}
    for index, name in enumerate(header):
        match = re.fullmatch(rf"{ctx.registry.abbrev}\.([A-Za-z0-9_]+)\.gnm(\d+)\.ann\d+", name.strip())
        if match is None:
            continue
        try:
            assembly = lis_assembly(ctx, match.group(2), match.group(1))
        except UnknownAssemblyError:
            assembly = None
        if assembly is None or assembly not in source.assemblies:
            ctx.count(source.id, f"skipped_column:{name.strip()}")
            continue
        columns[index] = assembly
    if ctx.registry.canonical_assembly not in columns.values():
        raise BuildError(f"{path.name} has no column for {ctx.registry.canonical_assembly}")

    def rows() -> Iterator[dict[str, Any]]:
        for fields in with_header:
            pangene = fields[0].strip()
            for index, assembly in columns.items():
                cell = fields[index] if index < len(fields) else ""
                for gene_id in split_list(cell):
                    if gene_id == "NONE":
                        continue
                    yield {
                        **ctx.base(source, assembly),
                        "from_id": gene_id,
                        "to_id": pangene,
                        "to_assembly": "none",
                        "relation": "pangene_member",
                        "source_db": "LIS pangenes",
                    }

    ctx.count(source.id, "pangene_members", ctx.insert("id_map", rows()))


def build_lis_markers(ctx: BuildContext, source: Source) -> None:
    """Load every LIS marker-set GFF; the file name gives set and assembly."""
    for path in ctx.files(source):
        match = _LIS_MARKER_FILE.match(path.name)
        if match is None:
            raise BuildError(f"{source.id}: unexpected marker file {path.name}")
        load_marker_gff(
            ctx,
            path,
            assembly=lis_assembly(ctx, match.group(2)),
            marker_set=match.group(1),
            source_version=match.group(1),
            parser=source.id,
        )


def _genes(ctx: BuildContext, source: Source, assembly: str) -> dict[str, dict[str, Any]]:
    gff = ctx.file(source, ".gene_models_main.gff3.gz")
    genes: dict[str, dict[str, Any]] = {}
    for feature in features(gff, {"gene"}):
        gene_id = feature.attributes.get("Name")
        chrom = ctx.chrom(assembly, feature.seqid)
        if not gene_id or chrom is None:
            ctx.count(source.id, "genes_skipped_unknown_seqid")
            continue
        if gene_id in genes:
            ctx.count(source.id, "duplicate_gene_ids")
            continue
        if not ctx.in_bounds(assembly, chrom, feature.start, feature.end):
            ctx.count(source.id, "genes_out_of_bounds")
            continue
        genes[gene_id] = {
            "gene_id": gene_id,
            "chrom": chrom,
            "start": feature.start,
            "end": feature.end,
            "strand": feature.strand,
            "defline": feature.attributes.get("Note"),
            "ancestor_id": feature.attributes.get("ancestorIdentifier"),
            "source_db": "LIS",
        }
    defline = ctx.optional_file(source, ".defline.txt.gz")
    if defline is not None:
        for fields in tsv_rows(defline):
            gene_id = _gene_of_transcript(fields[0])
            gene = genes.get(gene_id)
            if gene is not None and not gene["defline"] and len(fields) >= 3:
                gene["defline"] = fields[2].split(" - ", 1)[-1].strip()
    return genes


def _gene_parts(
    ctx: BuildContext,
    source: Source,
    assembly: str,
    genes: dict[str, dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    """Yield the CDS and UTR intervals of every transcript of a loaded gene."""
    gff = ctx.file(source, ".gene_models_main.gff3.gz")
    transcripts: dict[str, tuple[str, str]] = {}
    base = ctx.base(source, assembly)
    for feature in features(gff, {"mRNA", *GENE_PARTS}):
        if feature.type == "mRNA":
            gene_id = strip_id_prefix(feature.attributes.get("Parent", ""))
            if gene_id in genes:
                transcripts[feature.attributes.get("ID", "")] = (gene_id, strip_id_prefix(feature.attributes.get("ID", "")))
            continue
        owner = transcripts.get(feature.attributes.get("Parent", "").split(",")[0])
        if owner is None:
            ctx.count(source.id, "gene_parts_without_transcript")
            continue
        gene = genes[owner[0]]
        yield {
            **base,
            "gene_id": owner[0],
            "transcript_id": owner[1],
            "part": feature.type,
            "chrom": gene["chrom"],
            "start": feature.start,
            "end": feature.end,
            "strand": gene["strand"],
            "source_db": "LIS",
        }


def _parse_ancestor(ctx: BuildContext, value: str) -> tuple[str | None, str]:
    match = _A1_ANCESTOR.match(value)
    if match:
        return match.group(1), _A1
    match = _WM82_ANCESTOR.match(value)
    if match:
        try:
            return match.group(1), ctx.registry.assembly(match.group(2)).id
        except UnknownAssemblyError:
            return None, ""
    return None, ""


def _load_info_annot(
    ctx: BuildContext,
    source: Source,
    assembly: str,
    path: Path,
    known: set[str],
) -> None:
    rows = tsv_rows(path, skip_comments=False)
    header = [name.lstrip("#").strip() for name in next(rows)]
    index = {name: position for position, name in enumerate(header)}
    annotations: dict[tuple[str, str, str], str | None] = {}
    go_terms: set[tuple[str, str]] = set()
    best_hits: set[tuple[str, str]] = set()

    def cell(fields: list[str], *names: str) -> str:
        for name in names:
            position = index.get(name)
            if position is not None and position < len(fields):
                return fields[position].strip()
        return ""

    for fields in rows:
        gene_id = cell(fields, "locusName")
        if gene_id not in known:
            continue
        for kind, names in _INFO_COLUMNS.items():
            for value in split_list(cell(fields, *names), ", "):
                annotations.setdefault((gene_id, kind, value.removeprefix("EC:")), None)
        for go_id in split_list(cell(fields, "GO"), ", "):
            if go_id.startswith("GO:"):
                go_terms.add((gene_id, go_id))
        arabidopsis = _gene_of_transcript(cell(fields, "Best-hit-arabi-name"))
        if arabidopsis:
            symbol = cell(fields, "arabi-symbol")
            defline = cell(fields, "arabi-defline", "Best-hit-arabi-defline")
            label = f"{symbol}: {defline}" if symbol and defline else symbol or defline or None
            annotations[(gene_id, "arabidopsis_best_hit", arabidopsis)] = label
            best_hits.add((gene_id, arabidopsis))
        rice = _gene_of_transcript(cell(fields, "Best-hit-rice-name"))
        if rice:
            annotations[(gene_id, "rice_best_hit", rice)] = cell(fields, "Best-hit-rice-defline") or None

    base = ctx.base(source, assembly)
    ctx.count(
        source.id,
        "annotation",
        ctx.insert(
            "annotation",
            (
                {**base, "gene_id": gene_id, "kind": kind, "value": value, "label": label, "source_db": "LIS"}
                for (gene_id, kind, value), label in sorted(annotations.items())
            ),
        ),
    )
    ctx.count(
        source.id,
        "go_annot",
        ctx.insert(
            "go_annot",
            (
                {
                    **base,
                    "gene_id": gene_id,
                    "go_id": go_id,
                    "evidence_code": "IEA",
                    "qualifier": None,
                    "reference": source.version,
                    "source_db": "LIS",
                }
                for gene_id, go_id in sorted(go_terms)
            ),
        ),
    )
    ctx.count(
        source.id,
        "blast_best_hits",
        ctx.insert(
            "orthologs",
            (
                {
                    **base,
                    "gene_id": gene_id,
                    "source_gene_id": gene_id,
                    "target_species": "arabidopsis",
                    "target_assembly": "TAIR10",
                    "target_gene_id": target,
                    "method": "blast_best_hit",
                    "relation": None,
                    "identity": None,
                    "high_confidence": None,
                    "source_db": "LIS",
                }
                for gene_id, target in sorted(best_hits)
            ),
        ),
    )


def _load_interpro(
    ctx: BuildContext,
    source: Source,
    assembly: str,
    path: Path,
    known: set[str],
) -> None:
    found: dict[tuple[str, str], str | None] = {}
    for fields in tsv_rows(path):
        if len(fields) < 2:
            continue
        gene_id = fields[0].strip().removeprefix(f"{ctx.registry.abbrev}.")
        if gene_id not in known:
            continue
        for interpro, name in _INTERPRO.findall(fields[1]):
            if name or (gene_id, interpro) not in found:
                found[(gene_id, interpro)] = name or None
    base = ctx.base(source, assembly)
    ctx.count(
        source.id,
        "interpro",
        ctx.insert(
            "annotation",
            (
                {**base, "gene_id": gene_id, "kind": "interpro", "value": interpro, "label": name, "source_db": "LIS"}
                for (gene_id, interpro), name in sorted(found.items())
            ),
        ),
    )


def _load_synonyms(
    ctx: BuildContext,
    source: Source,
    assembly: str,
    path: Path,
    known: set[str],
) -> None:
    pairs = sorted(
        {
            (fields[1].strip(), _gene_of_transcript(fields[0]))
            for fields in tsv_rows(path)
            if len(fields) >= 2 and _gene_of_transcript(fields[0]) in known
        }
    )
    ctx.count(
        source.id,
        "synonyms",
        ctx.insert(
            "id_map",
            (
                {
                    **ctx.base(source, _A1),
                    "from_id": old_id,
                    "to_id": gene_id,
                    "to_assembly": assembly,
                    "relation": "synonym",
                    "source_db": "LIS",
                }
                for old_id, gene_id in pairs
            ),
        ),
    )


def _gene_of_transcript(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    match = _TRANSCRIPT.match(value)
    return match.group(1) if match else value
