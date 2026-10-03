"""Point liftover through a UCSC chain file (MaizeGDB ``B73_RefGen_v4_to_Zm-B73-REFERENCE-NAM-5.0.chain``).

A chain lists aligned blocks ``size dt dq`` between gaps; a position inside an
aligned block maps by its offset, a position in a gap does not map. When
several chains cover a position the highest-scoring one wins, as liftOver
does with ``-minMatch`` relaxed. Chromosome names are normalized through the
registry on both sides.
"""

import bisect
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agrihub_data.build.context import open_text


@dataclass(frozen=True)
class Block:
    """An ungapped aligned block: ``[start, start + size)`` on the source maps to ``target_start`` (0-based)."""

    start: int
    size: int
    target_chrom: str
    target_start: int
    reverse: bool
    target_size: int
    score: int


@dataclass
class ChainLiftover:
    """Lift 1-based positions from a chain file's source assembly to its target."""

    blocks: dict[str, list[Block]] = field(default_factory=dict)
    _starts: dict[str, list[int]] = field(default_factory=dict)

    @classmethod
    def read(cls, path: Path, source_chrom: Callable[[str], str | None], target_chrom: Callable[[str], str | None]) -> "ChainLiftover":
        """Parse a chain file; chromosomes the callbacks reject are skipped."""
        grouped: dict[str, list[Block]] = defaultdict(list)
        with open_text(path) as handle:
            current: tuple[str, str, int, int, bool, int, int] | None = None
            for line in handle:
                fields = line.split()
                if not fields:
                    current = None
                    continue
                if fields[0] == "chain":
                    header = fields[1:11]
                    source_name, target_name = source_chrom(header[1]), target_chrom(header[6])
                    if header[3] != "+" or source_name is None or target_name is None:
                        current = None
                        continue
                    current = (source_name, target_name, int(header[4]), int(header[9]), header[8] == "-", int(header[7]), int(header[0]))
                    continue
                if current is None:
                    continue
                source, target, t_pos, q_pos, reverse, q_size, score = current
                size = int(fields[0])
                grouped[source].append(Block(t_pos, size, target, q_pos, reverse, q_size, score))
                if len(fields) >= 3:
                    current = (source, target, t_pos + size + int(fields[1]), q_pos + size + int(fields[2]), reverse, q_size, score)
        liftover = cls()
        for chrom, items in grouped.items():
            items.sort(key=lambda block: (block.start, -block.score))
            liftover.blocks[chrom] = items
            liftover._starts[chrom] = [block.start for block in items]
        return liftover

    @property
    def size(self) -> int:
        """Return the number of aligned blocks."""
        return sum(len(items) for items in self.blocks.values())

    def lift(self, chrom: str, pos: int) -> tuple[str, int] | None:
        """Return the target ``(chrom, pos)`` of a 1-based position, or ``None`` in a gap."""
        items = self.blocks.get(chrom)
        if not items:
            return None
        zero = pos - 1
        index = bisect.bisect_right(self._starts[chrom], zero)
        best: Block | None = None
        for block in reversed(items[max(0, index - 64) : index]):
            if block.start <= zero < block.start + block.size and (best is None or block.score > best.score):
                best = block
        if best is None:
            return None
        offset = zero - best.start
        target = best.target_start + offset
        if best.reverse:
            target = best.target_size - 1 - target
        return best.target_chrom, target + 1
