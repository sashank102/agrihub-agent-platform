"""Resolve gene ids from any registered namespace to canonical-assembly genes during a build.

A source id is first rewritten through the registry's gene namespaces (for
example ``Sobic.001G000100`` -> ``SORBI_3001G000100``, ``GLYMA_18G092200`` ->
``Glyma.18G092200``) and looked up case-insensitively among the genes loaded
so far. Ids of other namespaces or assemblies (MSU ``LOC_Os`` loci, maize v4
ids) go through ``id_map`` synonyms loaded earlier in the build; a synonym is
used only when it names exactly one canonical gene.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from agrihub_data.build.context import BuildContext


@dataclass
class GeneResolver:
    """Source gene id -> canonical gene id, with how it was resolved."""

    ctx: BuildContext
    genes: dict[str, str] = field(default_factory=dict)
    synonyms: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, ctx: BuildContext) -> "GeneResolver":
        """Read canonical genes and synonym links from the bundle under construction."""
        canonical = ctx.registry.canonical_assembly
        genes = {
            str(gene_id).casefold(): str(gene_id)
            for (gene_id,) in ctx.connection.execute("SELECT gene_id FROM genes WHERE assembly = ?", [canonical]).fetchall()
        }
        synonyms: dict[str, set[str]] = defaultdict(set)
        for from_id, to_id in ctx.connection.execute(
            "SELECT from_id, to_id FROM id_map WHERE relation = 'synonym' AND to_assembly = ?",
            [canonical],
        ).fetchall():
            synonyms[str(from_id).casefold()].add(str(to_id))
        return cls(ctx=ctx, genes=genes, synonyms=dict(synonyms))

    def resolve(self, value: str) -> tuple[str | None, str]:
        """Return ``(canonical gene, via)``; via is namespace, synonym, ambiguous or unmapped."""
        text = value.strip()
        if not text:
            return None, "unmapped"
        rewritten = self.ctx.registry.canonical_gene_id(text)
        if rewritten is not None:
            found = self.genes.get(rewritten.casefold())
            if found is not None:
                return found, "namespace"
        targets = self.synonyms.get(text.casefold()) or set()
        if len(targets) == 1:
            return next(iter(targets)), "synonym"
        if len(targets) > 1:
            return None, "ambiguous"
        return None, "unmapped"

    def get(self, value: str) -> str | None:
        """Return the canonical gene or ``None``."""
        return self.resolve(value)[0]
