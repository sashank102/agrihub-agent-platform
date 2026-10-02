"""Map gene ids of another Wm82 assembly to the canonical one, as loaded so far in the build.

The GFF ``ancestorIdentifier`` link is preferred; pangene co-membership is the
fallback and is used only when it names exactly one canonical gene. Sources
whose ids are on the canonical assembly map to themselves when the gene exists.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from agrihub_data.build.context import BuildContext


@dataclass
class CanonicalMap:
    """``source gene -> canonical gene`` with how each link was made."""

    source_assembly: str
    canonical: str
    genes: dict[str, str] = field(default_factory=dict)
    via: dict[str, str] = field(default_factory=dict)
    ambiguous: set[str] = field(default_factory=set)

    def get(self, gene_id: str) -> str | None:
        """Return the canonical gene for a source gene, or ``None``."""
        return self.genes.get(gene_id)


def canonical_map(ctx: BuildContext, source_assembly: str) -> CanonicalMap:
    """Return the map from ``source_assembly`` gene ids to canonical-assembly genes."""
    canonical = ctx.registry.canonical_assembly
    mapping = CanonicalMap(source_assembly=source_assembly, canonical=canonical)
    if source_assembly == canonical:
        for (gene_id,) in ctx.connection.execute("SELECT gene_id FROM genes WHERE assembly = ?", [canonical]).fetchall():
            mapping.genes[str(gene_id)] = str(gene_id)
            mapping.via[str(gene_id)] = "same_id"
        return mapping
    for source_gene, gene_id in ctx.connection.execute(
        "SELECT from_id, to_id FROM id_map WHERE relation = 'ancestor' AND assembly = ? AND to_assembly = ? ORDER BY 1, 2",
        [source_assembly, canonical],
    ).fetchall():
        mapping.genes.setdefault(str(source_gene), str(gene_id))
        mapping.via.setdefault(str(source_gene), "ancestor")
    members: dict[str, set[str]] = defaultdict(set)
    for source_gene, gene_id in ctx.connection.execute(
        """
        SELECT a.from_id, b.from_id FROM id_map AS a JOIN id_map AS b ON a.to_id = b.to_id
        WHERE a.relation = 'pangene_member' AND b.relation = 'pangene_member' AND a.assembly = ? AND b.assembly = ?
        """,
        [source_assembly, canonical],
    ).fetchall():
        members[str(source_gene)].add(str(gene_id))
    for source_gene, targets in members.items():
        if source_gene in mapping.genes:
            continue
        if len(targets) == 1:
            mapping.genes[source_gene] = next(iter(targets))
            mapping.via[source_gene] = "pangene"
        else:
            mapping.ambiguous.add(source_gene)
    return mapping
