"""Known-gene retrospective benchmark for one species bundle.

Pseudo-studies: for each benchmark trait, the curated trait genes of the
bundle (``SPECIES_BENCHMARKS`` names the curated sources per species: LIS
``glyma.traits.yml`` for soybean, RAP-DB curated genes and funRiceGenes for
rice, MaizeGDB classical genes for maize, the Grant et al. 2023 Ma/Dw genes
for sorghum) that have a trait-matched GWAS Atlas hit within the species'
typical LD distance of the gene. The most significant such hit becomes the
study SNP and the curated gene is the target of its locus.

The target's own curated record, every catalog GWAS hit that reports it, and
the input hit itself are held out while the study runs, so the rubric cannot
find the answer it is graded on. Other evidence (annotation, orthology,
expression, networks, literature links) stays.

Four rankings are compared per locus: the full pipeline (agents), the
deterministic rubric with no specialist rounds, the same rubric with the trait
profile's seed families and curated keywords removed (the ablation that shows
how much the textbook priors carry), and distance to the nearest SNP. Recall
comes with a percentile bootstrap 95% interval over targets. The verifier's
verdicts give the unsupported- and contradicted-claim rates; cited aliases are
checked against the run's evidence store.
"""

import asyncio
import csv
import hashlib
import io
import random
import re
import statistics
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from agrihub.evidence_store import EvidenceStore
from agrihub.fake_llm import call, register_script, reply
from agrihub.graph import build_study_graph
from agrihub_data.bundle import Bundle
from agrihub_data.query import network, overlap
from agrihub_data.query.common import Region
from agrihub_data.query.traits import map_trait, without_priors
from agrihub_data.registry import load_species

SOYBEAN_TRAITS = (
    "plant height",
    "flowering time",
    "maturity",
    "seed protein content",
    "seed oil content",
    "seed weight",
    "nodulation",
    "flower color",
)
CATALOG = "GWAS Atlas"
RUBRIC_ONLY = "agrihub-fake:rubric_only"
BOOTSTRAP_SAMPLES = 2000
METHODS: tuple[tuple[str, str | None, bool], ...] = (
    ("rubric_only", RUBRIC_ONLY, True),
    ("rubric_no_priors", RUBRIC_ONLY, False),
    ("agents", None, True),
)
"""``(method, orchestrator model, trait priors on)`` of every ranking that runs a study."""


@dataclass(frozen=True)
class BenchmarkSpec:
    """Which curated genes are targets and which traits are benchmarked for one species."""

    traits: tuple[str, ...]
    target_sources: tuple[str, ...]
    target_note: str
    order: str = "gene_id"
    """``gene_id`` takes targets in id order; ``spread`` in a fixed hash order, so they span chromosomes."""
POSTER_SNPS = (
    {"raw": "S5_2899164", "chrom": "5", "pos": 2_899_164},
    {"raw": "S18_9263941", "chrom": "18", "pos": 9_263_941},
    {"raw": "S18_51620945", "chrom": "18", "pos": 51_620_945},
)
POSTER_TARGET = "Glyma.18G092200"
_ALIAS = re.compile(r"\[(E\d+)\]")


def _rubric_only_script(messages: list[Any], tools: tuple[str, ...]) -> Any:
    """Finish research at once, so the ranking is the deterministic rubric on harvested evidence."""
    return reply(call("finish_research", {"reason": "Rubric-only baseline: no specialist rounds."}, "call_rubric_only"))


register_script("rubric_only", _rubric_only_script)


@dataclass
class Target:
    """A curated trait gene and the catalog hit used as its study SNP."""

    trait: str
    gene_id: str
    symbol: str
    hit_id: str
    marker: str
    chrom: str
    pos: int
    p_value: float | None
    distance_bp: int
    held_out_hits: list[str] = field(default_factory=list)


@dataclass
class PseudoStudy:
    """One benchmark study: a trait and the targets whose hits are its SNPs."""

    trait: str
    targets: list[Target]
    study: dict[str, Any]


SPECIES_BENCHMARKS: dict[str, BenchmarkSpec] = {
    "soybean": BenchmarkSpec(SOYBEAN_TRAITS, ("LIS gene_functions",), "curated trait genes (LIS glyma.traits.yml)"),
    "rice": BenchmarkSpec(
        ("plant height", "heading date", "grain yield", "grain weight"),
        ("RAP-DB curated genes", "RAP-DB agronomic genes", "funRiceGenes"),
        "RAP-DB curated and agronomic genes and funRiceGenes keyword genes",
        order="spread",
    ),
    "maize": BenchmarkSpec(
        ("plant height", "flowering time", "grain yield", "kernel weight"),
        ("MaizeGDB classical genes",),
        "MaizeGDB classical (named) genes whose names match the trait",
        order="spread",
    ),
    "sorghum": BenchmarkSpec(
        ("plant height", "flowering time", "grain yield", "grain weight"),
        ("Grant et al. 2023 Ma/Dw genes", "Sorghum QTL Atlas"),
        "cloned Ma/Dw genes (Grant et al. 2023) and Sorghum QTL Atlas major genes when an export is loaded",
    ),
}


def spec_for(species: str) -> BenchmarkSpec:
    """Return the benchmark definition of a species."""
    if species not in SPECIES_BENCHMARKS:
        raise KeyError(f"no benchmark is defined for {species}; known: {', '.join(sorted(SPECIES_BENCHMARKS))}")
    return SPECIES_BENCHMARKS[species]


def build_pseudo_studies(
    bundle: Bundle,
    traits: tuple[str, ...] | None = None,
    *,
    max_loci: int = 8,
    max_distance_bp: int | None = None,
    flank_bp: int | None = None,
) -> list[PseudoStudy]:
    """Return one pseudo-study per trait with at least one target, targets on distinct loci.

    ``traits`` default to the species' benchmark traits; ``max_distance_bp``
    to its typical LD distance. Targets come from the species' curated sources.
    """
    species = bundle.species
    spec = spec_for(species)
    registry = load_species(species)
    assembly = registry.canonical_assembly
    max_distance_bp = max_distance_bp or int(registry.typical_ld_kb * 1_000)
    flank = flank_bp or registry.default_window.flank_bp
    studies = []
    used_genes: set[str] = set()
    for trait in traits or spec.traits:
        profile = map_trait(trait, species, bundle)
        known: dict[str, Any] = {}
        for hit in overlap.known_trait_genes(bundle, profile, assembly=assembly):
            if hit.source_db in spec.target_sources:
                known.setdefault(hit.gene_id, hit)
        targets: list[Target] = []
        for gene_id, record in sorted(known.items(), key=lambda item: _target_order(item[0], spec.order)):
            if gene_id in used_genes or record.chrom is None or record.start is None or record.end is None:
                continue
            region = Region(
                label=gene_id,
                chrom=str(record.chrom),
                start=max(1, int(record.start) - max_distance_bp),
                end=int(record.end) + max_distance_bp,
                core_start=int(record.start),
                core_end=int(record.end),
                assembly=assembly,
            )
            hits = [
                hit
                for hit in overlap.gwas_catalog_overlap(bundle, region, profile, assembly=assembly, trait_only=True)
                if hit.source_db == CATALOG
            ]
            if not hits:
                continue
            hits.sort(key=lambda hit: (hit.p_value if hit.p_value is not None else 1.0, hit.distance_to_core or 0, hit.hit_id))
            best = hits[0]
            if any(item.chrom == best.chrom and abs(item.pos - best.pos) < 2 * flank for item in targets):
                continue
            reporting = [
                hit.hit_id
                for hit in overlap.gwas_catalog_overlap(bundle, region, None, assembly=assembly)
                if gene_id in (hit.reported_genes or [])
            ]
            targets.append(
                Target(
                    trait=trait,
                    gene_id=gene_id,
                    symbol=", ".join(record.symbols[:2]),
                    hit_id=best.hit_id,
                    marker=f"{trait.replace(' ', '_')}:{best.marker or best.hit_id}",
                    chrom=best.chrom,
                    pos=best.pos,
                    p_value=best.p_value,
                    distance_bp=int(best.distance_to_core or 0),
                    held_out_hits=sorted({best.hit_id, *reporting}),
                )
            )
            used_genes.add(gene_id)
            if len(targets) >= max_loci:
                break
        if not targets:
            continue
        study: dict[str, Any] = {
            "mode": "snps",
            "species": species,
            "assembly": assembly,
            "trait_text": trait,
            "snps": [{"raw": target.marker, "chrom": target.chrom, "pos": target.pos} for target in targets],
        }
        if flank_bp is not None:
            study["window"] = {"mode": "fixed", "flank_bp": flank_bp}
        studies.append(PseudoStudy(trait=trait, targets=targets, study=study))
    return studies


def _target_order(gene_id: str, order: str) -> str:
    return hashlib.sha256(gene_id.encode()).hexdigest() if order == "spread" else gene_id


@contextmanager
def holdout(gene_ids: set[str], hit_ids: set[str]) -> Iterator[None]:
    """Hide curated records of ``gene_ids`` and catalog hits ``hit_ids`` from every tool for the duration."""
    known = overlap.known_trait_genes
    catalog = overlap.gwas_catalog_overlap
    seeds = network.known_trait_genes

    def filtered_known(*args: Any, **kwargs: Any) -> list[Any]:
        return [hit for hit in known(*args, **kwargs) if hit.gene_id not in gene_ids]

    def filtered_catalog(*args: Any, **kwargs: Any) -> list[Any]:
        return [hit for hit in catalog(*args, **kwargs) if hit.hit_id not in hit_ids]

    def filtered_seeds(*args: Any, **kwargs: Any) -> list[Any]:
        return [hit for hit in seeds(*args, **kwargs) if hit.gene_id not in gene_ids]

    overlap.known_trait_genes = filtered_known  # type: ignore[assignment]
    overlap.gwas_catalog_overlap = filtered_catalog  # type: ignore[assignment]
    network.known_trait_genes = filtered_seeds  # type: ignore[assignment]
    try:
        yield
    finally:
        overlap.known_trait_genes = known
        overlap.gwas_catalog_overlap = catalog
        network.known_trait_genes = seeds


@dataclass
class RunOutcome:
    """One finished study run."""

    values: dict[str, Any]
    events: list[dict[str, Any]]
    seconds: float
    run_id: str

    @property
    def tokens(self) -> dict[str, int]:
        """Return input and output tokens summed over agents (``agent.usage`` is cumulative per agent)."""
        last: dict[str, tuple[int, int]] = {}
        for event in self.events:
            if event.get("type") == "agent.usage":
                data = event["data"]
                last[event["agent"]["id"]] = (int(data.get("input_tokens") or 0), int(data.get("output_tokens") or 0))
        return {"input": sum(item[0] for item in last.values()), "output": sum(item[1] for item in last.values())}


def run_study(study: dict[str, Any], *, orchestrator_model: str | None = None) -> RunOutcome:
    """Run one study on the in-memory checkpointer and return its final state and events."""
    run_id = uuid.uuid4().hex
    configurable: dict[str, Any] = {"thread_id": run_id, "run_id": run_id}
    if orchestrator_model:
        configurable["orchestrator_model"] = orchestrator_model

    async def scenario() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        graph = build_study_graph(checkpointer=InMemorySaver())
        config = {"configurable": configurable}
        events = [
            part["data"]
            async for part in graph.astream({"study": study}, config, stream_mode=["custom"], subgraphs=True, version="v2")
        ]
        return events, dict((await graph.aget_state(config)).values)

    started = time.monotonic()
    events, values = asyncio.run(scenario())
    return RunOutcome(values=values, events=events, seconds=time.monotonic() - started, run_id=run_id)


def locus_of(report: dict[str, Any], target: Target) -> dict[str, Any] | None:
    """Return the report locus whose window holds the target's study SNP."""
    for locus in report.get("loci") or []:
        if target.marker in (locus.get("snp_positions") or {}) or locus.get("lead_snp") == target.marker:
            return dict(locus)
    return None


def rank_in_locus(candidates: list[dict[str, Any]], locus_id: str, gene_id: str, key: Callable[[dict[str, Any]], Any]) -> int | None:
    """Return the 1-based rank of ``gene_id`` among the locus's candidates ordered by ``key``."""
    rows = sorted((row for row in candidates if row.get("locus_id") == locus_id), key=key)
    for index, row in enumerate(rows, start=1):
        if row.get("gene_id") == gene_id:
            return index
    return None


def by_score(row: dict[str, Any]) -> tuple[Any, ...]:
    """Order by the rubric score, as the report does."""
    return (row.get("rank_in_locus") or 10**6, row.get("gene_id"))


def by_distance(row: dict[str, Any]) -> tuple[Any, ...]:
    """Order by distance to the nearest SNP of the locus; genes over a SNP first."""
    return (row.get("distance_bp") if row.get("distance_bp") is not None else 10**9, row.get("start") or 0, row.get("gene_id"))


def claim_rates(report: dict[str, Any]) -> dict[str, Any]:
    """Return verifier verdict counts and the unsupported- and contradicted-claim rates."""
    verdicts = [str(item.get("status")) for item in report.get("verification") or []]
    total = len(verdicts)
    return {
        "claims": total,
        "verified": verdicts.count("verified"),
        "unverified": verdicts.count("unverified"),
        "contradicted": verdicts.count("contradicted"),
        "unsupported_rate": verdicts.count("unverified") / total if total else None,
        "contradicted_rate": verdicts.count("contradicted") / total if total else None,
    }


def citation_check(report: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Return how many aliases the report cites and how many resolve in the run's evidence store."""
    cited = set(_ALIAS.findall("\n".join(str(report.get(key) or "") for key in ("markdown", "details_markdown"))))
    cited |= {str(item.get("alias")) for item in report.get("citations") or [] if item.get("alias")}
    store = EvidenceStore.for_run(run_id)
    try:
        known = {item.alias for item in store.get(sorted(cited))}
    finally:
        store.close()
    return {"cited": len(cited), "resolvable": len(cited & known)}


def score_study(pseudo: PseudoStudy, outcome: RunOutcome, method: str) -> list[dict[str, Any]]:
    """Return one row per target: its locus, the method's rank and the distance-only rank."""
    report = outcome.values.get("report") or {}
    candidates = list(report.get("candidates_full") or [])
    rows = []
    for target in pseudo.targets:
        locus = locus_of(report, target)
        locus_id = str(locus["locus_id"]) if locus else ""
        in_locus = [row for row in candidates if row.get("locus_id") == locus_id]
        rows.append(
            {
                "trait": pseudo.trait,
                "method": method,
                "gene_id": target.gene_id,
                "symbol": target.symbol,
                "snp": target.marker,
                "hit_distance_bp": target.distance_bp,
                "locus_id": locus_id,
                "n_genes": len(in_locus),
                "in_locus": any(row.get("gene_id") == target.gene_id for row in in_locus),
                "rank": rank_in_locus(candidates, locus_id, target.gene_id, by_score),
                "distance_rank": rank_in_locus(candidates, locus_id, target.gene_id, by_distance),
                "tier": next((row.get("tier") for row in in_locus if row.get("gene_id") == target.gene_id), None),
            }
        )
    return rows


def recall(ranks: list[int | None], k: int) -> float | None:
    """Return the share of targets ranked within the top ``k`` (a missing target counts as a miss)."""
    if not ranks:
        return None
    return sum(1 for rank in ranks if rank is not None and rank <= k) / len(ranks)


def recall_interval(ranks: list[int | None], k: int, *, samples: int = BOOTSTRAP_SAMPLES, seed: int = 0) -> tuple[float, float] | None:
    """Return the percentile bootstrap 95% interval of recall@k, resampling targets with replacement."""
    if not ranks:
        return None
    hits = [1 if rank is not None and rank <= k else 0 for rank in ranks]
    generator = random.Random(seed)
    values = sorted(sum(generator.choices(hits, k=len(hits))) / len(hits) for _ in range(samples))
    return values[int(0.025 * (samples - 1))], values[int(0.975 * (samples - 1))]


@dataclass
class Scorecard:
    """Per-target rows, per-study run metrics and the summary table."""

    rows: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    smoke: list[dict[str, Any]]
    model: str
    notes: list[str] = field(default_factory=list)
    species: str = "soybean"
    skipped: list[str] = field(default_factory=list)
    """Benchmark traits without a target, with the reason."""

    @property
    def scripted(self) -> bool:
        """Return whether the agents ran on the scripted fake model."""
        return self.model.startswith("agrihub-fake:")

    def summary(self) -> list[dict[str, Any]]:
        """Return recall@1/3/5 with bootstrap 95% intervals and the median rank for every ranking."""
        by_method = {method: [row for row in self.rows if row["method"] == method] for method, _, _ in METHODS}
        agents_label = "agents (full pipeline)" + (" - pending real model" if self.scripted else "")
        table = []
        for name, source, column in (
            ("distance only", by_method["agents"] or by_method["rubric_only"], "distance_rank"),
            ("rubric only (no agents)", by_method["rubric_only"], "rank"),
            ("rubric without seed families / keyword priors", by_method["rubric_no_priors"], "rank"),
            (agents_label, by_method["agents"], "rank"),
        ):
            ranks = [row[column] for row in source]
            found = [rank for rank in ranks if rank is not None]
            table.append(
                {
                    "method": name,
                    "targets": len(ranks),
                    **{f"recall@{k}": recall(ranks, k) for k in (1, 3, 5)},
                    **{f"ci@{k}": recall_interval(ranks, k) for k in (1, 3, 5)},
                    "median_rank": statistics.median(found) if found else None,
                }
            )
        return table

    def csv_text(self) -> str:
        """Return the per-target rows as CSV."""
        buffer = io.StringIO()
        fields = list(self.rows[0]) if self.rows else ["trait"]
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        writer.writerows(self.rows)
        return buffer.getvalue()

    def markdown(self) -> str:
        """Return the scorecard as Markdown."""
        lines = [
            f"# {self.species.capitalize()} known-gene benchmark",
            "",
            f"Model: `{self.model}`"
            + (" (scripted; agent results are pending a real model)" if self.scripted else "")
            + f". {len({row['trait'] for row in self.rows})} pseudo-studies, {len({row['gene_id'] for row in self.rows})} target genes.",
            "",
            "## Recall of the curated gene within its locus",
            "",
            "Brackets are percentile bootstrap 95% intervals over targets "
            f"({BOOTSTRAP_SAMPLES} resamples).",
            "",
            "| Ranking | Targets | recall@1 | recall@3 | recall@5 | Median rank |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for row in self.summary():
            cells = " | ".join(_with_ci(row[f"recall@{k}"], row[f"ci@{k}"]) for k in (1, 3, 5))
            lines.append(
                f"| {row['method']} | {row['targets']} | {cells} | {row['median_rank'] if row['median_rank'] is not None else '-'} |"
            )
        lines.extend(
            [
                "",
                "## Runs",
                "",
                "| Trait | Method | Loci | Genes | Seconds | Input tokens | Output tokens | Claims | Unsupported | Contradicted | Citations resolvable |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for run in self.runs:
            lines.append(
                f"| {run['trait']} | {run['method']} | {run['loci']} | {run['genes']} | {run['seconds']:.1f} | "
                f"{run['input_tokens']} | {run['output_tokens']} | {run['claims']} | {_pct(run['unsupported_rate'])} | "
                f"{_pct(run['contradicted_rate'])} | {run['resolvable']}/{run['cited']} |"
            )
        agents = [run for run in self.runs if run["method"] == "agents"]
        claims = sum(run["claims"] for run in agents)
        if agents:
            lines.extend(
                [
                    "",
                    ("Agent runs (scripted model, pending real model): " if self.scripted else "Agent runs: ")
                    + f"{claims} verifier-checked claims, "
                    f"{_pct(sum(run['unverified'] for run in agents) / claims if claims else None)} unsupported, "
                    f"{_pct(sum(run['contradicted'] for run in agents) / claims if claims else None)} contradicted; "
                    f"{sum(run['resolvable'] for run in agents)}/{sum(run['cited'] for run in agents)} cited aliases resolve; "
                    f"{sum(run['seconds'] for run in agents):.0f} s and "
                    f"{sum(run['input_tokens'] + run['output_tokens'] for run in agents)} tokens in total.",
                ]
            )
        if self.smoke:
            lines.extend(["", "## Smoke case: poster SNPs (plant height)", "", "| Method | Target | Locus | Rank | Distance rank | Tier | Seconds |", "| --- | --- | --- | --- | --- | --- | --- |"])
            for row in self.smoke:
                lines.append(f"| {row['method']} | {row['gene_id']} | {row['locus_id']} | {row['rank']} | {row['distance_rank']} | {row['tier']} | {row['seconds']:.1f} |")
        lines.extend(
            [
                "",
                "## Targets",
                "",
                "| Trait | Gene | Symbol | Hit distance | Genes in locus | Distance rank | Rubric rank | No-priors rank | Agent rank |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        ranks = {(row["method"], row["trait"], row["gene_id"]): row for row in self.rows}
        for row in (item for item in self.rows if item["method"] == "agents"):
            rubric = ranks.get(("rubric_only", row["trait"], row["gene_id"]), {})
            ablated = ranks.get(("rubric_no_priors", row["trait"], row["gene_id"]), {})
            lines.append(
                f"| {row['trait']} | {row['gene_id']} | {row['symbol']} | {row['hit_distance_bp'] / 1000:.1f} kb | {row['n_genes']} | "
                f"{row['distance_rank']} | {rubric.get('rank')} | {ablated.get('rank')} | {row['rank']} |"
            )
        if self.skipped:
            lines.extend(["", "## Traits without targets", ""])
            lines.extend(f"- {item}" for item in self.skipped)
        if self.notes:
            lines.extend(["", "## Notes", ""])
            lines.extend(f"- {note}" for note in self.notes)
        return "\n".join(lines) + "\n"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _with_ci(value: float | None, interval: tuple[float, float] | None) -> str:
    if value is None:
        return "-"
    return f"{value:.0%} [{interval[0]:.0%}-{interval[1]:.0%}]" if interval else f"{value:.0%}"


def run_benchmark(
    bundle: Bundle,
    *,
    traits: tuple[str, ...] | None = None,
    max_loci: int = 8,
    flank_bp: int | None = None,
    model: str,
    smoke: bool = True,
    progress: Callable[[str], None] | None = None,
) -> Scorecard:
    """Run every pseudo-study with each ranking method, plus the poster smoke case for soybean."""
    say = progress or (lambda message: None)
    species = bundle.species
    spec = spec_for(species)
    registry = load_species(species)
    wanted = traits or spec.traits
    studies = build_pseudo_studies(bundle, wanted, max_loci=max_loci, flank_bp=flank_bp)
    covered = {study.trait for study in studies}
    skipped = [
        f"{trait}: no {spec.target_note} with a trait-matched GWAS Atlas hit within {registry.typical_ld_kb:g} kb"
        for trait in wanted
        if trait not in covered
    ]
    rows: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    for pseudo in studies:
        genes = {target.gene_id for target in pseudo.targets}
        hits = {hit for target in pseudo.targets for hit in target.held_out_hits}
        for method, orchestrator, priors in METHODS:
            say(f"{species} {pseudo.trait}: {method} ({len(pseudo.targets)} loci)")
            with holdout(genes, hits), (nullcontext() if priors else without_priors()):
                outcome = run_study(pseudo.study, orchestrator_model=orchestrator)
            report = outcome.values.get("report") or {}
            rows.extend(score_study(pseudo, outcome, method))
            runs.append(
                {
                    "trait": pseudo.trait,
                    "method": method,
                    "loci": len(report.get("loci") or []),
                    "genes": len(report.get("candidates_full") or []),
                    "seconds": outcome.seconds,
                    "input_tokens": outcome.tokens["input"],
                    "output_tokens": outcome.tokens["output"],
                    **claim_rates(report),
                    **citation_check(report, outcome.run_id),
                }
            )
    smoke_rows: list[dict[str, Any]] = []
    if smoke and species == "soybean":
        study = {"mode": "snps", "species": species, "assembly": registry.canonical_assembly, "trait_text": "plant height", "snps": list(POSTER_SNPS)}
        target = Target("plant height", POSTER_TARGET, "", "", "S18_9263941", "Gm18", 9_263_941, None, 0)
        poster = PseudoStudy("plant height (poster)", [target], study)
        for method, orchestrator in (("rubric_only", RUBRIC_ONLY), ("agents", None)):
            say(f"poster smoke: {method}")
            outcome = run_study(study, orchestrator_model=orchestrator)
            [row] = score_study(poster, outcome, method)
            smoke_rows.append({**row, "seconds": outcome.seconds})
    notes = [
        f"Targets are {spec.target_note} with a trait-matched GWAS Atlas hit within the typical {species} LD distance "
        f"({registry.typical_ld_kb:g} kb); the most significant hit is the study SNP.",
        "Held out while each study runs: every curated record of the target, catalog GWAS hits that report it, and the input hit.",
        "Ranks are within the target's locus (all genes in its window); a target outside every locus counts as a miss.",
        "Distance-only ranks the same genes by distance to the nearest study SNP.",
        "The no-priors ablation reruns the rubric with the trait profile's seed families and curated keywords removed; "
        "ontology terms stay, and the only keyword left is the trait text itself.",
        "The verifier's verdicts are deterministic; the rates measure what it catches among the top candidates' claims. "
        "Unsupported means no independent source confirmed the claim; contradicted means another source or a passage check refuted it.",
    ]
    if model.startswith("agrihub-fake:"):
        notes.append(
            "The agents ran on the scripted fake model, so the agents row equals the rubric up to the specialists' "
            "scripted findings and is not a result: it is pending a real model. The scripted model reads no literature "
            "passages (gene2pubmed links only), so the unsupported rate is expected to be near 100%."
        )
    return Scorecard(rows=rows, runs=runs, smoke=smoke_rows, model=model, notes=notes, species=species, skipped=skipped)


def as_dicts(studies: list[PseudoStudy]) -> list[dict[str, Any]]:
    """Return pseudo-studies as plain dictionaries (for a dry run listing)."""
    return [{"trait": study.trait, "targets": [asdict(target) for target in study.targets]} for study in studies]
