"""Identifier and coordinate translation: markers, gene ids, chromosomes, liftover."""

import bisect
import re
from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field

from agrihub_data.bundle import Bundle
from agrihub_data.query.common import Region, registry_of, resolve_region
from agrihub_data.registry import (
    SpeciesRegistry,
    UnknownChromosomeError,
    load_species,
    normalize_chrom,
)

__all__ = [
    "GeneIdMapping",
    "LiftedRegion",
    "MarkerHit",
    "liftover",
    "map_gene_ids",
    "normalize_chrom",
    "parse_positional",
    "resolve_marker",
]

_POSITIONAL = (
    re.compile(r"^S(?P<chrom>\d{1,2})_(?P<pos>\d+)$"),
    re.compile(r"^(?P<chrom>(?:[A-Za-z]+[._]?)?\d{1,2})\s*[:_\s]\s*(?P<pos>\d+)$"),
)
_BARC_SNP = re.compile(r"^BARC_\d\.\d{2}_(?P<chrom>Gm\d{2})_(?P<pos>\d+)_[ACGTN]_[ACGTN]$")
_LIS_GENE = re.compile(r"^[a-z]+\.Wm82\.gnm(?P<gnm>\d+)\.ann\d+\.(?P<gene>\S+)$")
_ENSEMBL_GENE = re.compile(r"^GLYMA_(?P<rest>\d{2}G\d{6})$")
_GLYMA_TRANSCRIPT = re.compile(r"^(?P<gene>Glyma\.\d{2}G\d{6})\.\d+(?:\.p)?$")
_GLYMA1 = re.compile(r"^Glyma(?P<chrom>\d{2})g(?P<num>\d{5})(?:\.\d+)?$", re.IGNORECASE)

MarkerFlag = Literal[
    "positional_id",
    "assembly_assumed",
    "a1_embedded_position",
    "other_assembly",
    "ambiguous",
]


class MarkerHit(BaseModel):
    """One placement of a marker on one assembly."""

    query: str
    marker_id: str
    alias: str | None = None
    assembly: str
    chrom: str
    pos: int
    end: int
    marker_set: str | None = None
    source_version: str | None = None
    flags: list[MarkerFlag] = Field(default_factory=list)
    note: str | None = None


class GeneIdMapping(BaseModel):
    """How one input id maps to a gene on the target assembly."""

    query: str
    from_id: str
    from_assembly: str | None
    to_id: str | None
    to_assembly: str
    relation: str
    """identical, ancestor, pangene, synonym, same_id or unmapped."""
    exists: bool


class LiftedRegion(BaseModel):
    """A region moved between assemblies through shared gene or marker anchors."""

    source: Region
    to_assembly: str
    chrom: str | None
    start: int | None
    end: int | None
    method: Literal["gene_anchor", "marker_anchor", "none"]
    n_anchors: int
    genes: list[str] = Field(default_factory=list)
    """Target-assembly genes mapped by id from genes inside the source region."""
    note: str | None = None


def parse_positional(
    species: str,
    text: str,
    assembly: str | None = None,
) -> tuple[str, int] | None:
    """Parse ``S18_9263941``, ``Chr18:9263941``, ``18 9263941`` and similar ids."""
    value = text.strip()
    for pattern in _POSITIONAL:
        match = pattern.match(value)
        if match is None:
            continue
        try:
            chrom = normalize_chrom(species, match.group("chrom"), assembly)
        except UnknownChromosomeError:
            return None
        return chrom, int(match.group("pos"))
    return None


def resolve_marker(
    species: str,
    marker_id: str,
    assembly_hint: str | None = None,
    bundle: Bundle | None = None,
) -> list[MarkerHit]:
    """Place a SNP or marker id on registered assemblies.

    Positional ids are placed on ``assembly_hint`` (the canonical assembly
    when unset, flagged ``assembly_assumed``). Named markers are looked up in
    the bundle's marker sets on every assembly, hint first. BARC SNP names
    embed a Wm82.a1 position, which is returned flagged
    ``a1_embedded_position`` and must be lifted before any a2 comparison.
    """
    registry = load_species(species)
    hint = registry.assembly(assembly_hint).id
    query = marker_id.strip()
    positional = parse_positional(species, query, hint)
    barc = _BARC_SNP.match(query)
    if positional is not None and barc is None:
        chrom, pos = positional
        flags: list[MarkerFlag] = ["positional_id"]
        if assembly_hint is None:
            flags.append("assembly_assumed")
        if pos > _length(registry, hint, chrom):
            return []
        return [MarkerHit(query=query, marker_id=query, assembly=hint, chrom=chrom, pos=pos, end=pos, flags=flags)]

    hits: list[MarkerHit] = []
    if bundle is not None:
        rows = bundle.rows(
            'SELECT marker_id, alias, assembly, chrom, start, "end", marker_set, source_version '
            "FROM markers WHERE lower(marker_id) = lower(?) OR lower(alias) = lower(?) "
            'ORDER BY assembly, chrom, start, marker_set',
            [query, query],
        )
        seen: set[tuple[str, str, int]] = set()
        for row in rows:
            key = (str(row["assembly"]), str(row["chrom"]), int(row["start"]))
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                MarkerHit(
                    query=query,
                    marker_id=str(row["marker_id"]),
                    alias=row["alias"],
                    assembly=key[0],
                    chrom=key[1],
                    pos=key[2],
                    end=int(row["end"]),
                    marker_set=row["marker_set"],
                    source_version=row["source_version"],
                )
            )
    a1 = next((assembly.id for assembly in registry.assemblies if assembly.embedded_in_marker_names), None)
    if barc is not None and a1 is not None:
        embedded = (normalize_chrom(species, barc.group("chrom"), a1), int(barc.group("pos")))
        if not any(hit.assembly == a1 and (hit.chrom, hit.pos) == embedded for hit in hits):
            hits.append(
                MarkerHit(
                    query=query,
                    marker_id=query,
                    assembly=a1,
                    chrom=embedded[0],
                    pos=embedded[1],
                    end=embedded[1],
                    note="position parsed from the BARC SNP name",
                )
            )
        for hit in hits:
            if hit.assembly == a1:
                hit.flags.append("a1_embedded_position")
                hit.note = hit.note or f"BARC names embed {a1} coordinates; lift before comparing with {hint}"
    by_assembly = Counter(hit.assembly for hit in hits)
    chromosomes: dict[str, set[str]] = {}
    for hit in hits:
        chromosomes.setdefault(hit.assembly, set()).add(hit.chrom)
        if hit.assembly != hint:
            hit.flags.append("other_assembly")
    for hit in hits:
        if by_assembly[hit.assembly] > 1 and len(chromosomes[hit.assembly]) > 1:
            hit.flags.append("ambiguous")
    hits.sort(key=lambda hit: (hit.assembly != hint, hit.assembly, hit.chrom, hit.pos))
    return hits


def map_gene_ids(
    bundle: Bundle,
    ids: list[str],
    to_assembly: str | None = None,
    *,
    from_assembly: str | None = None,
) -> list[GeneIdMapping]:
    """Map gene ids across namespaces and assemblies.

    Accepts ``Glyma.18G092200`` (on ``from_assembly``, canonical by default),
    transcripts (``Glyma.18G092200.1``), assembly-qualified LIS ids
    (``glyma.Wm82.gnm4.ann1.Glyma.18G092200``), Ensembl ``GLYMA_18G092200``
    (Wm82.a2) and Wm82.a1 ids (``Glyma18g15421``, via synonyms). Cross-assembly
    mapping uses GFF ancestor links first and LIS pangenes second.
    """
    registry = registry_of(bundle)
    target = registry.assembly(to_assembly).id
    default_from = registry.assembly(from_assembly).id
    mappings: list[GeneIdMapping] = []
    for query in dict.fromkeys(item.strip() for item in ids if item.strip()):
        gene, assembly = _parse_gene_id(registry, query, default_from)
        mappings.extend(_map_one(bundle, query, gene, assembly, target, registry))
    return mappings


def liftover(
    bundle: Bundle,
    regions: list[Region],
    from_assembly: str,
    to_assembly: str,
    *,
    pad_bp: int = 200_000,
) -> list[LiftedRegion]:
    """Move regions between assemblies by id-based anchors.

    Genes inside and around the region are mapped by id (ancestor links, then
    pangenes) and their positions interpolate the region ends. Shared marker
    ids are the fallback when fewer than two gene anchors land on one
    chromosome.
    """
    registry = registry_of(bundle)
    source_assembly = registry.assembly(from_assembly).id
    target = registry.assembly(to_assembly).id
    lifted: list[LiftedRegion] = []
    for raw in regions:
        region = resolve_region(registry, raw, source_assembly)
        if target == source_assembly:
            lifted.append(
                LiftedRegion(
                    source=region, to_assembly=target, chrom=region.chrom, start=region.start,
                    end=region.end, method="none", n_anchors=0, note="same assembly",
                )
            )
            continue
        gene_anchors = _gene_anchors(bundle, region, source_assembly, target, pad_bp)
        inside = sorted(
            {
                anchor[3]
                for anchor in gene_anchors
                if anchor[0] <= region.end and anchor[1] >= region.start
            }
        )
        method: Literal["gene_anchor", "marker_anchor", "none"] = "gene_anchor"
        anchors = [(anchor[0], anchor[1], anchor[2], anchor[4], anchor[5]) for anchor in gene_anchors]
        chrom, points = _dominant(anchors)
        if len(points) < 2:
            method = "marker_anchor"
            chrom, points = _dominant(_marker_anchors(bundle, region, source_assembly, target, pad_bp))
        if chrom is None or len(points) < 2:
            lifted.append(
                LiftedRegion(
                    source=region, to_assembly=target, chrom=None, start=None, end=None,
                    method="none", n_anchors=len(points), genes=inside,
                    note="fewer than two anchors on one chromosome",
                )
            )
            continue
        start = _interpolate(points, region.start)
        end = _interpolate(points, region.end)
        lifted.append(
            LiftedRegion(
                source=region,
                to_assembly=target,
                chrom=chrom,
                start=max(1, min(start, end)),
                end=min(max(start, end), _length(registry, target, chrom)),
                method=method,
                n_anchors=len(points),
                genes=inside,
            )
        )
    return lifted


def _parse_gene_id(registry: SpeciesRegistry, query: str, default: str) -> tuple[str, str | None]:
    lis = _LIS_GENE.match(query)
    if lis:
        namespace = f"{registry.abbrev}.Wm82.gnm{lis.group('gnm')}."
        assembly = next((a.id for a in registry.assemblies if a.chrom_namespace == namespace), None)
        return _strip_transcript(lis.group("gene")), assembly
    ensembl = _ENSEMBL_GENE.match(query)
    if ensembl:
        return f"Glyma.{ensembl.group('rest')}", _namespace_assembly(registry, "ensembl") or default
    old = _GLYMA1.match(query)
    if old:
        return f"Glyma{old.group('chrom')}g{old.group('num')}", _namespace_assembly(registry, "glyma1")
    for namespace in registry.id_namespaces:
        if namespace.kind not in {"gene", "transcript"} or namespace.canonical is None:
            continue
        found = namespace.match(query, ignore_case=False)
        if found is not None:
            return found.expand(namespace.canonical), _namespace_assembly(registry, namespace.id) or default
    for namespace in registry.id_namespaces:
        if namespace.kind == "gene" and namespace.assembly and namespace.match(query, ignore_case=False):
            return query, registry.assembly(namespace.assembly).id
    return _strip_transcript(query), default


def _namespace_assembly(registry: SpeciesRegistry, namespace: str) -> str | None:
    for item in registry.id_namespaces:
        if item.id == namespace and item.assembly:
            return registry.assembly(item.assembly).id
    return None


def _strip_transcript(value: str) -> str:
    match = _GLYMA_TRANSCRIPT.match(value)
    return match.group("gene") if match else value


def _map_one(
    bundle: Bundle,
    query: str,
    gene: str,
    assembly: str | None,
    target: str,
    registry: SpeciesRegistry,
) -> list[GeneIdMapping]:
    def mapping(to_id: str | None, relation: str) -> GeneIdMapping:
        return GeneIdMapping(
            query=query,
            from_id=gene,
            from_assembly=assembly,
            to_id=to_id,
            to_assembly=target,
            relation=relation,
            exists=to_id is not None and _exists(bundle, to_id, target),
        )

    if assembly is None:
        return [mapping(None, "unmapped")]
    if assembly == target and _exists(bundle, gene, target):
        return [mapping(gene, "identical")]
    synonyms = bundle.rows_raw(
        "SELECT DISTINCT to_id FROM id_map WHERE relation = 'synonym' AND assembly = ? "
        "AND from_id = ? AND to_assembly = ? ORDER BY 1",
        [assembly, gene, target],
    )
    if synonyms:
        return [mapping(str(row[0]), "synonym") for row in synonyms]
    if assembly == target:
        return [mapping(gene, "identical")]
    chain = _ancestor_chain(bundle, gene, assembly, target, registry)
    if chain:
        return [mapping(to_id, "ancestor") for to_id in chain]
    pangene = _pangene(bundle, gene, assembly, target)
    if pangene:
        return [mapping(to_id, "pangene") for to_id in pangene]
    if _exists(bundle, gene, target):
        return [mapping(gene, "same_id")]
    return [mapping(None, "unmapped")]


def _ancestor_chain(
    bundle: Bundle,
    gene: str,
    assembly: str,
    target: str,
    registry: SpeciesRegistry,
) -> list[str]:
    for forward in (True, False):
        current, current_assembly = {gene}, assembly
        for _ in range(len(registry.assemblies)):
            placeholders = ", ".join("?" for _ in current)
            if forward:
                rows = bundle.rows_raw(
                    f"SELECT DISTINCT to_id, to_assembly FROM id_map WHERE relation = 'ancestor' "
                    f"AND assembly = ? AND from_id IN ({placeholders})",
                    [current_assembly, *sorted(current)],
                )
            else:
                rows = bundle.rows_raw(
                    f"SELECT DISTINCT from_id, assembly FROM id_map WHERE relation = 'ancestor' "
                    f"AND to_assembly = ? AND to_id IN ({placeholders})",
                    [current_assembly, *sorted(current)],
                )
            if not rows:
                break
            current_assembly = str(rows[0][1])
            current = {str(row[0]) for row in rows if row[1] == current_assembly}
            if current_assembly == target:
                return sorted(current)
    return []


def _pangene(bundle: Bundle, gene: str, assembly: str, target: str) -> list[str]:
    rows = bundle.rows_raw(
        "SELECT DISTINCT b.from_id FROM id_map AS a JOIN id_map AS b ON a.to_id = b.to_id "
        "WHERE a.relation = 'pangene_member' AND b.relation = 'pangene_member' "
        "AND a.assembly = ? AND a.from_id = ? AND b.assembly = ? ORDER BY 1",
        [assembly, gene, target],
    )
    return [str(row[0]) for row in rows]


def _exists(bundle: Bundle, gene: str, assembly: str) -> bool:
    return bool(bundle.rows_raw("SELECT 1 FROM genes WHERE assembly = ? AND gene_id = ?", [assembly, gene]))


def _gene_anchors(
    bundle: Bundle,
    region: Region,
    source: str,
    target: str,
    pad_bp: int,
) -> list[tuple[int, int, str, str, int, int]]:
    """Return ``(src_start, src_end, target_chrom, gene, target_start, target_end)`` anchors."""
    rows = bundle.rows_raw(
        'SELECT gene_id, start, "end" FROM genes WHERE assembly = ? AND chrom = ? '
        'AND "end" >= ? AND start <= ? ORDER BY start',
        [source, region.chrom, max(1, region.start - pad_bp), region.end + pad_bp],
    )
    anchors: list[tuple[int, int, str, str, int, int]] = []
    registry = load_species(bundle.species)
    for gene_id, start, end in rows:
        targets = _ancestor_chain(bundle, str(gene_id), source, target, registry) or _pangene(
            bundle, str(gene_id), source, target
        )
        if len(targets) != 1:
            continue
        placed = bundle.rows_raw(
            'SELECT chrom, start, "end" FROM genes WHERE assembly = ? AND gene_id = ?',
            [target, targets[0]],
        )
        if placed:
            anchors.append((int(start), int(end), str(placed[0][0]), targets[0], int(placed[0][1]), int(placed[0][2])))
    return anchors


def _marker_anchors(
    bundle: Bundle,
    region: Region,
    source: str,
    target: str,
    pad_bp: int,
) -> list[tuple[int, int, str, int, int]]:
    rows = bundle.rows_raw(
        """
        SELECT DISTINCT a.start, a."end", b.chrom, b.start, b."end"
        FROM markers AS a
        JOIN markers AS b ON coalesce(a.alias, a.marker_id) = coalesce(b.alias, b.marker_id)
        WHERE a.assembly = ? AND b.assembly = ? AND a.chrom = ?
          AND a."end" >= ? AND a.start <= ?
        ORDER BY a.start
        """,
        [source, target, region.chrom, max(1, region.start - pad_bp), region.end + pad_bp],
    )
    return [(int(r[0]), int(r[1]), str(r[2]), int(r[3]), int(r[4])) for r in rows]


def _dominant(
    anchors: list[tuple[int, int, str, int, int]],
) -> tuple[str | None, list[tuple[int, int]]]:
    if not anchors:
        return None, []
    tally = Counter(anchor[2] for anchor in anchors)
    chrom = sorted(tally, key=lambda name: (-tally[name], name))[0]
    points = sorted({((a[0] + a[1]) // 2, (a[3] + a[4]) // 2) for a in anchors if a[2] == chrom})
    return chrom, points


def _interpolate(points: list[tuple[int, int]], position: int) -> int:
    sources = [point[0] for point in points]
    index = bisect.bisect_left(sources, position)
    if index <= 0:
        left, right = points[0], points[1]
    elif index >= len(points):
        left, right = points[-2], points[-1]
    else:
        left, right = points[index - 1], points[index]
    if right[0] == left[0]:
        return left[1] + (position - left[0])
    slope = (right[1] - left[1]) / (right[0] - left[0])
    if abs(slope) < 0.2 or abs(slope) > 5:
        slope = 1.0 if slope >= 0 else -1.0
    return int(round(left[1] + slope * (position - left[0])))


def _length(registry: SpeciesRegistry, assembly: str, chrom: str) -> int:
    chromosome = registry.assembly(assembly).chromosome(chrom)
    return chromosome.length if chromosome else 10**12
