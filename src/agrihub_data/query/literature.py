"""Literature: gene aliases, publication links, online search and verbatim passages.

Offline functions read the NCBI Gene tables of the bundle (``ncbi_genes``,
``ncbi_gene_pubmed``, ``ncbi_gene_go``). Online functions use one
:class:`LiteratureClient` that rate-limits each API to its documented limit,
retries 429/5xx responses with backoff, and caches responses on disk:

- Europe PMC REST: no published limit, so 5 requests/s.
- PubTator3: at most 3 requests/s (PubTator3 API page).
- NCBI E-utilities: 3 requests/s, 10 with ``NCBI_API_KEY``.

Every search hit and passage becomes an ``EvidenceItem`` in the literature
category with a PMID citation and a verbatim quote (the title for a hit, the
sentence for a passage). Passages are sentences in which an alias of the gene
and a trait term co-occur, with a species term in the same sentence, or in
the title or abstract when the alias names only this gene. A relation class
(causal-experimental, association, mention) comes from fixed cue words.
"""

import asyncio
import hashlib
import html
import json
import os
import re
import xml.etree.ElementTree as ElementTree
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from aiolimiter import AsyncLimiter
from pydantic import BaseModel, Field
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from agrihub.state import EvidenceItem
from agrihub_data.bundle import Bundle
from agrihub_data.query import orthology
from agrihub_data.query.common import registry_of

Api = Literal["europe_pmc", "pubtator3", "pubmed"]
Relation = Literal["causal-experimental", "association", "mention"]
AliasKind = Literal[
    "gene_id",
    "legacy_id",
    "symbol",
    "ncbi_symbol",
    "ncbi_locus",
    "ncbi_synonym",
    "designation",
    "ortholog_symbol",
    "ortholog_prefixed",
]

HUB_PMID_GENES = 30
"""A PMID linked to more genes than this in gene2pubmed is a hub paper (genome, atlas) and is dropped."""
CACHE_TTL_SECONDS = 7 * 24 * 3600
MAX_QUERY_ALIASES = 10
MAX_TRAIT_TERMS = 10
EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
PUBTATOR = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
RATE_LIMITS: dict[Api, float] = {"europe_pmc": 5.0, "pubtator3": 3.0, "pubmed": 3.0}
NCBI_KEY_RATE = 10.0
SOURCES: dict[Api, dict[str, str]] = {
    "europe_pmc": {
        "name": "Europe PMC REST API",
        "url": "https://europepmc.org/RestfulWebService",
        "license": "Europe PMC terms of use; abstracts keep publisher copyright, open-access full text under its own licence",
    },
    "pubtator3": {
        "name": "PubTator3 (NCBI)",
        "url": "https://www.ncbi.nlm.nih.gov/research/pubtator3/",
        "license": "NCBI annotations are public domain; article text keeps publisher copyright",
    },
    "pubmed": {
        "name": "PubMed via NCBI E-utilities",
        "url": "https://www.ncbi.nlm.nih.gov/books/NBK25501/",
        "license": "NCBI public data; abstracts keep publisher copyright",
    },
}
_NCBI_TABLES = ("ncbi_genes", "ncbi_gene_pubmed", "ncbi_gene_go")
_LOC_SYMBOL = re.compile(r"^LOC\d+$")
_SPECIFIC_KINDS = frozenset({"gene_id", "legacy_id", "symbol", "ncbi_symbol", "ncbi_locus", "ncbi_synonym"})
_UNSEARCHABLE_KINDS = frozenset({"ncbi_locus"})
_CAUSAL = re.compile(
    r"\b(mutants?|mutation|knock-?outs?|knock-?downs?|knocked[- ]out|CRISPR|Cas9|gene[- ]edit\w*|"
    r"over-?express\w*|transgenic|RNAi|silenc\w*|complement\w*|loss[- ]of[- ]function|"
    r"gain[- ]of[- ]function|map-based cloning|positional cloning|near[- ]isogenic|"
    r"functional(?:ly)? validat\w*|ectopic(?:ally)? express\w*)",
    re.IGNORECASE,
)
_ASSOCIATION = re.compile(
    r"\b(GWAS|genome-wide association|QTLs?|quantitative trait|associat\w*|linked|linkage|"
    r"correlat\w*|candidate genes?|SNPs?|haplotypes?|alleles?|allelic|loci|locus|"
    r"co-?locali[sz]\w*|eQTLs?|differentially expressed|markers?)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])")
_NO_BREAK = re.compile(r"(?:\b(?:et al|e\.g|i\.e|Fig|Figs|cv|sp|spp|var|ca|approx|vs|No)\.|\b[A-Z]\.)$")
_TAG = re.compile(r"<[^>]+>")
_RELATION_RANK: dict[str, int] = {"causal-experimental": 0, "association": 1, "mention": 2}


class LiteratureDataMissingError(ValueError):
    """The bundle was built without the NCBI Gene tables."""


class Alias(BaseModel):
    """One name a gene goes by, where it came from, and whether it names only this gene."""

    text: str
    kind: AliasKind
    source: str
    specific: bool
    searchable: bool = True


class GeneAliases(BaseModel):
    """Every alias of one gene and its NCBI GeneIDs."""

    gene_id: str
    species: str
    ncbi_gene_ids: list[str] = Field(default_factory=list)
    aliases: list[Alias] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def search_terms(self, limit: int = MAX_QUERY_ALIASES) -> list[str]:
        """Return searchable alias texts, gene-specific names first."""
        ordered = sorted(
            (alias for alias in self.aliases if alias.searchable),
            key=lambda alias: (not alias.specific, _KIND_ORDER.index(alias.kind)),
        )
        return list(dict.fromkeys(alias.text for alias in ordered))[:limit]

    def specific_terms(self) -> list[str]:
        """Return the aliases that name only this gene."""
        return [alias.text for alias in self.aliases if alias.specific]


_KIND_ORDER: list[str] = [
    "symbol",
    "ncbi_symbol",
    "ncbi_synonym",
    "gene_id",
    "legacy_id",
    "ortholog_prefixed",
    "designation",
    "ortholog_symbol",
    "ncbi_locus",
]


class GenePublication(BaseModel):
    """A gene2pubmed link that is not a hub paper, with GO annotations citing the same PMID."""

    gene_id: str
    species: str
    ncbi_gene_id: str
    pmid: str
    genes_per_pmid: int
    go_terms: list[str] = Field(default_factory=list)
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the link as one literature fact."""
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="literature",
                subtype="gene2pubmed",
                value={
                    "pmid": self.pmid,
                    "ncbi_gene_id": self.ncbi_gene_id,
                    "genes_per_pmid": self.genes_per_pmid,
                    "go_terms": self.go_terms,
                },
                source_db="NCBI gene2pubmed",
                db_version=self.source_version,
                source_record=f"GeneID:{self.ncbi_gene_id}|PMID:{self.pmid}",
                primary_citation=f"PMID:{self.pmid}",
            )
        ]


class LiteratureHit(BaseModel):
    """One publication found for a gene, merged across the APIs that returned it."""

    gene_id: str
    pmid: str
    title: str
    pmcid: str | None = None
    doi: str | None = None
    year: int | None = None
    journal: str | None = None
    open_access: bool = False
    found_by: list[Api] = Field(default_factory=list)
    gene_normalized: bool = False
    """PubTator3 tagged the gene's own NCBI GeneID in this paper."""
    trait_in_title: bool = False
    metadata_source: Api = "europe_pmc"
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def evidence(self) -> list[EvidenceItem]:
        """Return the hit with its verbatim title as the quote."""
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="literature",
                subtype="search_hit",
                value={
                    "pmid": self.pmid,
                    "pmcid": self.pmcid,
                    "doi": self.doi,
                    "year": self.year,
                    "journal": self.journal,
                    "found_by": self.found_by,
                    "gene_normalized": self.gene_normalized,
                    "trait_in_title": self.trait_in_title,
                },
                source_db=SOURCES[self.metadata_source]["name"],
                db_version=f"retrieved {self.retrieved_at.date().isoformat()}",
                source_record=f"PMID:{self.pmid}",
                primary_citation=_citation(self.pmid, self.doi),
                quote=self.title,
                retrieved_at=self.retrieved_at,
            )
        ]


class LiteratureSearch(BaseModel):
    """The hits for one gene and how they were found."""

    gene_id: str
    hits: list[LiteratureHit] = Field(default_factory=list)
    queries: dict[str, str] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)


class Passage(BaseModel):
    """A verbatim sentence linking a gene alias to the trait, with its relation class."""

    gene_id: str
    pmid: str
    pmcid: str | None = None
    doi: str | None = None
    title: str | None = None
    section: str
    offset: int
    quote: str
    relation: Relation
    alias: str
    trait_term: str
    species_scope: Literal["sentence", "document"]
    gene_normalized: bool = False
    text_source: Api
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def evidence(self) -> list[EvidenceItem]:
        """Return the passage as one literature fact quoting the sentence verbatim."""
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="literature",
                subtype=f"passage:{self.relation}",
                value={
                    "pmid": self.pmid,
                    "pmcid": self.pmcid,
                    "doi": self.doi,
                    "title": self.title,
                    "section": self.section,
                    "relation": self.relation,
                    "alias": self.alias,
                    "trait_term": self.trait_term,
                    "species_scope": self.species_scope,
                    "gene_normalized": self.gene_normalized,
                },
                source_db=SOURCES[self.text_source]["name"],
                db_version=f"retrieved {self.retrieved_at.date().isoformat()}",
                source_record=f"PMID:{self.pmid}|{self.section}|{self.offset}",
                primary_citation=_citation(self.pmid, self.doi),
                quote=self.quote,
                retrieved_at=self.retrieved_at,
            )
        ]


def has_ncbi_tables(bundle: Bundle) -> bool:
    """Return whether the bundle carries the NCBI Gene tables."""
    rows = bundle.rows_raw(
        f"SELECT count(*) FROM information_schema.tables WHERE table_name IN ({', '.join('?' for _ in _NCBI_TABLES)})",
        list(_NCBI_TABLES),
    )
    return bool(rows) and int(rows[0][0]) == len(_NCBI_TABLES)


def species_terms(bundle: Bundle) -> list[str]:
    """Return the names a paper uses for the bundle species."""
    registry = registry_of(bundle)
    common = registry.common_name
    return list(dict.fromkeys([registry.scientific_name, common, f"{common}s"]))


def gene_aliases(bundle: Bundle, gene_ids: list[str]) -> list[GeneAliases]:
    """Collect gene ids, legacy ids, curated and NCBI symbols, and Arabidopsis ortholog symbols per gene.

    Ortholog symbols and NCBI designations can name other genes, so they are
    marked non-specific; ``ortholog_prefixed`` adds the species prefix to an
    Arabidopsis symbol (``GmWRKY6``) as a search heuristic only.
    """
    registry = registry_of(bundle)
    canonical = registry.canonical_assembly
    wanted = list(dict.fromkeys(gene for gene in gene_ids if gene))
    if not wanted:
        return []
    ncbi = has_ncbi_tables(bundle)
    marks = ", ".join("?" for _ in wanted)
    found: dict[str, GeneAliases] = {}
    for gene in wanted:
        is_agi = bool(re.match(r"^AT[1-5CM]G\d{5}$", gene, re.IGNORECASE))
        found[gene] = GeneAliases(gene_id=gene, species="arabidopsis" if is_agi else registry.species)
        _add(found[gene], gene, "gene_id", "registry", True)
        if not ncbi:
            found[gene].notes.append("bundle has no NCBI Gene tables; rebuild with the ncbi_gene source")
    for namespace in registry.id_namespaces:
        if namespace.id == "ensembl":
            for gene in wanted:
                match = re.match(r"^Glyma\.(\d{2}G\d{6})$", gene)
                if match:
                    _add(found[gene], f"GLYMA_{match.group(1)}", "gene_id", "Ensembl Plants", True)
    for gene, legacy in bundle.rows_raw(
        f"SELECT from_id, to_id FROM id_map WHERE assembly = ? AND from_id IN ({marks}) "
        "AND relation IN ('ancestor', 'synonym') AND to_assembly = 'Wm82.a1.v1'",
        [canonical, *wanted],
    ):
        _add(found[str(gene)], str(legacy), "legacy_id", "LIS", True)
    for gene, symbols in bundle.rows_raw(
        f"SELECT gene_id, symbols FROM known_genes WHERE assembly = ? AND gene_id IN ({marks})",
        [canonical, *wanted],
    ):
        for symbol in symbols or []:
            _add(found[str(gene)], str(symbol), "symbol", "LIS gene_functions", True)
    for gene, symbol in bundle.rows_raw(
        f"SELECT gene_id, value FROM annotation WHERE kind = 'symbol' AND gene_id IN ({marks})",
        wanted,
    ):
        _add(found[str(gene)], str(symbol), "symbol", "TAIR", True)
    if ncbi:
        for row in bundle.rows(
            f"SELECT gene_id, ncbi_gene_id, symbol, synonyms, designations, description FROM ncbi_genes "
            f"WHERE gene_id IN ({marks}) ORDER BY ncbi_gene_id",
            wanted,
        ):
            entry = found[str(row["gene_id"])]
            entry.ncbi_gene_ids = list(dict.fromkeys([*entry.ncbi_gene_ids, str(row["ncbi_gene_id"])]))
            symbol = str(row["symbol"])
            if _LOC_SYMBOL.match(symbol):
                _add(entry, symbol, "ncbi_locus", "NCBI Gene", True, searchable=False)
            else:
                _add(entry, symbol, "ncbi_symbol", "NCBI Gene", _prefixed(symbol, registry.symbol_prefixes))
            for synonym in row["synonyms"] or []:
                _add(entry, str(synonym), "ncbi_synonym", "NCBI Gene", _prefixed(str(synonym), registry.symbol_prefixes))
            for designation in row["designations"] or []:
                _add(entry, str(designation), "designation", "NCBI Gene", False)
    soy = [gene for gene in wanted if found[gene].species == registry.species]
    prefix = registry.symbol_prefixes[0] if registry.symbol_prefixes else ""
    calls = [call for call in orthology.get_orthologs(bundle, soy, ["arabidopsis"]) if call.confidence != "low"]
    targets = sorted({call.target_gene_id for call in calls})
    target_symbols: dict[str, list[str]] = {}
    if targets:
        for agi, symbol in bundle.rows_raw(
            f"SELECT gene_id, value FROM annotation WHERE kind = 'symbol' AND gene_id IN ({', '.join('?' for _ in targets)}) "
            "ORDER BY length(value), value",
            targets,
        ):
            target_symbols.setdefault(str(agi), []).append(str(symbol))
    for call in calls:
        for symbol in target_symbols.get(call.target_gene_id, [])[:3]:
            _add(found[call.gene_id], symbol, "ortholog_symbol", f"TAIR via {call.target_gene_id} ({call.confidence})", False)
            bare = re.sub(r"^At(?=[A-Z])|^AT(?=[A-Z]{2,})", "", symbol)
            if prefix and bare == symbol:
                _add(found[call.gene_id], f"{prefix}{symbol}", "ortholog_prefixed", f"heuristic from {symbol}", False)
    for gene in soy:
        if not any(alias.kind == "ortholog_symbol" for alias in found[gene].aliases):
            found[gene].notes.append("no medium/high-confidence Arabidopsis ortholog with a symbol")
    return [found[gene] for gene in wanted]


def gene_publications(
    bundle: Bundle,
    gene_ids: list[str],
    max_genes_per_pmid: int = HUB_PMID_GENES,
) -> tuple[list[GenePublication], dict[str, int]]:
    """Return gene2pubmed links without hub papers, and how many hub links were dropped per gene.

    Raises:
        LiteratureDataMissingError: when the bundle has no NCBI Gene tables.
    """
    if not has_ncbi_tables(bundle):
        raise LiteratureDataMissingError("the bundle has no NCBI Gene tables; rebuild it with the ncbi_gene source")
    wanted = list(dict.fromkeys(gene for gene in gene_ids if gene))
    if not wanted:
        return [], {}
    marks = ", ".join("?" for _ in wanted)
    rows = bundle.rows(
        f"""
        SELECT p.gene_id, p.species, p.ncbi_gene_id, p.pmid, p.genes_per_pmid, p.source_version,
               list(DISTINCT g.go_id || ' ' || coalesce(g.go_term, '') || ' (' || g.evidence_code || ')')
                   FILTER (WHERE g.go_id IS NOT NULL) AS go_terms
        FROM ncbi_gene_pubmed AS p
        LEFT JOIN ncbi_gene_go AS g ON g.ncbi_gene_id = p.ncbi_gene_id AND list_contains(g.pmids, p.pmid)
        WHERE p.gene_id IN ({marks})
        GROUP BY ALL
        """,
        wanted,
    )
    kept: list[GenePublication] = []
    dropped: Counter[str] = Counter()
    for row in rows:
        if int(row["genes_per_pmid"]) > max_genes_per_pmid:
            dropped[str(row["gene_id"])] += 1
            continue
        kept.append(
            GenePublication(
                gene_id=str(row["gene_id"]),
                species=str(row["species"]),
                ncbi_gene_id=str(row["ncbi_gene_id"]),
                pmid=str(row["pmid"]),
                genes_per_pmid=int(row["genes_per_pmid"]),
                go_terms=sorted(str(term) for term in row["go_terms"] or []),
                source_version=str(row["source_version"]),
            )
        )
    order = {gene: index for index, gene in enumerate(wanted)}
    kept.sort(key=lambda row: (order[row.gene_id], row.genes_per_pmid, row.pmid))
    return kept, dict(dropped)


def europepmc_query(
    aliases: Iterable[str],
    species: Iterable[str],
    traits: Iterable[str],
    specific: Iterable[str] = (),
) -> str:
    """Return ``(aliases) AND (species) AND (traits)`` in Europe PMC syntax.

    Species and trait terms must appear in the title or abstract. Aliases
    that name only this gene (``specific``) may appear anywhere in the
    text, including open-access full text and supplements; other aliases
    must appear in the title or abstract.
    """
    anywhere = {term.casefold() for term in specific}
    groups = _groups(aliases, species, traits)
    rendered = [
        " OR ".join(_quote(term) if index == 0 and term.casefold() in anywhere else f"TITLE_ABS:{_quote(term)}" for term in group)
        for index, group in enumerate(groups)
    ]
    return " AND ".join(f"({group})" for group in rendered)


def pubmed_query(aliases: Iterable[str], species: Iterable[str], traits: Iterable[str]) -> str:
    """Return ``(aliases) AND (species) AND (traits)`` in PubMed syntax over titles and abstracts."""
    return " AND ".join(
        f"({' OR '.join(_quote(term) + '[tiab]' for term in group)})" for group in _groups(aliases, species, traits)
    )


def pubtator_query(ncbi_gene_id: str, traits: Iterable[str]) -> str:
    """Return a PubTator3 search for papers tagging one NCBI GeneID together with a trait term."""
    terms = [_quote(term) for term in list(dict.fromkeys(traits))[:MAX_TRAIT_TERMS]]
    return f"@GENE_{ncbi_gene_id} AND ({' OR '.join(terms)})" if terms else f"@GENE_{ncbi_gene_id}"


@dataclass
class Fetched:
    """A response body with the time it was first retrieved."""

    data: Any
    fetched_at: datetime
    cached: bool


class RetryableStatusError(httpx.HTTPStatusError):
    """A 429 or 5xx response worth retrying."""


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, RetryableStatusError | httpx.TransportError)


@dataclass
class LiteratureClient:
    """Rate-limited, retrying, disk-cached HTTP access to Europe PMC, PubTator3 and E-utilities.

    One client should serve a whole process so parallel agents share each
    API's limiter. ``transport`` lets tests replay recorded responses.
    """

    cache_dir: Path | None = None
    ncbi_api_key: str | None = None
    email: str | None = None
    transport: httpx.AsyncBaseTransport | None = None
    rates: dict[Api, float] | None = None
    attempts: int = 4
    backoff: float = 0.5
    timeout: float = 30.0
    stats: Counter[str] = field(default_factory=Counter)

    def __post_init__(self) -> None:
        """Create the HTTP client, the per-API limiters and the cache."""
        rates = dict(RATE_LIMITS)
        if self.ncbi_api_key:
            rates["pubmed"] = NCBI_KEY_RATE
        rates.update(self.rates or {})
        self.limits = rates
        self._limiters = {api: AsyncLimiter(max(rate, 1.0), max(rate, 1.0) / rate) for api, rate in rates.items()}
        self._http = httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "AgriHub/0.1 (post-GWAS candidate-gene research)"},
        )
        self._cache: Any = None
        if self.cache_dir is not None:
            import diskcache

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache = diskcache.Cache(str(self.cache_dir))

    async def aclose(self) -> None:
        """Close the HTTP client and the cache."""
        await self._http.aclose()
        if self._cache is not None:
            self._cache.close()

    async def get(self, api: Api, url: str, params: dict[str, Any] | None = None, *, as_json: bool = True) -> Fetched:
        """GET ``url`` through the cache, the API's limiter and the retry policy."""
        query = {key: value for key, value in (params or {}).items() if value is not None}
        key = _cache_key(api, url, query)
        if self._cache is not None:
            entry = await asyncio.to_thread(self._cache.get, key)
            if entry is not None:
                self.stats[f"{api}:cache_hit"] += 1
                return Fetched(entry["data"], datetime.fromisoformat(entry["fetched_at"]), cached=True)
        if api == "pubmed":
            query = {**query, "tool": "agrihub", "email": self.email, "api_key": self.ncbi_api_key}
            query = {name: value for name, value in query.items() if value}
        response: httpx.Response | None = None
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.attempts),
            wait=_RetryAfterWait(self.backoff),
            retry=retry_if_exception(_retryable),
            reraise=True,
        ):
            with attempt:
                async with self._limiters[api]:
                    self.stats[f"{api}:request"] += 1
                    response = await self._http.get(url, params=query)
                if response.status_code == 429 or response.status_code >= 500:
                    self.stats[f"{api}:retryable_status"] += 1
                    raise RetryableStatusError(
                        f"{api} returned {response.status_code}", request=response.request, response=response
                    )
                response.raise_for_status()
        assert response is not None
        data = response.json() if as_json else response.text
        fetched_at = datetime.now(UTC)
        if self._cache is not None:
            await asyncio.to_thread(
                self._cache.set, key, {"data": data, "fetched_at": fetched_at.isoformat()}, CACHE_TTL_SECONDS
            )
        return Fetched(data, fetched_at, cached=False)


class _RetryAfterWait:
    """Wait ``Retry-After`` seconds when the server sends it (at most 30), else back off exponentially."""

    def __init__(self, backoff: float) -> None:
        self._exponential = wait_exponential(multiplier=backoff, max=8)

    def __call__(self, retry_state: Any) -> float:
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None else None
        if isinstance(exc, httpx.HTTPStatusError):
            header = exc.response.headers.get("Retry-After", "")
            if header.strip().isdigit():
                return min(float(header), 30.0)
        return float(self._exponential(retry_state))


_clients: dict[int, LiteratureClient] = {}


def default_client(cache_dir: Path | None = None) -> LiteratureClient:
    """Return the process client for the running event loop, configured from the environment."""
    loop = asyncio.get_running_loop()
    client = _clients.get(id(loop))
    if client is None or client._http.is_closed:
        client = LiteratureClient(
            cache_dir=cache_dir,
            ncbi_api_key=os.environ.get("NCBI_API_KEY") or None,
            email=os.environ.get("NCBI_EMAIL") or None,
        )
        _clients.clear()
        _clients[id(loop)] = client
    return client


async def search_literature(
    client: LiteratureClient,
    *,
    gene_id: str,
    aliases: list[str],
    species: list[str],
    traits: list[str],
    ncbi_gene_ids: list[str] | None = None,
    specific_aliases: list[str] | None = None,
    max_results: int = 10,
) -> LiteratureSearch:
    """Search Europe PMC, PubMed and PubTator3 for one gene, the species and the trait; merge by PMID.

    An API that fails is reported in ``errors`` and the others still count.
    Hits where PubTator3 tagged the gene's own GeneID, or whose title names
    the trait, rank first.
    """
    result = LiteratureSearch(gene_id=gene_id)
    hits: dict[str, LiteratureHit] = {}
    trait_terms = list(dict.fromkeys(traits))[:MAX_TRAIT_TERMS]
    alias_terms = list(dict.fromkeys(aliases))[:MAX_QUERY_ALIASES]
    if not alias_terms:
        result.errors["query"] = "no searchable aliases"
        return result
    result.queries["europe_pmc"] = europepmc_query(alias_terms, species, trait_terms, specific_aliases or ())
    result.queries["pubmed"] = pubmed_query(alias_terms, species, trait_terms)
    pubtator_ids = list(dict.fromkeys(ncbi_gene_ids or []))[:2]
    for ncbi in pubtator_ids:
        result.queries[f"pubtator3:{ncbi}"] = pubtator_query(ncbi, trait_terms)

    async def europe() -> None:
        fetched = await client.get(
            "europe_pmc",
            f"{EUROPE_PMC}/search",
            {"query": result.queries["europe_pmc"], "format": "json", "resultType": "lite", "pageSize": max_results},
        )
        records = ((fetched.data or {}).get("resultList") or {}).get("result") or []
        result.counts["europe_pmc"] = int((fetched.data or {}).get("hitCount") or 0)
        for record in records:
            if record.get("pmid") and record.get("title"):
                _merge(hits, gene_id, str(record["pmid"]), "europe_pmc", fetched, _europe_fields(record))

    async def pubmed() -> None:
        fetched = await client.get(
            "pubmed",
            f"{EUTILS}/esearch.fcgi",
            {"db": "pubmed", "term": result.queries["pubmed"], "retmode": "json", "retmax": max_results, "sort": "relevance"},
        )
        found = (fetched.data or {}).get("esearchresult") or {}
        warnings = found.get("warninglist") or {}
        unfound = {
            str(phrase).casefold().split("[")[0].strip('"')
            for phrase in [*(warnings.get("quotedphrasesnotfound") or []), *(warnings.get("phrasesignored") or [])]
        }
        if all(alias.casefold() in unfound for alias in alias_terms):
            result.counts["pubmed"] = 0
            return
        result.counts["pubmed"] = int(found.get("count") or 0)
        for pmid in found.get("idlist") or []:
            _merge(hits, gene_id, str(pmid), "pubmed", fetched, {})

    async def pubtator(ncbi: str) -> None:
        fetched = await client.get("pubtator3", f"{PUBTATOR}/search/", {"text": result.queries[f"pubtator3:{ncbi}"], "page": 1})
        data = fetched.data or {}
        result.counts[f"pubtator3:{ncbi}"] = int(data.get("count") or 0)
        for record in (data.get("results") or [])[:max_results]:
            pmid = str(record.get("pmid") or "")
            if pmid:
                fields = {
                    "title": record.get("title"),
                    "doi": record.get("doi"),
                    "journal": record.get("journal"),
                    "year": _year(record.get("date")),
                }
                _merge(hits, gene_id, pmid, "pubtator3", fetched, fields, normalized=True)

    jobs = {"europe_pmc": europe(), "pubmed": pubmed(), **{f"pubtator3:{ncbi}": pubtator(ncbi) for ncbi in pubtator_ids}}
    outcomes = await asyncio.gather(*jobs.values(), return_exceptions=True)
    for name, outcome in zip(jobs, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            result.errors[name] = _error_text(outcome)
    untitled = [pmid for pmid, hit in hits.items() if not hit.title]
    if untitled:
        try:
            await _fill_titles(client, hits, untitled)
        except Exception as exc:  # noqa: BLE001
            result.errors["pubmed:esummary"] = _error_text(exc)
    patterns = [_term_pattern(term) for term in trait_terms]
    ordered = []
    for hit in hits.values():
        if not hit.title:
            continue
        hit.trait_in_title = any(pattern.search(hit.title) for pattern in patterns)
        ordered.append(hit)
    ordered.sort(key=lambda hit: (not hit.gene_normalized, not hit.trait_in_title, -len(hit.found_by), -(hit.year or 0), hit.pmid))
    result.hits = ordered[:max_results]
    return result


async def extract_passages(
    client: LiteratureClient,
    *,
    gene_id: str,
    pmids: list[str],
    aliases: list[str],
    specific_aliases: list[str],
    species: list[str],
    traits: list[str],
    ncbi_gene_ids: list[str] | None = None,
    full_text: bool = True,
    max_per_pmid: int = 3,
) -> tuple[list[Passage], dict[str, str]]:
    """Return verbatim trait sentences about the gene from PubTator3 text, Europe PMC abstracts and OA full text.

    Returns the passages (deduplicated per PMID, best relation first) and
    per-PMID notes for papers that yielded none.
    """
    wanted = list(dict.fromkeys(str(pmid).strip() for pmid in pmids if str(pmid).strip()))
    documents: dict[str, _Document] = {}
    notes: dict[str, str] = {}
    if wanted:
        try:
            fetched = await client.get(
                "pubtator3",
                f"{PUBTATOR}/publications/export/biocjson",
                {"pmids": ",".join(wanted), "full": "true" if full_text else None},
            )
            for document in _pubtator_documents(fetched):
                documents[document.pmid] = document
        except Exception as exc:  # noqa: BLE001
            notes["pubtator3"] = _error_text(exc)
    missing = [pmid for pmid in wanted if pmid not in documents or not documents[pmid].has_abstract]
    if missing:
        try:
            for document in await _europe_abstracts(client, missing, full_text):
                documents[document.pmid] = _combine(documents.get(document.pmid), document)
        except Exception as exc:  # noqa: BLE001
            notes["europe_pmc"] = _error_text(exc)
    alias_patterns = [(alias, _alias_pattern(alias)) for alias in dict.fromkeys(aliases) if alias]
    specific = {alias.casefold() for alias in specific_aliases}
    trait_patterns = [(term, _term_pattern(term)) for term in dict.fromkeys(traits) if term]
    species_patterns = [_term_pattern(term) for term in species if term]
    gene_ids = set(ncbi_gene_ids or [])
    passages: list[Passage] = []
    for pmid in wanted:
        text = documents.get(pmid)
        if text is None:
            notes.setdefault(pmid, "no text retrieved")
            continue
        found = _scan(text, gene_id, alias_patterns, specific, trait_patterns, species_patterns, gene_ids)
        if not found:
            notes.setdefault(pmid, "no sentence with an alias and a trait term")
        found.sort(key=lambda item: (_RELATION_RANK[item.relation], item.species_scope != "sentence", item.offset))
        passages.extend(found[:max_per_pmid])
    return passages, notes


def classify_relation(sentence: str) -> Relation:
    """Classify a sentence as causal-experimental, association or mention from fixed cue words."""
    if _CAUSAL.search(sentence):
        return "causal-experimental"
    if _ASSOCIATION.search(sentence):
        return "association"
    return "mention"


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` spans of the sentences in ``text`` without trailing whitespace."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        if _NO_BREAK.search(text[start : match.start()]):
            continue
        spans.append((start, match.start()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return [(begin, end) for begin, end in ((begin, len(text[:end].rstrip())) for begin, end in spans) if end > begin]


@dataclass
class _Section:
    name: str
    offset: int
    text: str
    genes: list[tuple[int, int, set[str]]] = field(default_factory=list)


@dataclass
class _Document:
    pmid: str
    source: Api
    fetched_at: datetime
    title: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    sections: list[_Section] = field(default_factory=list)

    @property
    def has_abstract(self) -> bool:
        return any(section.name.upper() == "ABSTRACT" and section.text.strip() for section in self.sections)

    @property
    def front(self) -> str:
        return " ".join(section.text for section in self.sections if section.name.upper() in {"TITLE", "ABSTRACT"})


def _scan(
    document: _Document,
    gene_id: str,
    aliases: list[tuple[str, re.Pattern[str]]],
    specific: set[str],
    traits: list[tuple[str, re.Pattern[str]]],
    species: list[re.Pattern[str]],
    ncbi_gene_ids: set[str],
) -> list[Passage]:
    species_in_front = any(pattern.search(document.front) for pattern in species)
    found: list[Passage] = []
    for section in document.sections:
        for start, end in split_sentences(section.text):
            sentence = section.text[start:end]
            trait = next((term for term, pattern in traits if pattern.search(sentence)), None)
            if trait is None:
                continue
            absolute = (section.offset + start, section.offset + end)
            normalized = any(
                ids & ncbi_gene_ids and begin < absolute[1] and stop > absolute[0] for begin, stop, ids in section.genes
            )
            alias = next((name for name, pattern in aliases if pattern.search(sentence)), None)
            if alias is None and normalized:
                alias = f"GeneID:{sorted(ncbi_gene_ids)[0]}"
            if alias is None:
                continue
            if any(pattern.search(sentence) for pattern in species):
                scope: Literal["sentence", "document"] = "sentence"
            elif species_in_front and (normalized or alias.casefold() in specific):
                scope = "document"
            else:
                continue
            found.append(
                Passage(
                    gene_id=gene_id,
                    pmid=document.pmid,
                    pmcid=document.pmcid,
                    doi=document.doi,
                    title=document.title,
                    section=section.name,
                    offset=absolute[0],
                    quote=sentence,
                    relation=classify_relation(sentence),
                    alias=alias,
                    trait_term=trait,
                    species_scope=scope,
                    gene_normalized=normalized,
                    text_source=document.source,
                    retrieved_at=document.fetched_at,
                )
            )
    return found


def _pubtator_documents(fetched: Fetched) -> list[_Document]:
    data = fetched.data or {}
    records = data.get("PubTator3") if isinstance(data, dict) else data
    documents = []
    for record in records or []:
        pmid = str(record.get("pmid") or record.get("id") or "").split("|")[0]
        if not pmid:
            continue
        document = _Document(pmid=pmid, source="pubtator3", fetched_at=fetched.fetched_at, pmcid=record.get("pmcid"))
        for passage in record.get("passages") or []:
            infons = passage.get("infons") or {}
            text = passage.get("text") or ""
            if not text.strip():
                continue
            name = str(infons.get("section_type") or infons.get("type") or "TEXT").upper()
            if name == "FRONT":
                name = "TITLE"
            if name == "TITLE" and document.title is None:
                document.title = text
            if name in {"REF", "TABLE"}:
                continue
            section = _Section(name=name, offset=int(passage.get("offset") or 0), text=text)
            for annotation in passage.get("annotations") or []:
                info = annotation.get("infons") or {}
                if str(info.get("type")) != "Gene":
                    continue
                ids = {part.strip() for part in str(info.get("identifier") or "").replace(",", ";").split(";") if part.strip()}
                for location in annotation.get("locations") or []:
                    begin = int(location.get("offset") or 0)
                    section.genes.append((begin, begin + int(location.get("length") or 0), ids))
            document.sections.append(section)
        documents.append(document)
    return documents


async def _europe_abstracts(client: LiteratureClient, pmids: list[str], full_text: bool) -> list[_Document]:
    query = f"({' OR '.join(f'EXT_ID:{pmid}' for pmid in pmids)}) AND SRC:MED"
    fetched = await client.get(
        "europe_pmc",
        f"{EUROPE_PMC}/search",
        {"query": query, "format": "json", "resultType": "core", "pageSize": len(pmids)},
    )
    documents = []
    for record in ((fetched.data or {}).get("resultList") or {}).get("result") or []:
        pmid = str(record.get("pmid") or "")
        if not pmid:
            continue
        title = _strip_tags(record.get("title") or "")
        document = _Document(
            pmid=pmid,
            source="europe_pmc",
            fetched_at=fetched.fetched_at,
            title=title or None,
            pmcid=record.get("pmcid"),
            doi=record.get("doi"),
        )
        if title:
            document.sections.append(_Section("TITLE", 0, title))
        abstract = _strip_tags(record.get("abstractText") or "")
        if abstract:
            document.sections.append(_Section("ABSTRACT", len(title) + 1, abstract))
        if full_text and document.pmcid and record.get("isOpenAccess") == "Y":
            try:
                text = await client.get("europe_pmc", f"{EUROPE_PMC}/{document.pmcid}/fullTextXML", as_json=False)
                document.sections.extend(_full_text_sections(str(text.data), len(title) + len(abstract) + 2))
            except httpx.HTTPError:
                pass
        documents.append(document)
    return documents


def _full_text_sections(xml_text: str, offset: int) -> list[_Section]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    sections = []
    body = root.find(".//body")
    if body is None:
        return []
    for sec in body.iter("sec"):
        title = sec.findtext("title") or "BODY"
        for paragraph in sec.findall("p"):
            text = " ".join("".join(paragraph.itertext()).split())
            if text:
                sections.append(_Section(title.strip().upper()[:40] or "BODY", offset, text))
                offset += len(text) + 1
    return sections


def _combine(first: "_Document | None", second: _Document) -> _Document:
    if first is None:
        return second
    names = {section.name for section in first.sections}
    first.sections.extend(section for section in second.sections if section.name not in names)
    first.doi = first.doi or second.doi
    first.pmcid = first.pmcid or second.pmcid
    first.title = first.title or second.title
    return first


async def _fill_titles(client: LiteratureClient, hits: dict[str, LiteratureHit], pmids: list[str]) -> None:
    fetched = await client.get("pubmed", f"{EUTILS}/esummary.fcgi", {"db": "pubmed", "id": ",".join(pmids), "retmode": "json"})
    summary = (fetched.data or {}).get("result") or {}
    for pmid in pmids:
        record = summary.get(pmid) or {}
        title = record.get("title")
        if not title:
            continue
        hit = hits[pmid]
        hit.title = str(title)
        hit.journal = hit.journal or record.get("source")
        hit.year = hit.year or _year(record.get("pubdate"))
        hit.metadata_source = "pubmed"
        for article in record.get("articleids") or []:
            if article.get("idtype") == "doi" and not hit.doi:
                hit.doi = article.get("value")
            if article.get("idtype") == "pmc" and not hit.pmcid:
                hit.pmcid = article.get("value")


def _merge(
    hits: dict[str, LiteratureHit],
    gene_id: str,
    pmid: str,
    api: Api,
    fetched: Fetched,
    fields: dict[str, Any],
    *,
    normalized: bool = False,
) -> None:
    hit = hits.get(pmid)
    values = {key: value for key, value in fields.items() if value not in (None, "")}
    if hit is None:
        hit = LiteratureHit(gene_id=gene_id, pmid=pmid, title="", retrieved_at=fetched.fetched_at, metadata_source=api)
        hits[pmid] = hit
    if values.get("title") and not hit.title:
        hit.title = _strip_tags(str(values["title"]))
        hit.metadata_source = api
    for key in ("pmcid", "doi", "year", "journal"):
        if getattr(hit, key) in (None, "") and values.get(key) is not None:
            setattr(hit, key, values[key])
    hit.open_access = hit.open_access or bool(values.get("open_access"))
    hit.gene_normalized = hit.gene_normalized or normalized
    if api not in hit.found_by:
        hit.found_by.append(api)


def _europe_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": record.get("title"),
        "pmcid": record.get("pmcid"),
        "doi": record.get("doi"),
        "year": _year(record.get("pubYear")),
        "journal": record.get("journalTitle"),
        "open_access": record.get("isOpenAccess") == "Y",
    }


def _add(entry: GeneAliases, text: str, kind: AliasKind, source: str, specific: bool, *, searchable: bool = True) -> None:
    text = text.strip()
    if len(text) < 2 or any(alias.text.casefold() == text.casefold() for alias in entry.aliases):
        return
    entry.aliases.append(
        Alias(text=text, kind=kind, source=source, specific=specific and kind in _SPECIFIC_KINDS, searchable=searchable and kind not in _UNSEARCHABLE_KINDS)
    )


def _prefixed(symbol: str, prefixes: list[str]) -> bool:
    """Return whether a symbol carries the species prefix (``GmFT2a``); bare family names (``GH3``) name many genes."""
    return any(symbol.startswith(prefix) and len(symbol) > len(prefix) for prefix in prefixes)


def _groups(aliases: Iterable[str], species: Iterable[str], traits: Iterable[str]) -> list[list[str]]:
    groups = [
        list(dict.fromkeys(aliases))[:MAX_QUERY_ALIASES],
        list(dict.fromkeys(species)),
        list(dict.fromkeys(traits))[:MAX_TRAIT_TERMS],
    ]
    return [group for group in groups if group]


def _quote(term: str) -> str:
    cleaned = term.replace('"', " ").strip()
    return f'"{cleaned}"'


def _alias_pattern(alias: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.IGNORECASE)


def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z]){re.escape(term)}", re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return " ".join(_TAG.sub(" ", html.unescape(text)).split())


def _year(value: Any) -> int | None:
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def _citation(pmid: str, doi: str | None) -> str:
    return f"PMID:{pmid}" + (f"; doi:{doi}" if doi else "")


def _cache_key(api: str, url: str, params: dict[str, Any]) -> str:
    payload = json.dumps([api, url, sorted((key, str(value)) for key, value in params.items())], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from {exc.request.url.host}"
    return f"{type(exc).__name__}: {exc}"[:200]
