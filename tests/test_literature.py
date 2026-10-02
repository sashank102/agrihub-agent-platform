"""Literature tools: NCBI tables in the bundle, query construction, rate limits, retries, caching and verbatim passages.

HTTP responses under tests/data/literature were recorded from Europe PMC,
PubMed E-utilities and PubTator3 on 2026-10-02 (trimmed to a few records)
and are replayed through ``httpx.MockTransport``; no test touches the network.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from agrihub_fixtures import FixtureBundle

from agrihub.evidence_store import EvidenceStore
from agrihub.tools import literature_tools
from agrihub_data.bundle import open_bundle
from agrihub_data.query import literature

RECORDED = Path(__file__).parent / "data" / "literature"
SPECIES = ["Glycine max", "soybean", "soybeans"]
TRAITS = ["plant height", "dwarf", "internode length"]


def _recorded(name: str) -> dict[str, Any]:
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


class Replay:
    """Serve recorded responses by API path and keep every request."""

    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.overrides = overrides or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for key, value in self.overrides.items():
            if key in path:
                return value(request) if callable(value) else httpx.Response(200, json=value)
        if path.endswith("/europepmc/webservices/rest/search"):
            name = "europepmc_core.json" if request.url.params.get("resultType") == "core" else "europepmc_search.json"
        elif path.endswith("esearch.fcgi"):
            name = "pubmed_esearch.json"
        elif path.endswith("esummary.fcgi"):
            name = "pubmed_esummary.json"
        elif path.endswith("/search/"):
            name = "pubtator_search.json"
        elif path.endswith("/export/biocjson"):
            name = "pubtator_export.json"
        else:
            return httpx.Response(404, json={"error": path})
        return httpx.Response(200, json=_recorded(name)["response"])


def _client(handler: Any, tmp_path: Path, **kwargs: Any) -> literature.LiteratureClient:
    return literature.LiteratureClient(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(handler), **kwargs)


def test_queries_combine_aliases_species_and_trait_terms():
    query = literature.europepmc_query(
        ["GmDW1", "DW1", "Glyma.18G092000"], ["Glycine max", "soybean"], ["plant height", 'dwarf "x"'], specific=["GmDW1", "Glyma.18G092000"]
    )
    assert query == (
        '("GmDW1" OR TITLE_ABS:"DW1" OR "Glyma.18G092000") AND (TITLE_ABS:"Glycine max" OR TITLE_ABS:"soybean") '
        'AND (TITLE_ABS:"plant height" OR TITLE_ABS:"dwarf  x")'
    )
    assert literature.pubmed_query(["GmDW1"], ["soybean"], ["dwarf"]) == '("GmDW1"[tiab]) AND ("soybean"[tiab]) AND ("dwarf"[tiab])'
    assert literature.pubtator_query("100800001", ["plant height"]) == '@GENE_100800001 AND ("plant height")'
    recorded = _recorded("europepmc_search.json")
    assert '"GH3"' in recorded["params"]["query"] and recorded["params"]["resultType"] == "lite"


def test_search_merges_the_three_apis_by_pmid(tmp_path: Path):
    replay = Replay()

    async def scenario() -> literature.LiteratureSearch:
        client = _client(replay, tmp_path)
        try:
            return await literature.search_literature(
                client,
                gene_id="Glyma.05G101300",
                aliases=["GH3", "Glyma.05G101300", "GmGH3.3"],
                species=SPECIES,
                traits=TRAITS,
                ncbi_gene_ids=["100811309"],
                max_results=10,
            )
        finally:
            await client.aclose()

    result = asyncio.run(scenario())
    by_pmid = {hit.pmid: hit for hit in result.hits}
    assert set(result.counts) == {"europe_pmc", "pubmed", "pubtator3:100811309"}
    assert not result.errors
    assert set(by_pmid["35955771"].found_by) == {"pubmed", "pubtator3"}
    assert by_pmid["35955771"].gene_normalized and by_pmid["35955771"].trait_in_title
    assert result.hits[0].pmid == "35955771"
    assert set(by_pmid["39435023"].found_by) == {"europe_pmc", "pubtator3"}
    assert all(hit.title for hit in result.hits)
    esearch = next(request for request in replay.requests if request.url.path.endswith("esearch.fcgi"))
    assert esearch.url.params["term"] == result.queries["pubmed"]
    assert esearch.url.params["tool"] == "agrihub"
    assert "api_key" not in esearch.url.params
    item = by_pmid["35955771"].evidence()[0]
    assert item.category == "literature" and item.primary_citation.startswith("PMID:35955771")
    assert item.quote == by_pmid["35955771"].title


def test_pubmed_results_are_dropped_when_it_ignored_every_alias(tmp_path: Path):
    ignored = {
        "esearchresult": {
            "count": "25",
            "idlist": ["1", "2"],
            "warninglist": {"quotedphrasesnotfound": ['"GmXYZ1"[tiab]'], "phrasesignored": []},
        }
    }

    async def scenario() -> literature.LiteratureSearch:
        client = _client(Replay({"esearch.fcgi": ignored}), tmp_path)
        try:
            return await literature.search_literature(
                client, gene_id="Glyma.18G092000", aliases=["GmXYZ1"], species=SPECIES, traits=TRAITS
            )
        finally:
            await client.aclose()

    result = asyncio.run(scenario())
    assert result.counts["pubmed"] == 0
    assert all("pubmed" not in hit.found_by for hit in result.hits)


def test_each_api_is_rate_limited_to_its_documented_rate(tmp_path: Path):
    assert literature.RATE_LIMITS == {"europe_pmc": 5.0, "pubtator3": 3.0, "pubmed": 3.0}
    assert literature.LiteratureClient(ncbi_api_key="key").limits["pubmed"] == literature.NCBI_KEY_RATE

    async def scenario() -> tuple[float, literature.LiteratureClient]:
        client = literature.LiteratureClient(transport=httpx.MockTransport(Replay()), rates={"pubtator3": 4.0})
        started = time.monotonic()
        await asyncio.gather(*(client.get("pubtator3", f"{literature.PUBTATOR}/search/", {"text": f"q{index}"}) for index in range(8)))
        elapsed = time.monotonic() - started
        await client.aclose()
        return elapsed, client

    elapsed, client = asyncio.run(scenario())
    assert client.stats["pubtator3:request"] == 8
    assert elapsed >= 0.9, elapsed


def test_429_is_retried_after_the_servers_delay_and_responses_are_cached(tmp_path: Path):
    calls = {"count": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json=_recorded("pubtator_search.json")["response"])

    async def scenario() -> tuple[literature.Fetched, literature.Fetched, literature.LiteratureClient]:
        client = _client(Replay({"/search/": flaky}), tmp_path, backoff=0.01)
        try:
            first = await client.get("pubtator3", f"{literature.PUBTATOR}/search/", {"text": "x"})
            second = await client.get("pubtator3", f"{literature.PUBTATOR}/search/", {"text": "x"})
            return first, second, client
        finally:
            await client.aclose()

    first, second, client = asyncio.run(scenario())
    assert calls["count"] == 2
    assert client.stats["pubtator3:retryable_status"] == 1
    assert client.stats["pubtator3:request"] == 2
    assert client.stats["pubtator3:cache_hit"] == 1
    assert not first.cached and second.cached
    assert second.data == first.data and second.fetched_at == first.fetched_at


def test_persistent_server_errors_surface_as_api_errors_not_failures(tmp_path: Path):
    async def scenario() -> literature.LiteratureSearch:
        client = _client(Replay({"/search/": lambda request: httpx.Response(503)}), tmp_path, attempts=2, backoff=0.01)
        try:
            return await literature.search_literature(
                client, gene_id="Glyma.05G101300", aliases=["GH3"], species=SPECIES, traits=TRAITS, ncbi_gene_ids=["100811309"]
            )
        finally:
            await client.aclose()

    result = asyncio.run(scenario())
    assert result.errors["pubtator3:100811309"].startswith("HTTP 503")
    assert result.hits


def test_passages_keep_verbatim_sentences_and_classify_the_relation(tmp_path: Path):
    async def scenario(**kwargs: Any) -> tuple[list[literature.Passage], dict[str, str]]:
        client = _client(Replay(), tmp_path)
        try:
            return await literature.extract_passages(client, gene_id="Glyma.09G193000", pmids=["35955771"], species=SPECIES, **kwargs)
        finally:
            await client.aclose()

    passages, notes = asyncio.run(scenario(aliases=["GmIAA27", "Glyma.09G193000"], specific_aliases=["GmIAA27"], traits=["dwarf", "plant height"]))
    document = _recorded("pubtator_export.json")["response"]["PubTator3"][0]
    texts = [passage["text"] for passage in document["passages"]]
    assert passages and not notes
    title = passages[0]
    assert title.quote in texts[0] and title.quote == texts[0]
    assert title.section == "TITLE" and title.species_scope == "sentence"
    assert title.relation == "mention" and title.alias == "GmIAA27" and title.trait_term == "dwarf"
    assert title.evidence()[0].quote == title.quote
    assert all(any(passage.quote in text for text in texts) for passage in passages)

    specific, _ = asyncio.run(scenario(aliases=["GH3"], specific_aliases=["GH3"], traits=["auxin response"]))
    generic, generic_notes = asyncio.run(scenario(aliases=["GH3"], specific_aliases=[], traits=["auxin response"]))
    assert [passage.species_scope for passage in specific] == ["document"]
    assert specific[0].quote.startswith("Expression analysis of GmIAA27 and auxin response genes")
    assert generic == [] and "no sentence" in generic_notes["35955771"]


def test_passage_relations_and_pubtator_gene_tags(tmp_path: Path):
    text = (
        "Overexpression of GmDW1 reduced plant height in soybean. "
        "GmDW1 is associated with plant height in a soybean GWAS panel. "
        "The tagged gene shapes plant height in soybean (Fig. 2)."
    )
    tagged_start = text.index("The tagged gene")
    export = {
        "PubTator3": [
            {
                "pmid": 111,
                "pmcid": None,
                "passages": [
                    {"infons": {"type": "title"}, "offset": 0, "text": "A soybean dwarf gene", "annotations": []},
                    {
                        "infons": {"type": "abstract"},
                        "offset": 21,
                        "text": text,
                        "annotations": [
                            {
                                "infons": {"type": "Gene", "identifier": "100800001"},
                                "text": "tagged gene",
                                "locations": [{"offset": 21 + tagged_start + 4, "length": 11}],
                            }
                        ],
                    },
                ],
            }
        ]
    }

    async def scenario() -> list[literature.Passage]:
        client = _client(Replay({"/export/biocjson": export}), tmp_path)
        try:
            passages, _ = await literature.extract_passages(
                client,
                gene_id="Glyma.18G092000",
                pmids=["111"],
                aliases=["GmDW1"],
                specific_aliases=["GmDW1"],
                species=SPECIES,
                traits=["plant height"],
                ncbi_gene_ids=["100800001"],
                max_per_pmid=5,
            )
            return passages
        finally:
            await client.aclose()

    passages = asyncio.run(scenario())
    assert [passage.relation for passage in passages] == ["causal-experimental", "association", "mention"]
    assert [passage.quote for passage in passages] == [
        "Overexpression of GmDW1 reduced plant height in soybean.",
        "GmDW1 is associated with plant height in a soybean GWAS panel.",
        "The tagged gene shapes plant height in soybean (Fig. 2).",
    ]
    assert passages[2].gene_normalized and passages[2].alias == "GeneID:100800001"
    assert all(passage.quote in text for passage in passages)


def test_titles_are_unescaped_and_untagged():
    assert literature._strip_tags("Network in &lt;i&gt;Arabidopsis&lt;/i&gt;: roles") == "Network in Arabidopsis : roles"


def test_sentence_split_keeps_abbreviations_together():
    text = "Wang et al. found GmDW1 in soybean, e.g. in cv. Williams. A second sentence follows."
    spans = literature.split_sentences(text)
    assert [text[start:end] for start, end in spans] == [
        "Wang et al. found GmDW1 in soybean, e.g. in cv. Williams.",
        "A second sentence follows.",
    ]


@pytest.fixture
def bundle(fixture_env: FixtureBundle) -> Any:
    return open_bundle("soybean")


def test_ncbi_gene_records_map_locus_tags_to_the_canonical_assembly(bundle: Any):
    rows = {row["ncbi_gene_id"]: row for row in bundle.rows("SELECT * FROM ncbi_genes")}
    assert set(rows) == {"100779533", "100800001", "100800002", "100800003", "842527"}
    assert rows["100800001"]["gene_id"] == "Glyma.18G092000" and rows["100800001"]["mapping"] == "ancestor"
    assert rows["100800002"]["gene_id"] == "Glyma.05G032200" and rows["100800002"]["mapping"] == "locus_tag"
    assert rows["100800003"]["gene_id"] is None and rows["100800003"]["assembly"] == "Wm82.a4.v1"
    assert rows["842527"]["species"] == "arabidopsis" and rows["842527"]["gene_id"] == "AT1G62300"
    hub = bundle.rows("SELECT DISTINCT genes_per_pmid FROM ncbi_gene_pubmed WHERE pmid = '30000002'")
    assert hub == [{"genes_per_pmid": 32}]


def test_gene_publications_drop_hub_papers_and_attach_go_citations(bundle: Any):
    rows, dropped = literature.gene_publications(bundle, ["Glyma.18G092000", "Glyma.18G092200", "AT1G62300"])
    assert [(row.gene_id, row.pmid) for row in rows] == [("Glyma.18G092000", "30000001"), ("AT1G62300", "30000003")]
    assert dropped == {"Glyma.18G092000": 1, "Glyma.18G092200": 1}
    assert rows[0].go_terms == ["GO:0009740 gibberellic acid mediated signaling pathway (IMP)"]
    item = rows[0].evidence()[0]
    assert item.subtype == "gene2pubmed" and item.primary_citation == "PMID:30000001" and item.quote is None
    kept, _ = literature.gene_publications(bundle, ["Glyma.18G092200"], max_genes_per_pmid=100)
    assert [row.pmid for row in kept] == ["30000002"]


def test_gene_aliases_separate_specific_names_from_ortholog_symbols(bundle: Any):
    entries = {entry.gene_id: entry for entry in literature.gene_aliases(bundle, ["Glyma.18G092000", "Glyma.18G092200", "Glyma.05G032200"])}
    dw1 = {alias.text: alias for alias in entries["Glyma.18G092000"].aliases}
    assert dw1["GmDW1"].specific and dw1["GmDWARF1"].specific and not dw1["DW1"].specific
    assert dw1["dwarf protein 1"].kind == "designation" and not dw1["dwarf protein 1"].specific
    assert entries["Glyma.18G092000"].ncbi_gene_ids == ["100800001"]
    assert entries["Glyma.18G092000"].search_terms()[0] == "GmDW1"
    wrky = {alias.text: alias for alias in entries["Glyma.18G092200"].aliases}
    assert wrky["LOC100779533"].kind == "ncbi_locus" and not wrky["LOC100779533"].searchable
    assert wrky["GLYMA_18G092200"].specific
    assert not {alias.text: alias for alias in entries["Glyma.05G032200"].aliases}["GH3"].specific


def test_search_tool_stores_hits_as_quoted_literature_evidence(bundle: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    replay = Replay()
    clients: list[literature.LiteratureClient] = []

    def client() -> literature.LiteratureClient:
        if not clients:
            clients.append(_client(replay, tmp_path))
        return clients[0]

    monkeypatch.setattr(literature_tools, "_client", client)
    config = {"configurable": {"run_id": "literature-tool"}}

    async def scenario() -> tuple[Any, Any]:
        try:
            search = await literature_tools.search_literature.ainvoke(
                {"name": "search_literature", "args": {"gene_ids": ["Glyma.18G092000"], "trait": "plant height"}, "id": "call-1", "type": "tool_call"},
                config,
            )
            passages = await literature_tools.extract_passages.ainvoke(
                {"name": "extract_passages", "args": {"gene_id": "Glyma.18G092000", "pmids": ["35955771"], "trait": "plant height", "extra_aliases": ["GmIAA27"]}, "id": "call-2", "type": "tool_call"},
                config,
            )
            return search, passages
        finally:
            await clients[0].aclose()

    search, passages = asyncio.run(scenario())
    assert search.status == "success", search.content
    assert search.content.startswith("search_literature 'plant height':")
    assert "trait terms: plant height" in search.content
    store = EvidenceStore.for_run("literature-tool")
    hits = store.get(search.artifact["aliases"])
    assert hits and all(item.category == "literature" and item.subtype == "search_hit" and item.quote for item in hits)
    assert all(item.primary_citation and item.primary_citation.startswith("PMID:") for item in hits)
    europe = next(request for request in replay.requests if request.url.params.get("resultType") == "lite")
    assert europe.url.params["query"].startswith('("GmDW1" OR "GmDWARF1"') and 'TITLE_ABS:"soybean"' in europe.url.params["query"]
    assert 'TITLE_ABS:"DW1"' in europe.url.params["query"]
    assert passages.status == "success" and "extract_passages Glyma.18G092000" in passages.content
    store.close()
