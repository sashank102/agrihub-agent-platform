"""Network neighbours, co-expression partners and seed propagation.

``seed_propagation`` runs a random walk with restart (RWR; Köhler et al.
2008) from trait seed genes over a STRING or ATTED-II graph and asks how
unusual each candidate's score is. For a column-stochastic transition matrix
``W`` and restart probability ``r`` the stationary vector from seed vector
``p0`` is ``K p0`` with ``K = r (I - (1 - r) W)^-1``. A candidate's score is
``(K^T e_c) . p0``, so one batched power iteration over the candidates gives
the row ``K^T e_c`` and every seed set can be scored by indexing it. The
empirical p-value draws ``n_perm`` random seed sets, each seed replaced by a
random gene of the same degree bin, and is ``(1 + #null >= observed) /
(1 + n_perm)``. A candidate that is itself a seed is scored without itself.

Seeds are the curated trait genes of the profile plus soybean genes whose
medium- or high-confidence Arabidopsis ortholog has a trait-matching mutant
phenotype or experimental GO term.
"""

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field
from scipy import sparse

from agrihub.state import EvidenceItem
from agrihub_data.bundle import Bundle
from agrihub_data.query.annotation import EXPERIMENTAL_GO_CODES
from agrihub_data.query.common import registry_of, source_version
from agrihub_data.query.overlap import known_trait_genes
from agrihub_data.query.traits import TraitProfile

Network = Literal["string", "atted"]
NETWORK_SOURCES = {"string": ("string_soybean", "STRING"), "atted": ("atted_soybean", "ATTED-II")}
DEFAULT_MIN_SCORE = {"string": 0.7, "atted": 3.0}
"""Edges below these scores are left out of walks: high-confidence STRING, z >= 3 co-expression."""
RESTART = 0.5
N_PERMUTATIONS = 1_000
DEGREE_BINS = 20
MAX_ITERATIONS = 100
TOLERANCE = 1e-8
MAX_SEEDS = 150
SEED_ORTHOLOG_METHODS = 2
RANDOM_SEED = 20_260_101


class Seed(BaseModel):
    """A gene the walk restarts from, and why."""

    gene_id: str
    reason: str


class NetworkNeighbor(BaseModel):
    """One neighbour of a gene in a network."""

    gene_id: str
    neighbor_id: str
    network: Network
    score: float
    rank: int | None = None
    channels: dict[str, float] | None = None
    neighbor_is_seed: bool = False
    neighbor_defline: str | None = None
    trait_key: str | None = None
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the edge as one network fact about ``gene_id``."""
        kind = "coexpression" if self.network == "atted" else "neighbor"
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="network",
                subtype=f"{kind}:{self.network}:{self.neighbor_id}",
                value=self.model_dump(exclude={"gene_id", "source_version"}),
                source_db=NETWORK_SOURCES[self.network][1],
                db_version=self.source_version,
                source_record=f"{self.network}:{self.gene_id}|{self.neighbor_id}|{self.trait_key or 'any'}",
                quote=self.neighbor_defline,
            )
        ]


class SeedContribution(BaseModel):
    """How much one seed contributes to a candidate's walk score."""

    seed: str
    share: float


class PropagationScore(BaseModel):
    """A candidate's RWR score from the trait seeds and its empirical p-value."""

    gene_id: str
    network: Network
    trait_key: str
    in_network: bool
    is_seed: bool = False
    score: float | None = None
    empirical_p: float | None = None
    rank: int | None = None
    """Rank among the requested candidates, 1 = closest to the seeds."""
    degree: int = 0
    n_seeds: int
    n_seeds_in_network: int
    n_permutations: int
    restart: float
    min_score: float
    top_seeds: list[SeedContribution] = Field(default_factory=list)
    seed_set: str
    """Digest of the seed gene ids, so different seed sets are different facts."""
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the propagation result as one network fact (nothing for genes off the network)."""
        if not self.in_network:
            return []
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="network",
                subtype=f"seed_propagation:{self.network}",
                value=self.model_dump(exclude={"gene_id", "source_version"}),
                source_db=f"AgriHub RWR over {NETWORK_SOURCES[self.network][1]}",
                db_version=self.source_version,
                source_record=f"{self.network}:{self.gene_id}|{self.trait_key}|seeds:{self.seed_set}|r={self.restart}|min={self.min_score}",
            )
        ]


def trait_seeds(bundle: Bundle, profile: TraitProfile) -> list[Seed]:
    """Return curated trait genes and genes with a trait-matched Arabidopsis ortholog."""
    canonical = registry_of(bundle).canonical_assembly
    seeds: dict[str, str] = {}
    for hit in known_trait_genes(bundle, profile):
        if hit.assembly == canonical and hit.trait_match != "none":
            seeds.setdefault(hit.gene_id, f"curated {'/'.join(hit.symbols[:2]) or hit.gene_id} ({hit.trait_match})")
    agis: dict[str, str] = {}
    for gene_id, phenotype in bundle.rows_raw("SELECT gene_id, phenotype FROM phenotypes WHERE species = 'arabidopsis' ORDER BY gene_id"):
        keywords = profile.matched_keywords(str(phenotype))
        if keywords:
            agis.setdefault(str(gene_id), f"phenotype mentions {keywords[0]}")
    wanted = sorted(set(profile.expanded_ids) | set(profile.go))
    codes = sorted(EXPERIMENTAL_GO_CODES)
    if wanted:
        for gene_id, go_id in bundle.rows_raw(
            "SELECT DISTINCT gene_id, go_id FROM go_annot WHERE species = 'arabidopsis' "
            f"AND go_id IN ({', '.join('?' for _ in wanted)}) AND evidence_code IN ({', '.join('?' for _ in codes)}) ORDER BY 1, 2",
            [*wanted, *codes],
        ):
            agis.setdefault(str(gene_id), f"experimental {go_id}")
    if agis:
        targets = sorted(agis)
        for gene_id, target, methods in bundle.rows_raw(
            "SELECT gene_id, target_gene_id, count(DISTINCT method) FROM orthologs WHERE assembly = ? AND target_species = 'arabidopsis' "
            f"AND target_gene_id IN ({', '.join('?' for _ in targets)}) GROUP BY 1, 2 HAVING count(DISTINCT method) >= ? ORDER BY 3 DESC, 1",
            [canonical, *targets, SEED_ORTHOLOG_METHODS],
        ):
            seeds.setdefault(str(gene_id), f"ortholog {target} ({agis[str(target)]}, {methods} methods)")
    return [Seed(gene_id=gene_id, reason=reason) for gene_id, reason in list(seeds.items())[:MAX_SEEDS]]


def network_neighbors(
    bundle: Bundle,
    gene_ids: list[str],
    network: Network = "string",
    min_score: float | None = None,
    limit: int = 10,
    seeds: set[str] | None = None,
    trait_key: str | None = None,
) -> list[NetworkNeighbor]:
    """Return each gene's strongest neighbours, best first, flagging seed genes."""
    wanted = list(dict.fromkeys(gene for gene in gene_ids if gene))
    if not wanted:
        return []
    threshold = DEFAULT_MIN_SCORE[network] if min_score is None else min_score
    placeholders = ", ".join("?" for _ in wanted)
    directed = network == "atted"
    query = (
        "SELECT gene_a AS gene_id, gene_b AS neighbor_id, score, rank, channels FROM edges "
        f"WHERE network = ? AND gene_a IN ({placeholders}) AND score >= ?"
        if directed
        else "SELECT gene_a AS gene_id, gene_b AS neighbor_id, score, rank, channels FROM edges "
        f"WHERE network = ? AND gene_a IN ({placeholders}) AND score >= ? UNION ALL "
        "SELECT gene_b, gene_a, score, rank, channels FROM edges "
        f"WHERE network = ? AND gene_b IN ({placeholders}) AND score >= ?"
    )
    parameters: list[Any] = [network, *wanted, threshold] + ([] if directed else [network, *wanted, threshold])
    rows = bundle.rows(f"SELECT * FROM ({query}) ORDER BY gene_id, score DESC, neighbor_id", parameters)
    neighbors = sorted({str(row["neighbor_id"]) for row in rows})
    deflines = _deflines(bundle, neighbors)
    version = source_version(bundle, NETWORK_SOURCES[network][0])
    kept: dict[str, list[NetworkNeighbor]] = {}
    for row in rows:
        items = kept.setdefault(str(row["gene_id"]), [])
        if len(items) >= limit:
            continue
        neighbor = str(row["neighbor_id"])
        items.append(
            NetworkNeighbor(
                gene_id=str(row["gene_id"]),
                neighbor_id=neighbor,
                network=network,
                score=round(float(row["score"]), 4),
                rank=row["rank"],
                channels=_channels(row["channels"]),
                neighbor_is_seed=neighbor in (seeds or set()),
                neighbor_defline=deflines.get(neighbor),
                trait_key=trait_key,
                source_version=version,
            )
        )
    order = {gene: index for index, gene in enumerate(wanted)}
    return [item for gene in sorted(kept, key=lambda gene: order.get(gene, 0)) for item in kept[gene]]


def seed_propagation(
    bundle: Bundle,
    candidates: list[str],
    seeds: list[str],
    network: Network = "string",
    *,
    trait_key: str = "custom",
    min_score: float | None = None,
    restart: float = RESTART,
    n_perm: int = N_PERMUTATIONS,
) -> list[PropagationScore]:
    """Score candidates by RWR proximity to the seeds with a degree-matched empirical p-value."""
    threshold = DEFAULT_MIN_SCORE[network] if min_score is None else min_score
    return propagate(
        network_graph(bundle, network, threshold),
        candidates,
        seeds,
        network=network,
        trait_key=trait_key,
        min_score=threshold,
        restart=restart,
        n_perm=n_perm,
        version=source_version(bundle, NETWORK_SOURCES[network][0]),
    )


def propagate(
    graph: "NetworkGraph",
    candidates: list[str],
    seeds: list[str],
    *,
    network: Network,
    trait_key: str,
    min_score: float,
    restart: float = RESTART,
    n_perm: int = N_PERMUTATIONS,
    version: str = "",
) -> list[PropagationScore]:
    """Score candidates on one graph; see :func:`seed_propagation`."""
    threshold = min_score
    wanted = list(dict.fromkeys(gene for gene in candidates if gene))
    seed_ids = list(dict.fromkeys(seed for seed in seeds if seed))
    seed_nodes = np.array([graph.index[seed] for seed in seed_ids if seed in graph.index], dtype=np.int64)
    digest = hashlib.sha1(",".join(sorted(seed_ids)).encode()).hexdigest()[:10]
    common = {
        "network": network,
        "trait_key": trait_key,
        "n_seeds": len(seed_ids),
        "n_seeds_in_network": int(seed_nodes.size),
        "n_permutations": n_perm,
        "restart": restart,
        "min_score": threshold,
        "seed_set": digest,
        "source_version": version,
    }
    present = [gene for gene in wanted if gene in graph.index]
    results = {gene: PropagationScore(gene_id=gene, in_network=False, **common) for gene in wanted if gene not in graph.index}
    if present and seed_nodes.size:
        rows = graph.rwr_rows(np.array([graph.index[gene] for gene in present], dtype=np.int64), restart)
        rng = np.random.default_rng(RANDOM_SEED)
        for column, gene in enumerate(present):
            node = graph.index[gene]
            own = seed_nodes[seed_nodes != node]
            row = rows[:, column]
            entry = PropagationScore(gene_id=gene, in_network=True, is_seed=own.size < seed_nodes.size, degree=int(graph.degree[node]), **common)
            if own.size:
                observed = float(row[own].mean())
                null = graph.random_seed_scores(row, own, n_perm, rng, exclude=node)
                entry.score = round(observed, 8)
                entry.empirical_p = round((1 + int((null >= observed).sum())) / (1 + n_perm), 4)
                shares = row[own] / row[own].sum() if row[own].sum() > 0 else np.zeros(own.size)
                top = np.argsort(-shares)[:3]
                entry.top_seeds = [SeedContribution(seed=graph.genes[int(own[index])], share=round(float(shares[index]), 3)) for index in top if shares[index] > 0]
            results[gene] = entry
    ranked = sorted((item for item in results.values() if item.score is not None), key=lambda item: -(item.score or 0.0))
    for rank, item in enumerate(ranked, start=1):
        item.rank = rank
    return [results[gene] for gene in wanted]


@dataclass
class NetworkGraph:
    """A symmetric weighted gene graph with its transition matrix and degree bins."""

    genes: list[str]
    index: dict[str, int]
    transition: sparse.csr_matrix
    """Column-stochastic ``W`` transposed (``W^T``), ready for the row iteration."""
    degree: np.ndarray
    bins: list[np.ndarray]
    bin_of: np.ndarray

    def rwr_rows(self, nodes: np.ndarray, restart: float) -> np.ndarray:
        """Return ``K^T e_c`` for each node ``c`` as the columns of an ``n x len(nodes)`` array."""
        start = np.zeros((len(self.genes), nodes.size))
        start[nodes, np.arange(nodes.size)] = 1.0
        current = start.copy()
        for _ in range(MAX_ITERATIONS):
            following = (1 - restart) * (self.transition @ current) + restart * start
            if np.abs(following - current).sum() < TOLERANCE * nodes.size:
                return following
            current = following
        return current

    def random_seed_scores(self, row: np.ndarray, seeds: np.ndarray, n_perm: int, rng: np.random.Generator, exclude: int) -> np.ndarray:
        """Return the scores of ``n_perm`` degree-matched random seed sets for one candidate row."""
        draws: np.ndarray = np.empty((n_perm, seeds.size), dtype=np.int64)
        for position, seed in enumerate(seeds):
            pool = self.bins[int(self.bin_of[seed])]
            pool = pool[pool != exclude] if pool.size > 1 else pool
            draws[:, position] = rng.choice(pool, size=n_perm, replace=True)
        return row[draws].mean(axis=1)


_graphs: dict[tuple[str, int, str, float], NetworkGraph] = {}
_graph_lock = threading.Lock()


def network_graph(bundle: Bundle, network: Network, min_score: float) -> NetworkGraph:
    """Return the cached graph of one network at one score threshold."""
    key = (str(bundle.path), bundle.mtime, network, float(min_score))
    with _graph_lock:
        cached = _graphs.get(key)
    if cached is not None:
        return cached
    edges = bundle.rows_raw("SELECT gene_a, gene_b, score FROM edges WHERE network = ? AND score >= ? ORDER BY 1, 2", [network, min_score])
    graph = build_graph([(str(gene_a), str(gene_b), float(score)) for gene_a, gene_b, score in edges])
    with _graph_lock:
        _graphs[key] = graph
    return graph


def build_graph(edges: list[tuple[str, str, float]]) -> NetworkGraph:
    """Return the symmetric graph of weighted edges; a pair listed both ways keeps its larger weight."""
    genes = sorted({gene for edge in edges for gene in edge[:2]})
    index = {gene: position for position, gene in enumerate(genes)}
    size = len(genes)
    if edges:
        rows = np.array([index[edge[0]] for edge in edges], dtype=np.int64)
        cols = np.array([index[edge[1]] for edge in edges], dtype=np.int64)
        weights = np.array([edge[2] for edge in edges])
        directed = sparse.coo_matrix((weights, (rows, cols)), shape=(size, size)).tocsr()
        adjacency = directed.maximum(directed.T).tocsr()
    else:
        adjacency = sparse.csr_matrix((size, size))
    strength = np.asarray(adjacency.sum(axis=0)).ravel()
    inverse = np.divide(1.0, strength, out=np.zeros_like(strength), where=strength > 0)
    transition = (adjacency @ sparse.diags(inverse)).T.tocsr()
    degree = np.diff(adjacency.indptr)
    bins, bin_of = _degree_bins(degree)
    return NetworkGraph(genes=genes, index=index, transition=transition, degree=degree, bins=bins, bin_of=bin_of)


def _degree_bins(degree: np.ndarray) -> tuple[list[np.ndarray], np.ndarray]:
    if degree.size == 0:
        return [], np.zeros(0, dtype=np.int64)
    order = np.argsort(degree, kind="stable")
    chunks = np.array_split(order, min(DEGREE_BINS, degree.size))
    bin_of: np.ndarray = np.empty(degree.size, dtype=np.int64)
    bins = []
    for number, chunk in enumerate(chunks):
        bin_of[chunk] = number
        bins.append(np.sort(chunk))
    return bins, bin_of


def _channels(value: Any) -> dict[str, float] | None:
    if not value:
        return None
    loaded = json.loads(value) if isinstance(value, str) else value
    return {str(key): float(score) for key, score in loaded.items() if score}


def _deflines(bundle: Bundle, genes: list[str]) -> dict[str, str]:
    if not genes:
        return {}
    canonical = registry_of(bundle).canonical_assembly
    return {
        str(gene): str(defline)
        for gene, defline in bundle.rows_raw(
            f"SELECT gene_id, defline FROM genes WHERE assembly = ? AND gene_id IN ({', '.join('?' for _ in genes)}) AND defline IS NOT NULL",
            [canonical, *genes],
        )
    }
