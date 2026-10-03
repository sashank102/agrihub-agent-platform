"""Lift positions between assemblies of one species through one-to-one pangene gene anchors.

Method (``pangene_gene_anchor``):

- A position inside an anchor gene maps by its offset from the gene start,
  reversed when the two genes are on opposite strands.
- A position between genes is interpolated, using gene midpoints, between
  each of the ``FLANK`` nearest anchors on its left and each on its right
  within ``MAX_GAP_BP``. The lifted position is the median of those
  estimates; their median absolute deviation is the ``spread``. Pairs that
  straddle a rearrangement give outlying estimates, which the median ignores.
- Near a chromosome end, where only one side has anchors, the position keeps
  its offset from the nearest anchor, in the orientation of the nearest two.

Confidence comes from the distance to the nearest anchor and the spread:
``high`` within 50 kb and 25 kb, ``medium`` within 250 kb and 100 kb, ``low``
for a spread up to 500 kb. Anything else, or a position with no anchor within
``MAX_GAP_BP``, is not converted and the caller reports why.
"""

import bisect
import statistics
import threading
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

import duckdb

from agrihub_data.bundle import Bundle

METHOD = "pangene_gene_anchor"
FLANK = 4
MAX_GAP_BP = 1_000_000
Confidence = Literal["high", "medium", "low"]
_LEVELS: tuple[tuple[Confidence, int, int], ...] = (
    ("high", 50_000, 25_000),
    ("medium", 250_000, 100_000),
    ("low", MAX_GAP_BP, 500_000),
)


@dataclass(frozen=True)
class Anchor:
    """A gene on the source assembly and its one-to-one counterpart on the target."""

    from_gene_id: str
    from_chrom: str
    from_start: int
    from_end: int
    from_strand: str
    gene_id: str
    chrom: str
    start: int
    end: int
    strand: str

    @property
    def from_mid(self) -> float:
        """Return the source gene midpoint."""
        return (self.from_start + self.from_end) / 2

    @property
    def mid(self) -> float:
        """Return the target gene midpoint."""
        return (self.start + self.end) / 2


@dataclass(frozen=True)
class Lifted:
    """The outcome of lifting one position; ``chrom`` and ``pos`` are ``None`` when it failed."""

    chrom: str | None
    pos: int | None
    confidence: Confidence | None
    method: str = METHOD
    detail: str = ""
    anchors: tuple[str, ...] = ()
    distance_bp: int | None = None
    spread_bp: int | None = None

    @property
    def ok(self) -> bool:
        """Return whether the position was converted."""
        return self.pos is not None


@dataclass
class GeneAnchorLiftover:
    """Lift positions from ``from_assembly`` to ``assembly`` with a fixed anchor set."""

    from_assembly: str
    assembly: str
    anchors: Iterable[Anchor]
    lengths: dict[str, int] = field(default_factory=dict)
    """Target chromosome lengths; a lifted position past the end is not converted."""
    _by_chrom: dict[str, list[Anchor]] = field(init=False, default_factory=dict)
    _mids: dict[str, list[float]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        """Index anchors by source chromosome, ordered by source midpoint."""
        grouped: dict[str, list[Anchor]] = defaultdict(list)
        for anchor in self.anchors:
            grouped[anchor.from_chrom].append(anchor)
        for chrom, items in grouped.items():
            items.sort(key=lambda item: item.from_mid)
            self._by_chrom[chrom] = items
            self._mids[chrom] = [item.from_mid for item in items]

    @property
    def size(self) -> int:
        """Return the number of anchors."""
        return sum(len(items) for items in self._by_chrom.values())

    def lift(self, chrom: str, pos: int, *, exclude: str | None = None) -> Lifted:
        """Lift one position; ``exclude`` leaves out one source gene (leave-one-out checks)."""
        items = [item for item in self._by_chrom.get(chrom, []) if item.from_gene_id != exclude]
        if not items:
            return self._fail(f"no {self.from_assembly} anchors on {chrom}")
        mids = [item.from_mid for item in items] if exclude else self._mids[chrom]
        index = bisect.bisect_left(mids, pos)
        left = [item for item in reversed(items[max(0, index - FLANK) : index]) if pos - item.from_end <= MAX_GAP_BP]
        right = [item for item in items[index : index + FLANK] if item.from_start - pos <= MAX_GAP_BP]
        inside = next((item for item in (*left[:1], *right[:1]) if item.from_start <= pos <= item.from_end), None)
        estimates = self._interpolate(pos, left, right) if left and right else self._extrapolate(pos, left or right)
        if not estimates and inside is None:
            return self._fail(f"no {self.from_assembly} anchor within {MAX_GAP_BP // 1_000_000} Mb of {chrom}:{pos}")
        median = statistics.median(estimates) if estimates else None
        spread = int(statistics.median(abs(value - median) for value in estimates)) if median is not None else 0
        nearest = min((*left[:1], *right[:1]), key=lambda item: _gap(item, pos))
        distance = _gap(nearest, pos)
        used = tuple(item.gene_id for item in (*left[:1], *right[:1]))
        if inside is not None:
            offset = pos - inside.from_start
            direct = inside.start + offset if inside.strand == inside.from_strand else inside.end - offset
            disagreement = abs(direct - median) if median is not None and left[1:] and right[1:] else 0
            level: Confidence = "high" if disagreement <= 50_000 else "medium" if disagreement <= 250_000 else "low"
            detail = f"inside {inside.from_gene_id} / {inside.gene_id}"
            if disagreement > 50_000:
                detail += f"; neighbouring anchors place it {disagreement // 1_000} kb away"
            return self._result(chrom=inside.chrom, pos=int(direct), level=level, detail=detail, anchors=(inside.gene_id,), distance=0, spread=int(disagreement))
        assert median is not None
        for level, max_distance, max_spread in _LEVELS:
            if distance <= max_distance and spread <= max_spread:
                sides = "between" if left and right else "beyond"
                detail = f"{sides} anchors {', '.join(used)}; {len(estimates)} estimates, spread {spread // 1_000} kb"
                return self._result(chrom=nearest.chrom, pos=int(round(median)), level=level, detail=detail, anchors=used, distance=distance, spread=spread)
        return self._fail(
            f"flanking anchors disagree by {spread // 1_000} kb around {chrom}:{pos} (rearranged or repetitive region)",
            distance=distance,
            spread=spread,
        )

    def _interpolate(self, pos: int, left: list[Anchor], right: list[Anchor]) -> list[float]:
        estimates = []
        for low in left:
            for high in right:
                span = high.from_mid - low.from_mid
                if span <= 0 or low.chrom != high.chrom:
                    continue
                estimates.append(low.mid + (pos - low.from_mid) * (high.mid - low.mid) / span)
        return estimates

    def _extrapolate(self, pos: int, side: list[Anchor]) -> list[float]:
        if not side:
            return []
        orientation = 1.0
        if len(side) >= 2 and side[0].from_mid != side[1].from_mid:
            orientation = 1.0 if (side[0].mid - side[1].mid) / (side[0].from_mid - side[1].from_mid) >= 0 else -1.0
        return [item.mid + (pos - item.from_mid) * orientation for item in side]

    def _result(
        self,
        *,
        chrom: str,
        pos: int,
        level: Confidence,
        detail: str,
        anchors: tuple[str, ...],
        distance: int,
        spread: int,
    ) -> Lifted:
        length = self.lengths.get(chrom)
        if pos < 1 or (length is not None and pos > length):
            return self._fail(f"lifted to {chrom}:{pos}, outside {chrom} on {self.assembly}", distance=distance, spread=spread)
        return Lifted(
            chrom=chrom,
            pos=pos,
            confidence=level,
            detail=detail,
            anchors=anchors,
            distance_bp=distance,
            spread_bp=spread,
        )

    def _fail(self, reason: str, *, distance: int | None = None, spread: int | None = None) -> Lifted:
        return Lifted(chrom=None, pos=None, confidence=None, detail=reason, distance_bp=distance, spread_bp=spread)


def _gap(anchor: Anchor, pos: int) -> int:
    if anchor.from_start <= pos <= anchor.from_end:
        return 0
    return int(min(abs(pos - anchor.from_start), abs(pos - anchor.from_end)))


class LiftAnchorsMissingError(LookupError):
    """The bundle has no anchors from one assembly to another; rebuild with the lift source."""


_cache: dict[tuple[str, int, str, str], GeneAnchorLiftover] = {}
_cache_lock = threading.Lock()


def liftover_for(bundle: Bundle, from_assembly: str, assembly: str, lengths: dict[str, int] | None = None) -> GeneAnchorLiftover:
    """Return the cached liftover from ``from_assembly`` to ``assembly`` in a bundle.

    Raises:
        LiftAnchorsMissingError: when the bundle has no such anchors.
    """
    key = (str(bundle.path), bundle.mtime, from_assembly, assembly)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached
    try:
        rows = bundle.rows_raw(
            "SELECT from_gene_id, from_chrom, from_start, from_end, from_strand, gene_id, chrom, start, \"end\", strand "
            "FROM lift_anchors WHERE from_assembly = ? AND assembly = ?",
            [from_assembly, assembly],
        )
    except duckdb.CatalogException as exc:
        raise LiftAnchorsMissingError(f"the {bundle.species} bundle predates lift anchors; rebuild it") from exc
    if not rows:
        raise LiftAnchorsMissingError(f"the {bundle.species} bundle has no {from_assembly} -> {assembly} anchors")
    liftover = GeneAnchorLiftover(
        from_assembly=from_assembly,
        assembly=assembly,
        anchors=[
            Anchor(
                from_gene_id=str(from_gene),
                from_chrom=str(from_chrom),
                from_start=int(from_start),
                from_end=int(from_end),
                from_strand=str(from_strand),
                gene_id=str(gene_id),
                chrom=str(chrom),
                start=int(start),
                end=int(end),
                strand=str(strand),
            )
            for from_gene, from_chrom, from_start, from_end, from_strand, gene_id, chrom, start, end, strand in rows
        ],
        lengths=dict(lengths or {}),
    )
    with _cache_lock:
        _cache[key] = liftover
    return liftover
