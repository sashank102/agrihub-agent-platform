"""Gene models from plain GFF3 releases (RAP-DB, MaizeGDB, SorghumBase/Ensembl).

A ``gff_gene_models`` source loads ``genes`` and ``gene_parts`` for the one
assembly it names. ``params``:

- ``files``: ``[{file: <glob of a fetched file>, member: <tar member>}]``,
  read in order; RAP-DB keeps genes (``locus.gff``) and transcripts
  (``transcripts.gff``) in two members of one tarball. Default: the source's
  single ``*.gff3.gz``.
- ``gene_types`` (``[gene]``) and ``transcript_types`` (``[mRNA]``).
- ``transcript_gene``: the transcript attribute naming its gene
  (``Parent``; RAP-DB uses ``Locus_id``).
- ``description``: attributes tried in order for the defline
  (``[Note, description]``).
- ``source_db``: the label rows carry.

Only genes whose id matches one of the registry's gene namespaces on the
assembly are kept; the others (scaffold-only, non-coding id families) are
counted.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

from agrihub_data.build.context import (
    BuildContext,
    BuildError,
    open_tar_member,
    open_text,
)
from agrihub_data.build.gff import Feature, parse_features
from agrihub_data.registry import Source

GENE_PARTS = ("CDS", "five_prime_UTR", "three_prime_UTR")


def build_gff_gene_models(ctx: BuildContext, source: Source) -> None:
    """Load genes and their CDS/UTR parts from the source's GFF3 files."""
    if len(source.assemblies) != 1:
        raise BuildError(f"{source.id} must name exactly one assembly")
    assembly = source.assemblies[0]
    params = source.params
    gene_types = set(params.get("gene_types") or ["gene"])
    transcript_types = set(params.get("transcript_types") or ["mRNA"])
    transcript_gene = str(params.get("transcript_gene") or "Parent")
    description = [str(name) for name in params.get("description") or ["Note", "description"]]
    source_db = str(params.get("source_db") or source.name)
    namespaces = ctx.registry.gene_namespaces(assembly)
    genes: dict[str, dict[str, Any]] = {}
    transcripts: dict[str, tuple[str, str]] = {}
    parts: list[tuple[str, str, Feature]] = []
    for feature in _features(ctx, source, gene_types | transcript_types | set(GENE_PARTS)):
        if feature.type in gene_types:
            gene_id = _strip(feature.attributes.get("ID") or feature.attributes.get("Name") or "")
            if not any(namespace.match(gene_id) for namespace in namespaces):
                ctx.count(source.id, f"genes_skipped_namespace:{feature.type}")
                continue
            chrom = ctx.chrom(assembly, feature.seqid)
            if chrom is None or ctx.registry.assembly(assembly).chromosome(chrom) is None:
                ctx.count(source.id, "genes_off_chromosomes")
                continue
            if not ctx.in_bounds(assembly, chrom, feature.start, feature.end):
                ctx.count(source.id, "genes_out_of_bounds")
                continue
            if gene_id in genes:
                ctx.count(source.id, "duplicate_gene_ids")
                continue
            genes[gene_id] = {
                "gene_id": gene_id,
                "chrom": chrom,
                "start": feature.start,
                "end": feature.end,
                "strand": feature.strand,
                "defline": next((feature.attributes[name] for name in description if feature.attributes.get(name)), None),
                "ancestor_id": None,
                "source_db": source_db,
            }
        elif feature.type in transcript_types:
            transcript_id = _strip(feature.attributes.get("ID") or "")
            owner = _strip(feature.attributes.get(transcript_gene, "").split(",")[0])
            if transcript_id and owner:
                transcripts[transcript_id] = (owner, transcript_id)
                if owner in genes and not genes[owner]["defline"]:
                    genes[owner]["defline"] = next((feature.attributes[name] for name in description if feature.attributes.get(name)), None)
        else:
            parent = _strip(feature.attributes.get("Parent", "").split(",")[0])
            parts.append((parent, feature.type, feature))
    base = ctx.base(source, assembly)
    ctx.count(source.id, "genes", ctx.insert("genes", ({**base, **gene} for gene in genes.values())))

    def part_rows() -> Iterator[dict[str, Any]]:
        for parent, kind, feature in parts:
            owner = transcripts.get(parent)
            gene = genes.get(owner[0]) if owner else None
            if owner is None or gene is None:
                ctx.count(source.id, "gene_parts_without_gene")
                continue
            yield {
                **base,
                "gene_id": owner[0],
                "transcript_id": owner[1],
                "part": kind,
                "chrom": gene["chrom"],
                "start": feature.start,
                "end": feature.end,
                "strand": gene["strand"],
                "source_db": source_db,
            }

    ctx.count(source.id, "gene_parts", ctx.insert("gene_parts", part_rows()))
    ctx.count(source.id, "transcripts", len({owner for owner in transcripts.values() if owner[0] in genes}))


def _features(ctx: BuildContext, source: Source, types: set[str]) -> Iterator[Feature]:
    specs = source.params.get("files") or [{"file": "*.gff3.gz"}]
    for spec in specs:
        path = ctx.file(source, str(spec["file"]))
        with _open(path, spec.get("member")) as handle:
            yield from parse_features(handle, types)


@contextmanager
def _open(path: Path, member: object) -> Iterator[IO[str]]:
    handle = open_tar_member(path, str(member)) if member else open_text(path)
    try:
        yield handle
    finally:
        handle.close()


def _strip(identifier: str) -> str:
    """Drop Ensembl-style ``gene:`` / ``transcript:`` prefixes."""
    head, separator, rest = identifier.partition(":")
    return rest if separator and head in {"gene", "transcript", "mRNA"} else identifier
