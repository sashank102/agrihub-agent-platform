"""Literature tools for the literature specialist.

``gene_aliases`` and ``gene_publications`` read the bundle's NCBI Gene
tables. ``search_literature`` and ``extract_passages`` call Europe PMC,
PubTator3 and PubMed through the shared rate-limited, cached client; each
call announces the APIs it used with ``source.discovered``. Search hits and
passages are stored as literature evidence (PMID, verbatim quote, retrieval
date). ``web_search`` returns web leads only and stores no evidence.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import BaseModel

from agrihub import events
from agrihub.tools.bundle_tools import _EXPECTED_ERRORS, _bundle, _clip, _respond, _run
from agrihub_data.paths import data_root
from agrihub_data.query import literature, traits

MAX_GENES_PER_SEARCH = 8
MAX_PMIDS_PER_EXTRACT = 10
MAX_WEB_QUERIES = 3


def _client() -> literature.LiteratureClient:
    return literature.default_client(data_root() / "_cache" / "literature")


def _trait_terms(species: str, trait: str, given: list[str] | None) -> list[str]:
    if given:
        return [term for term in dict.fromkeys(given) if len(term.strip()) >= 3]
    bundle = _bundle(species)
    profile = traits.map_trait(trait, bundle.species, bundle)
    terms = [trait, *profile.keywords]
    return [term for term in dict.fromkeys(term.strip() for term in terms) if len(term) >= 4][: literature.MAX_TRAIT_TERMS]


def _announce(apis: set[str]) -> None:
    today = datetime.now(UTC).date().isoformat()
    for api in sorted(apis):
        source = literature.SOURCES[api]  # type: ignore[index]
        events.source_discovered(
            source_id=api,
            name=source["name"],
            version=f"live API, retrieved {today}",
            url=source["url"],
            license=source["license"],
        )


@tool(response_format="content_and_artifact", parse_docstring=True)
async def gene_aliases(
    gene_ids: list[str],
    config: RunnableConfig,
    species: str = "soybean",
) -> tuple[str, dict[str, Any]]:
    """List every alias of each gene for literature search: gene ids, legacy ids, curated and NCBI symbols, and Arabidopsis ortholog symbols.

    Aliases marked specific name only this gene; ortholog symbols and NCBI
    designations can name other genes, so papers matching them need the
    species in the same sentence.

    Args:
        gene_ids: Canonical-assembly gene ids (or AGIs).
        species: Registered species, e.g. soybean.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        rows = literature.gene_aliases(bundle, gene_ids)
        return _respond(
            config,
            "gene_aliases",
            f"gene_aliases {len(rows)} genes",
            rows,
            _alias_line,
            evidence=False,
        )

    return await _run(work)


def _alias_line(row: literature.GeneAliases) -> str:
    specific = [alias.text for alias in row.aliases if alias.specific]
    other = [f"{alias.text} ({alias.kind})" for alias in row.aliases if not alias.specific]
    return (
        f"{row.gene_id}"
        + (f" ncbi={','.join(row.ncbi_gene_ids)}" if row.ncbi_gene_ids else " ncbi=none")
        + f" | specific: {', '.join(specific) or '-'}"
        + f" | non-specific: {_clip(', '.join(other), 200) or '-'}"
        + (f" | {'; '.join(row.notes)}" if row.notes else "")
    )


@tool(response_format="content_and_artifact", parse_docstring=True)
async def gene_publications(
    gene_ids: list[str],
    config: RunnableConfig,
    species: str = "soybean",
    max_genes_per_pmid: int = literature.HUB_PMID_GENES,
) -> tuple[str, dict[str, Any]]:
    """List NCBI gene2pubmed papers linked to each gene, dropping hub papers linked to more than max_genes_per_pmid genes.

    A link says a paper is about the gene; it does not say what the paper
    found. Read it with extract_passages before claiming support.

    Args:
        gene_ids: Canonical-assembly gene ids (or AGIs).
        species: Registered species, e.g. soybean.
        max_genes_per_pmid: Hub threshold; genome and atlas papers exceed it.
    """

    def work() -> tuple[str, dict[str, Any]]:
        bundle = _bundle(species)
        rows, dropped = literature.gene_publications(bundle, gene_ids, max_genes_per_pmid)
        linked = {row.gene_id for row in rows}
        notes = [f"hub papers dropped (> {max_genes_per_pmid} genes): " + ", ".join(f"{gene} {count}" for gene, count in dropped.items())] if dropped else []
        unlinked = [gene for gene in gene_ids if gene not in linked]
        if unlinked:
            notes.append(f"no non-hub gene2pubmed links: {', '.join(unlinked)}")
        return _respond(
            config,
            "gene_publications",
            f"gene_publications {len(rows)} links for {len(linked)} of {len(gene_ids)} genes",
            rows,
            lambda row: (
                f"{row.gene_id} PMID:{row.pmid} genes_per_pmid={row.genes_per_pmid}"
                + (f" GO: {_clip('; '.join(row.go_terms), 120)}" if row.go_terms else "")
            ),
            evidence=True,
            notes=notes,
        )

    return await _run(work)


class _SearchRow(BaseModel):
    """A hit with the gene it was searched for."""

    hit: literature.LiteratureHit

    def evidence(self) -> list[Any]:
        """Return the hit's evidence."""
        return self.hit.evidence()


@tool(response_format="content_and_artifact", parse_docstring=True)
async def search_literature(
    gene_ids: list[str],
    trait: str,
    config: RunnableConfig,
    species: str = "soybean",
    extra_aliases: list[str] | None = None,
    trait_terms: list[str] | None = None,
    max_results_per_gene: int = 8,
) -> tuple[str, dict[str, Any]]:
    """Search Europe PMC, PubMed and PubTator3 for papers naming each gene (any alias) with the species and the trait.

    Aliases come from gene_aliases; trait terms default to the trait and its
    profile keywords. A hit is a lead: its title is stored as a quote, but
    support needs a passage from extract_passages.

    Args:
        gene_ids: Canonical-assembly gene ids, at most 8 per call.
        trait: Trait text, e.g. plant height.
        species: Registered species, e.g. soybean.
        extra_aliases: More names to search for every gene (e.g. a symbol from a paper).
        trait_terms: Override the trait terms.
        max_results_per_gene: Hits kept per gene.
    """
    genes = list(dict.fromkeys(gene_ids))[:MAX_GENES_PER_SEARCH]
    try:
        bundle = await asyncio.to_thread(_bundle, species)
        aliases = await asyncio.to_thread(literature.gene_aliases, bundle, genes)
        terms = await asyncio.to_thread(_trait_terms, species, trait, trait_terms)
    except _EXPECTED_ERRORS as exc:
        raise ToolException(str(exc)) from exc
    species_names = literature.species_terms(bundle)
    client = _client()
    results = await asyncio.gather(
        *(
            literature.search_literature(
                client,
                gene_id=entry.gene_id,
                aliases=[*entry.search_terms(), *(extra_aliases or [])],
                species=species_names,
                traits=terms,
                ncbi_gene_ids=entry.ncbi_gene_ids,
                specific_aliases=entry.specific_terms(),
                max_results=max_results_per_gene,
            )
            for entry in aliases
        )
    )
    _announce({api.split(":")[0] for result in results for api in result.counts})
    rows = [_SearchRow(hit=hit) for result in results for hit in result.hits]
    notes = [f"trait terms: {', '.join(terms)}"]
    for result in results:
        counts = ", ".join(f"{api}={count}" for api, count in result.counts.items())
        errors = "; ".join(f"{api}: {error}" for api, error in result.errors.items())
        notes.append(f"{result.gene_id}: {len(result.hits)} kept; totals {counts or 'none'}" + (f"; errors {errors}" if errors else ""))

    def work() -> tuple[str, dict[str, Any]]:
        return _respond(
            config,
            "search_literature",
            f"search_literature '{trait}': {len(rows)} hits for {len(genes)} genes",
            rows,
            _hit_line,
            evidence=True,
            notes=notes,
        )

    return await asyncio.to_thread(work)


def _hit_line(row: _SearchRow) -> str:
    hit = row.hit
    flags = [flag for flag, on in (("gene-tagged", hit.gene_normalized), ("trait-in-title", hit.trait_in_title), ("OA", hit.open_access)) if on]
    return (
        f"{hit.gene_id} PMID:{hit.pmid}"
        + (f" {hit.year}" if hit.year else "")
        + f" [{'+'.join(hit.found_by)}]"
        + (f" {' '.join(flags)}" if flags else "")
        + f" \u00ab{_clip(hit.title, 160)}\u00bb"
    )


@tool(response_format="content_and_artifact", parse_docstring=True)
async def extract_passages(
    gene_id: str,
    pmids: list[str],
    trait: str,
    config: RunnableConfig,
    species: str = "soybean",
    extra_aliases: list[str] | None = None,
    trait_terms: list[str] | None = None,
    full_text: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Extract verbatim sentences where an alias of the gene and a trait term co-occur, classified causal-experimental, association or mention.

    Text comes from PubTator3 (abstracts, PMC open-access full text, gene
    tagging) and Europe PMC abstracts. The species must appear in the
    sentence, or in the title/abstract when the alias names only this gene.

    Args:
        gene_id: One canonical-assembly gene id.
        pmids: PubMed ids to read, at most 10, e.g. from search_literature or gene_publications.
        trait: Trait text, e.g. plant height.
        species: Registered species, e.g. soybean.
        extra_aliases: More names of this gene to look for.
        trait_terms: Override the trait terms.
        full_text: Also read open-access full text.
    """
    wanted = list(dict.fromkeys(pmids))[:MAX_PMIDS_PER_EXTRACT]
    try:
        bundle = await asyncio.to_thread(_bundle, species)
        entries = await asyncio.to_thread(literature.gene_aliases, bundle, [gene_id])
        terms = await asyncio.to_thread(_trait_terms, species, trait, trait_terms)
    except _EXPECTED_ERRORS as exc:
        raise ToolException(str(exc)) from exc
    entry = entries[0]
    passages, notes = await literature.extract_passages(
        _client(),
        gene_id=gene_id,
        pmids=wanted,
        aliases=[*entry.search_terms(limit=30), *(extra_aliases or [])],
        specific_aliases=entry.specific_terms(),
        species=literature.species_terms(bundle),
        traits=terms,
        ncbi_gene_ids=entry.ncbi_gene_ids,
        full_text=full_text,
    )
    _announce({passage.text_source for passage in passages} | {"pubtator3"})
    relations = {relation: sum(1 for passage in passages if passage.relation == relation) for relation in ("causal-experimental", "association", "mention")}

    def work() -> tuple[str, dict[str, Any]]:
        return _respond(
            config,
            "extract_passages",
            f"extract_passages {gene_id}: {len(passages)} passages from {len({p.pmid for p in passages})} of {len(wanted)} papers "
            + ", ".join(f"{name}={count}" for name, count in relations.items()),
            passages,
            lambda passage: (
                f"PMID:{passage.pmid} [{passage.relation}|{passage.section}|species:{passage.species_scope}"
                + ("|gene-tagged" if passage.gene_normalized else "")
                + f"] alias={passage.alias} trait={passage.trait_term} \u00ab{_clip(passage.quote, 300)}\u00bb"
            ),
            evidence=True,
            notes=[f"PMID:{pmid}: {note}" if pmid.isdigit() else f"{pmid}: {note}" for pmid, note in notes.items()],
        )

    return await asyncio.to_thread(work)


@tool(parse_docstring=True)
async def web_search(queries: list[str]) -> str:
    """Search the public web (DuckDuckGo) as a last resort when databases and literature APIs found nothing.

    Results are leads, never evidence: they have no evidence ids and cannot
    be cited in findings. Follow a lead with search_literature or
    extract_passages on its PMID.

    Args:
        queries: Up to 3 search queries.
    """
    from open_deep_research.utils import duckduckgo_search

    results = await duckduckgo_search.ainvoke({"queries": list(queries)[:MAX_WEB_QUERIES]})
    return "WEB LEADS ONLY (not evidence; no evidence ids):\n" + str(results)[:4_000]


LITERATURE_TOOLS: tuple[BaseTool, ...] = (gene_aliases, search_literature, extract_passages, gene_publications)
for _tool in (*LITERATURE_TOOLS, web_search):
    _tool.handle_tool_error = True
