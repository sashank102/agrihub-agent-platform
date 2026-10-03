"""Minimal GFF3 reading for gene models and marker sets."""

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from agrihub_data.build.context import BuildContext, open_text

_ID_PREFIX = re.compile(r"^[a-z]+\.[A-Za-z0-9_]+\.gnm\d+\.(?:ann\d+\.)?")


@dataclass(frozen=True)
class Feature:
    """One GFF3 line with parsed attributes."""

    seqid: str
    source: str
    type: str
    start: int
    end: int
    strand: str
    attributes: dict[str, str]


def features(path: Path, types: set[str] | None = None) -> Iterator[Feature]:
    """Yield features of the given types; attribute values are URL-decoded."""
    with open_text(path) as handle:
        yield from parse_features(handle, types)


def parse_features(lines: Iterable[str], types: set[str] | None = None) -> Iterator[Feature]:
    """Yield features of the given types from GFF3 lines."""
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.rstrip("\n\r").split("\t")
        if len(fields) < 9 or (types is not None and fields[2] not in types):
            continue
        attributes: dict[str, str] = {}
        for part in fields[8].split(";"):
            key, separator, value = part.partition("=")
            if separator and key.strip():
                attributes[key.strip()] = unquote(value.strip())
        yield Feature(
            seqid=fields[0],
            source=fields[1],
            type=fields[2],
            start=int(fields[3]),
            end=int(fields[4]),
            strand=fields[6] if fields[6] in {"+", "-"} else ".",
            attributes=attributes,
        )


def strip_id_prefix(identifier: str) -> str:
    """Drop an LIS ``glyma.Wm82.gnm2.ann1.`` prefix from a feature id."""
    return _ID_PREFIX.sub("", identifier)


def load_marker_gff(
    ctx: BuildContext,
    path: Path,
    *,
    assembly: str,
    marker_set: str,
    source_version: str,
    parser: str,
) -> int:
    """Insert every ``genetic_marker`` of one GFF into ``markers``."""
    base = {"species": ctx.species, "assembly": assembly, "source_version": source_version}

    def rows() -> Iterator[dict[str, object]]:
        for feature in features(path, {"genetic_marker", "SNP", "marker"}):
            chrom = ctx.chrom(assembly, feature.seqid)
            if chrom is None:
                ctx.count(parser, f"unplaced_seqid:{marker_set}")
                continue
            if not ctx.in_bounds(assembly, chrom, feature.start, feature.end):
                ctx.count(parser, f"out_of_bounds:{marker_set}")
                continue
            name = feature.attributes.get("Name") or strip_id_prefix(feature.attributes.get("ID", ""))
            if not name:
                continue
            alias = strip_id_prefix(feature.attributes.get("ID", "")) or None
            yield {
                **base,
                "marker_id": name,
                "alias": alias if alias != name else None,
                "marker_set": marker_set,
                "chrom": chrom,
                "start": feature.start,
                "end": feature.end,
                "alleles": feature.attributes.get("alleles"),
                "source_db": "LIS",
            }

    inserted = ctx.insert("markers", rows())
    ctx.count(parser, f"markers:{marker_set}", inserted)
    return inserted
