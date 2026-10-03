"""RAP-DB (IRGSP-1.0): functional annotation, the RAP <-> MSU id map and curated trait genes.

- ``rapdb_annotation`` reads ``IRGSP-1.0_representative_annotation_*.tsv.gz``
  (one row per representative transcript): InterPro domains with names and
  GO ids go to ``annotation`` and ``go_annot`` per locus.
- ``rapdb_msu`` reads ``RAP-MSU_*.txt.gz``. The map is many-to-many: one RAP
  locus can list several MSU loci and one MSU locus can belong to several
  RAP loci. Every pair becomes an ``id_map`` synonym ``LOC_Os.. -> Os..g..``;
  RAP loci the file maps to ``None`` keep an explicit ``no_counterpart`` row
  (``to_id = 'None'``) so a lookup can say "no MSU locus" rather than
  "unknown".
- ``rapdb_curated_genes`` merges ``curated_genes.json`` and
  ``agri_genes.json`` per locus into ``known_genes``: symbols, names, Trait
  Ontology terms and PubMed references curated by RAP-DB.
"""

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from typing import Any

from agrihub_data.build.context import BuildContext, split_list, tsv_rows
from agrihub_data.build.resolve import GeneResolver
from agrihub_data.registry import Source

_INTERPRO_SPLIT = re.compile(r"(?<=\(IPR\d{6}\)),\s*")
_INTERPRO = re.compile(r"^(.*?)\s*\((IPR\d{6})\)$")
_GO = re.compile(r"GO:\d{7}")
_TERM = re.compile(r"^((?:TO|PO):\d{7})\s*-\s*(.*)$")
_MSU_TRANSCRIPT = re.compile(r"^(LOC_Os\d{2}g\d{5})(?:\.\d+)?$")
_WORD = re.compile(r"[A-Za-z0-9]")


def build_rapdb_annotation(ctx: BuildContext, source: Source) -> None:
    """Load InterPro and GO annotations of RAP-DB representative transcripts, per locus."""
    assembly = ctx.registry.canonical_assembly
    known = GeneResolver.load(ctx)
    rows = tsv_rows(ctx.file(source, "*_annotation_*.tsv.gz"), skip_comments=False)
    header = [name.strip() for name in next(rows)]
    index = {name: position for position, name in enumerate(header)}
    domains: dict[tuple[str, str], str | None] = {}
    go_terms: set[tuple[str, str]] = set()
    for fields in rows:
        gene = known.get(_cell(fields, index, "Locus_ID"))
        if gene is None:
            ctx.count(source.id, "rows_without_gene")
            continue
        for part in _INTERPRO_SPLIT.split(_cell(fields, index, "InterPro")):
            match = _INTERPRO.match(part.strip())
            if match:
                domains.setdefault((gene, match.group(2)), match.group(1).strip() or None)
        for go_id in _GO.findall(_cell(fields, index, "GO")):
            go_terms.add((gene, go_id))
    base = ctx.base(source, assembly)
    ctx.count(
        source.id,
        "interpro",
        ctx.insert(
            "annotation",
            (
                {**base, "gene_id": gene, "kind": "interpro", "value": interpro, "label": label, "source_db": "RAP-DB"}
                for (gene, interpro), label in sorted(domains.items())
            ),
        ),
    )
    ctx.count(
        source.id,
        "go_annot",
        ctx.insert(
            "go_annot",
            (
                {**base, "gene_id": gene, "go_id": go_id, "evidence_code": "IEA", "qualifier": None, "reference": source.version, "source_db": "RAP-DB"}
                for gene, go_id in sorted(go_terms)
            ),
        ),
    )


def build_rapdb_msu(ctx: BuildContext, source: Source) -> None:
    """Load the many-to-many RAP <-> MSU locus map as id_map synonyms."""
    assembly = ctx.registry.canonical_assembly
    known = GeneResolver.load(ctx)
    pairs: set[tuple[str, str]] = set()
    without: set[str] = set()
    for fields in tsv_rows(ctx.file(source, "RAP-MSU*")):
        if len(fields) < 2:
            continue
        rap = known.get(fields[0])
        if rap is None:
            ctx.count(source.id, "rap_not_in_gene_models")
            continue
        targets = [match.group(1) for item in split_list(fields[1]) if (match := _MSU_TRANSCRIPT.match(item))]
        if not targets:
            if fields[1].strip() == "None":
                without.add(rap)
            continue
        pairs.update((msu, rap) for msu in targets)
    per_msu = Counter(msu for msu, _ in pairs)
    per_rap = Counter(rap for _, rap in pairs)
    ctx.count(source.id, "rap_loci_with_msu", len(per_rap))
    ctx.count(source.id, "rap_loci_none", len(without))
    ctx.count(source.id, "msu_loci", len(per_msu))
    ctx.count(source.id, "msu_loci_to_several_rap", sum(1 for count in per_msu.values() if count > 1))
    ctx.count(source.id, "rap_loci_to_several_msu", sum(1 for count in per_rap.values() if count > 1))
    base = ctx.base(source, assembly)

    def rows() -> Iterator[dict[str, Any]]:
        for msu, rap in sorted(pairs):
            yield {**base, "from_id": msu, "to_id": rap, "to_assembly": assembly, "relation": "synonym", "source_db": "RAP-MSU"}
        for rap in sorted(without):
            yield {**base, "from_id": rap, "to_id": "None", "to_assembly": assembly, "relation": "no_counterpart", "source_db": "RAP-MSU"}

    ctx.count(source.id, "id_map", ctx.insert("id_map", rows()))


def build_rapdb_curated_genes(ctx: BuildContext, source: Source) -> None:
    """Merge RAP-DB curated and agronomic gene lists into one known-gene row per locus."""
    assembly = ctx.registry.canonical_assembly
    known = GeneResolver.load(ctx)
    merged: dict[str, dict[str, Any]] = {}
    lists: dict[str, set[str]] = defaultdict(set)
    for path in ctx.files(source):
        if not path.name.endswith(".json"):
            continue
        entries = json.loads(path.read_text(encoding="utf-8"))
        label = path.name.removesuffix(".json")
        for entry in entries if isinstance(entries, list) else []:
            locus = str(entry.get("locus") or "")
            ctx.count(source.id, f"entries:{label}")
            gene = known.get(locus)
            record = merged.setdefault(
                locus,
                {"gene": gene, "symbols": [], "names": [], "terms": {}, "pmids": []},
            )
            lists[locus].add(label)
            for symbol in split_list(str(entry.get("gene_symbols") or "")):
                if _WORD.search(symbol) and symbol not in record["symbols"]:
                    record["symbols"].append(symbol)
            for name in split_list(str(entry.get("gene_names") or "")):
                if name not in record["names"]:
                    record["names"].append(name)
            for raw in entry.get("to") or []:
                match = _TERM.match(str(raw).strip())
                if match:
                    record["terms"].setdefault(match.group(1), match.group(2).strip())
            for pmid in (entry.get("references") or {}).keys():
                if str(pmid).isdigit() and str(pmid) not in record["pmids"]:
                    record["pmids"].append(str(pmid))

    def rows() -> Iterator[dict[str, Any]]:
        for locus, record in sorted(merged.items()):
            gene = record["gene"]
            ctx.count(source.id, "mapping:identical" if gene else "mapping:unmapped")
            yield {
                **ctx.base(source, assembly),
                "gene_id": gene or locus,
                "source_gene_id": locus,
                "source_assembly": assembly,
                "mapping": "identical" if gene else "unmapped",
                "symbols": record["symbols"][:12],
                "symbol_long": record["names"][0] if record["names"] else None,
                "synopsis": "; ".join(record["names"][:4]) or None,
                "trait_terms": sorted(record["terms"]),
                "trait_names": [record["terms"][term] for term in sorted(record["terms"])],
                "confidence": None,
                "pmids": record["pmids"],
                "dois": [],
                "weight": 1.0,
                "source_db": "RAP-DB curated genes" if "curated_genes" in lists[locus] else "RAP-DB agronomic genes",
            }

    ctx.count(source.id, "known_genes", ctx.insert("known_genes", rows()))


def _cell(fields: list[str], index: dict[str, int], name: str) -> str:
    position = index.get(name)
    return fields[position].strip() if position is not None and position < len(fields) else ""
