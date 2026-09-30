"""TAIR public release: Arabidopsis symbols, descriptions, phenotypes, GO and papers."""

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from agrihub_data.build.context import BuildContext, tsv_rows
from agrihub_data.registry import Source

SPECIES = "arabidopsis"
ASSEMBLY = "TAIR10"
_AGI = re.compile(r"^AT[1-5CM]G\d{5}$", re.IGNORECASE)
_MODEL = re.compile(r"^(AT[1-5CM]G\d{5})\.(\d+)$", re.IGNORECASE)
_PMID = re.compile(r"PMID:(\d+)")
_DESCRIPTION_KINDS = ("short_description", "curator_summary", "computational_description")


def build_tair(ctx: BuildContext, source: Source) -> None:
    """Load the TAIR files into annotation, phenotypes, go_annot and gene_publications."""
    base = ctx.base(source, ASSEMBLY, species=SPECIES)
    symbols = _load_aliases(ctx, source, base)
    resolve = _resolver(symbols)
    _load_descriptions(ctx, source, base)

    phenotypes: set[tuple[str, str, str, str]] = set()
    for fields in _data_rows(ctx.file(source, "Locus_Germplasm_Phenotype_*.txt.gz"), "LOCUS_NAME"):
        gene_id = resolve(fields[0])
        if gene_id is None or len(fields) < 3 or not fields[2].strip():
            ctx.count(source.id, "phenotypes_unresolved")
            continue
        phenotypes.add((gene_id, _text(fields[1]) or "", fields[2].strip(), _text(fields[3] if len(fields) > 3 else "") or ""))
    ctx.count(
        source.id,
        "phenotypes",
        ctx.insert(
            "phenotypes",
            (
                {**base, "gene_id": gene_id, "germplasm": germplasm or None, "phenotype": phenotype, "pmid": pmid or None, "source_db": "TAIR"}
                for gene_id, germplasm, phenotype, pmid in sorted(phenotypes)
            ),
        ),
    )

    papers: dict[tuple[str, str], int | None] = {}
    for fields in _data_rows(ctx.file(source, "Locus_Published_*.txt.gz"), "name"):
        gene_id = resolve(fields[0])
        pmid = _text(fields[2]) if len(fields) > 2 else None
        if gene_id is None or not pmid:
            continue
        year = _text(fields[3]) if len(fields) > 3 else None
        papers[(gene_id, pmid)] = int(year) if year and year.isdigit() else None
    ctx.count(
        source.id,
        "gene_publications",
        ctx.insert(
            "gene_publications",
            (
                {**base, "gene_id": gene_id, "pmid": pmid, "year": year, "source_db": "TAIR"}
                for (gene_id, pmid), year in sorted(papers.items())
            ),
        ),
    )

    go_terms: dict[tuple[str, str, str, str], str | None] = {}
    for fields in tsv_rows(ctx.file(source, "ATH_GO_GOSLIM.txt.gz"), skip_comments=False):
        if fields[0].startswith("!") or len(fields) < 10 or not _AGI.match(fields[0].strip()):
            continue
        key = (fields[0].strip().upper(), fields[5].strip(), fields[9].strip(), fields[3].strip())
        if not key[1].startswith("GO:"):
            continue
        cited = _PMID.search(fields[12]) if len(fields) > 12 else None
        if key not in go_terms or (go_terms[key] is None and cited):
            go_terms[key] = f"PMID:{cited.group(1)}" if cited else None
    ctx.count(
        source.id,
        "go_annot",
        ctx.insert(
            "go_annot",
            (
                {
                    **base,
                    "gene_id": gene_id,
                    "go_id": go_id,
                    "evidence_code": code,
                    "qualifier": qualifier or None,
                    "reference": reference,
                    "source_db": "TAIR",
                }
                for (gene_id, go_id, code, qualifier), reference in sorted(go_terms.items())
            ),
        ),
    )


def _load_aliases(ctx: BuildContext, source: Source, base: dict[str, str]) -> dict[str, set[str]]:
    rows: dict[tuple[str, str], str | None] = {}
    symbols: dict[str, set[str]] = defaultdict(set)
    for fields in _data_rows(ctx.file(source, "gene_aliases_*.txt.gz"), "locus_name"):
        gene_id = fields[0].strip().upper()
        symbol = _text(fields[1]) if len(fields) > 1 else None
        if not _AGI.match(gene_id) or not symbol:
            continue
        rows.setdefault((gene_id, symbol), _text(fields[2]) if len(fields) > 2 else None)
        symbols[symbol.casefold()].add(gene_id)
    ctx.count(
        source.id,
        "symbols",
        ctx.insert(
            "annotation",
            (
                {**base, "gene_id": gene_id, "kind": "symbol", "value": symbol, "label": full_name, "source_db": "TAIR"}
                for (gene_id, symbol), full_name in sorted(rows.items())
            ),
        ),
    )
    return symbols


def _load_descriptions(ctx: BuildContext, source: Source, base: dict[str, str]) -> None:
    best: dict[str, tuple[int, list[str]]] = {}
    for fields in _data_rows(ctx.file(source, "Araport11_functional_descriptions_*.txt.gz"), "name"):
        match = _MODEL.match(fields[0].strip())
        if match is None:
            continue
        gene_id, model = match.group(1).upper(), int(match.group(2))
        if gene_id not in best or model < best[gene_id][0]:
            best[gene_id] = (model, fields)
    rows: list[dict[str, Any]] = []
    for gene_id, (_, fields) in sorted(best.items()):
        for kind, position in zip(_DESCRIPTION_KINDS, (2, 3, 4), strict=True):
            value = _text(fields[position]) if position < len(fields) else None
            if value:
                rows.append({**base, "gene_id": gene_id, "kind": kind, "value": value, "label": None, "source_db": "TAIR"})
    ctx.count(source.id, "descriptions", ctx.insert("annotation", rows))


def _resolver(symbols: dict[str, set[str]]) -> Any:
    def resolve(name: str) -> str | None:
        name = name.strip()
        if _AGI.match(name):
            return name.upper()
        matches = symbols.get(name.casefold(), set())
        return next(iter(matches)) if len(matches) == 1 else None

    return resolve


def _data_rows(path: Path, first_header: str) -> Any:
    for fields in tsv_rows(path):
        if fields[0].strip() == first_header:
            continue
        yield fields


def _text(value: str) -> str | None:
    value = value.strip()
    return None if not value or value == "NULL" else value
