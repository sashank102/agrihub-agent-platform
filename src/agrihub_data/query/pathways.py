"""Pathway membership of genes (PMN SoyCyc, Plant Reactome) and its trait relevance."""

from collections import defaultdict

from pydantic import BaseModel, Field

from agrihub.state import EvidenceItem
from agrihub_data.bundle import Bundle
from agrihub_data.query.common import registry_of
from agrihub_data.query.traits import TraitProfile

MAX_REACTIONS = 5


class PathwayMembership(BaseModel):
    """One gene in one pathway, with the trait keywords the pathway name matches."""

    gene_id: str
    database: str
    pathway_id: str
    pathway_name: str
    reactions: list[str] = Field(default_factory=list)
    ec: list[str] = Field(default_factory=list)
    trait_key: str | None = None
    matched: list[str] = Field(default_factory=list)
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the membership as one annotation fact keyed by database and pathway."""
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="functional_annotation",
                subtype=f"pathway:{self.database}:{self.pathway_id}",
                value=self.model_dump(exclude={"gene_id", "source_version"}),
                source_db=self.database,
                db_version=self.source_version,
                source_record=f"{self.database}:{self.pathway_id}|{self.trait_key or 'any'}",
                quote=self.pathway_name,
            )
        ]


def get_pathways(bundle: Bundle, gene_ids: list[str], profile: TraitProfile | None = None) -> list[PathwayMembership]:
    """Return each gene's pathways, trait-matching pathways first."""
    canonical = registry_of(bundle).canonical_assembly
    wanted = list(dict.fromkeys(gene for gene in gene_ids if gene))
    if not wanted:
        return []
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}
    reactions: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    ecs: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in bundle.rows(
        "SELECT gene_id, source_db, pathway_id, pathway_name, reaction_id, ec, source_version FROM pathways "
        f"WHERE assembly = ? AND gene_id IN ({', '.join('?' for _ in wanted)}) ORDER BY gene_id, source_db, pathway_id",
        [canonical, *wanted],
    ):
        key = (str(row["gene_id"]), str(row["source_db"]), str(row["pathway_id"]))
        grouped.setdefault(key, {"name": row["pathway_name"], "version": row["source_version"]})
        if row["reaction_id"]:
            reactions[key].add(str(row["reaction_id"]))
        if row["ec"]:
            ecs[key].add(str(row["ec"]))
    order = {gene: index for index, gene in enumerate(wanted)}
    memberships = [
        PathwayMembership(
            gene_id=gene_id,
            database=database,
            pathway_id=pathway_id,
            pathway_name=str(facts["name"]),
            reactions=sorted(reactions[(gene_id, database, pathway_id)])[:MAX_REACTIONS],
            ec=sorted(ecs[(gene_id, database, pathway_id)])[:MAX_REACTIONS],
            trait_key=profile.key if profile else None,
            matched=profile.matched_keywords(str(facts["name"])) if profile else [],
            source_version=str(facts["version"]),
        )
        for (gene_id, database, pathway_id), facts in grouped.items()
    ]
    memberships.sort(key=lambda item: (order[item.gene_id], not item.matched, item.database, item.pathway_id))
    return memberships
