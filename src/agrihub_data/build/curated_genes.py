"""Known trait genes from gene catalogues other than LIS: Oryzabase, funRiceGenes, MaizeGDB, curated lists.

Every row is keyed by a canonical gene through :class:`GeneResolver`; ids
that resolve to nothing stay with ``mapping = 'unmapped'`` and no coordinates.
``weight`` says how much the catalogue's trait links are worth:

- ``oryzabase_genes`` (1.0): Oryzabase ``GENE_LIST`` with curated Trait
  Ontology terms. The download is declared ``Windows-31J``; it is decoded as
  strict UTF-8 first and as Shift-JIS (cp932) when that fails.
- ``funricegenes`` (``params.weight``, 0.5): keywords text-mined from paper
  titles and sentences (``geneKeyword.table.txt``); keyword matches only.
- ``maizegdb_classical_genes`` (``params.weight``, 0.6): loci with a
  MaizeGDB full name in ``Zm00001eb.1.fulldata.txt.gz`` (``d8 dwarf
  plant8``). The names carry no ontology terms, so traits match by keyword.
- ``curated_genes``: a YAML list shipped with AgriHub, every entry with its
  citation (``agrihub_data/curated/<species>.known_genes.yaml``).
"""

import re
from typing import Any

import yaml

from agrihub_data.build.context import (
    BuildContext,
    BuildError,
    decode_text,
    split_list,
    tsv_rows,
)
from agrihub_data.build.resolve import GeneResolver
from agrihub_data.registry import Source

_TO = re.compile(r"(TO:\d{7})\s*-\s*([^,]+)")
_MODEL_ID = re.compile(r"^(?:Zm\d{5}[a-z]{1,2}\d{6}|GRMZM\d\w+|AC\d+\.\d_FG\d+|LOC\d+)$")


def build_oryzabase_genes(ctx: BuildContext, source: Source) -> None:
    """Load Oryzabase genes with RAP or MSU ids and their Trait Ontology terms."""
    resolver = GeneResolver.load(ctx)
    path = ctx.file(source, "*")
    text, encoding = decode_text(path)
    ctx.count(source.id, f"encoding:{encoding}")
    lines = text.splitlines()
    header = [name.strip() for name in lines[0].split("\t")]
    index = {name: position for position, name in reversed(list(enumerate(header)))}
    rows: list[dict[str, Any]] = []
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) < len(header) - 2:
            ctx.count(source.id, "short_rows")
            continue

        def cell(name: str, fields: list[str] = fields) -> str:
            position = index.get(name)
            return fields[position].strip() if position is not None and position < len(fields) else ""

        ids = split_list(cell("RAP ID"), ", ") or split_list(cell("MSU ID"), ", ")
        genes = {gene for raw in ids if (gene := resolver.get(raw.split(".")[0] if raw.startswith("LOC_") else raw))}
        if not ids:
            ctx.count(source.id, "without_locus_id")
            continue
        terms = dict(_TO.findall(cell("Trait Ontology")))
        symbols = [cell("CGSNL Gene Symbol"), *split_list(cell("Gene symbol synonym(s)"))]
        for gene in sorted(genes) or [ids[0]]:
            ctx.count(source.id, "mapping:identical" if genes else "mapping:unmapped")
            rows.append(
                {
                    **ctx.base(source, ctx.registry.canonical_assembly),
                    "gene_id": gene,
                    "source_gene_id": ids[0],
                    "source_assembly": ctx.registry.canonical_assembly,
                    "mapping": "identical" if genes else "unmapped",
                    "symbols": [symbol for symbol in dict.fromkeys(symbols) if symbol][:12],
                    "symbol_long": cell("CGSNL Gene Name") or None,
                    "synopsis": (cell("Explanation En").strip('"')[:600] or None),
                    "trait_terms": sorted(terms),
                    "trait_names": [terms[term].strip() for term in sorted(terms)],
                    "confidence": None,
                    "pmids": [],
                    "dois": [],
                    "weight": float(source.params.get("weight", 1.0)),
                    "source_db": "Oryzabase",
                }
            )
    ctx.count(source.id, "known_genes", ctx.insert("known_genes", rows))


def build_funricegenes(ctx: BuildContext, source: Source) -> None:
    """Load funRiceGenes keyword links, one row per gene with its keywords and paper titles."""
    resolver = GeneResolver.load(ctx)
    genes: dict[str, dict[str, Any]] = {}
    for fields in tsv_rows(ctx.file(source, "geneKeyword.table.txt")):
        if len(fields) < 5 or fields[0] == "Symbol":
            continue
        rap, msu = fields[1].strip(), fields[2].strip()
        gene = resolver.get(rap) if rap and rap != "NA" else None
        gene = gene or (resolver.get(msu) if msu and msu != "NA" else None)
        if gene is None:
            ctx.count(source.id, "unmapped_rows")
            continue
        record = genes.setdefault(gene, {"symbols": [], "keywords": [], "titles": [], "source": rap if rap != "NA" else msu})
        for symbol in fields[0].split("|"):
            if symbol.strip() and symbol.strip() not in record["symbols"]:
                record["symbols"].append(symbol.strip())
        keyword = fields[3].strip()
        if keyword and keyword not in record["keywords"]:
            record["keywords"].append(keyword)
        title = fields[4].strip()
        if title and title not in record["titles"]:
            record["titles"].append(title)
    weight = float(source.params.get("weight", 0.5))
    rows = (
        {
            **ctx.base(source, ctx.registry.canonical_assembly),
            "gene_id": gene,
            "source_gene_id": record["source"],
            "source_assembly": ctx.registry.canonical_assembly,
            "mapping": "identical",
            "symbols": record["symbols"][:12],
            "symbol_long": None,
            "synopsis": " | ".join(record["titles"][:3]) or None,
            "trait_terms": [],
            "trait_names": record["keywords"][:40],
            "confidence": None,
            "pmids": [],
            "dois": [],
            "weight": weight,
            "source_db": "funRiceGenes",
        }
        for gene, record in sorted(genes.items())
    )
    ctx.count(source.id, "known_genes", ctx.insert("known_genes", rows))


def build_maizegdb_classical_genes(ctx: BuildContext, source: Source) -> None:
    """Load MaizeGDB named loci (symbol plus full name) from the v5 fulldata table."""
    resolver = GeneResolver.load(ctx)
    weight = float(source.params.get("weight", 0.6))
    found: dict[str, dict[str, Any]] = {}
    for fields in tsv_rows(ctx.file(source, "*fulldata*")):
        if len(fields) < 13:
            continue
        gene = resolver.get(fields[1])
        symbol, name, product = fields[10].strip(), fields[11].strip(), fields[12].strip()
        if gene is None or name in {"", "-"} or symbol in {"", "-"} or _MODEL_ID.match(symbol):
            continue
        record = found.setdefault(gene, {"symbols": [], "name": name, "product": product})
        if symbol not in record["symbols"]:
            record["symbols"].append(symbol)
    rows = (
        {
            **ctx.base(source, ctx.registry.canonical_assembly),
            "gene_id": gene,
            "source_gene_id": gene,
            "source_assembly": ctx.registry.canonical_assembly,
            "mapping": "identical",
            "symbols": record["symbols"],
            "symbol_long": record["name"],
            "synopsis": "; ".join(part for part in (record["name"], record["product"]) if part and part != "-") or None,
            "trait_terms": [],
            "trait_names": [],
            "confidence": None,
            "pmids": [],
            "dois": [],
            "weight": weight,
            "source_db": "MaizeGDB classical genes",
        }
        for gene, record in sorted(found.items())
    )
    ctx.count(source.id, "known_genes", ctx.insert("known_genes", rows))


def build_curated_genes(ctx: BuildContext, source: Source) -> None:
    """Load a packaged curated gene list; every entry names its id, traits and citation."""
    resolver = GeneResolver.load(ctx)
    path = ctx.file(source, "*.yaml")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = loaded.get("genes") or []
    if not entries:
        raise BuildError(f"{source.id}: {path.name} lists no genes")
    coordinates = {
        str(gene): (str(chrom), int(start), int(end))
        for gene, chrom, start, end in ctx.connection.execute(
            'SELECT gene_id, chrom, start, "end" FROM genes WHERE assembly = ?', [ctx.registry.canonical_assembly]
        ).fetchall()
    }
    rows: list[dict[str, Any]] = []
    for entry in entries:
        raw = str(entry["gene_id"])
        gene = resolver.get(raw)
        ctx.count(source.id, "mapping:identical" if gene else "mapping:unmapped")
        expected = entry.get("location")
        if gene and expected:
            chrom = ctx.chrom(ctx.registry.canonical_assembly, str(expected["chrom"]))
            actual = coordinates.get(gene)
            same = actual is not None and actual == (chrom, int(expected["start"]), int(expected["end"]))
            ctx.count(source.id, "location_matches_citation" if same else "location_differs_from_citation")
        rows.append(
            {
                **ctx.base(source, ctx.registry.canonical_assembly),
                "gene_id": gene or raw,
                "source_gene_id": raw,
                "source_assembly": ctx.registry.canonical_assembly,
                "mapping": "identical" if gene else "unmapped",
                "symbols": [str(symbol) for symbol in entry.get("symbols") or []],
                "symbol_long": entry.get("name"),
                "synopsis": entry.get("synopsis"),
                "trait_terms": [str(term) for term in entry.get("trait_terms") or []],
                "trait_names": [str(name) for name in entry.get("trait_names") or []],
                "confidence": entry.get("confidence"),
                "pmids": [str(pmid) for pmid in entry.get("pmids") or []],
                "dois": [str(doi) for doi in entry.get("dois") or loaded.get("dois") or []],
                "weight": float(entry.get("weight", source.params.get("weight", 1.0))),
                "source_db": str(source.params.get("source_db") or source.name),
            }
        )
    ctx.count(source.id, "known_genes", ctx.insert("known_genes", rows))
