"""MaizeGDB (B73 NAM-5.0, Zm00001eb.1): annotation, the v3/v4 -> v5 gene xref and Wallace 2014 GWAS.

- ``maizegdb_annotation``: deflines from ``Zm00001eb.1.fulldata.txt.gz``
  (gene product, else full name), GO ids from ``...GMs-GOTerms.csv.gz`` and
  Pfam/PANTHER/InterPro from ``...interproscan.tsv.gz`` (transcript rows,
  collapsed per gene). The GFF carries no descriptions.
- ``maizegdb_xref``: ``B73v{3,4}_to_B73v5.tsv`` (one old gene -> comma list of
  v5 genes) as ``id_map`` synonyms on the old assembly. One-to-many links are
  kept; consumers use a link only when it names one v5 gene.
- ``maizegdb_wallace_gwas``: ``B73v5_Wallace_2015_SNPs.gff`` (NAM GWAS hits of
  Wallace et al. 2014, positions lifted by MaizeGDB to v5). Each feature is a
  100-bp window; its start is the stored position.
"""

import csv
import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

from agrihub_data.build.context import BuildContext, open_text, split_list, tsv_rows
from agrihub_data.build.gff import features
from agrihub_data.build.resolve import GeneResolver
from agrihub_data.registry import Source, UnknownAssemblyError

_TRANSCRIPT = re.compile(r"^(.+?)_[TP]\d+$")
_GO = re.compile(r"GO:\d{7}")
_XREF = re.compile(r"^B73v(\d)_to_B73v5\.tsv$")
_ANALYSES = {"Pfam": "pfam", "PANTHER": "panther"}


def build_maizegdb_annotation(ctx: BuildContext, source: Source) -> None:
    """Load deflines, GO terms and protein domains of v5 gene models."""
    assembly = ctx.registry.canonical_assembly
    resolver = GeneResolver.load(ctx)
    base = ctx.base(source, assembly)
    deflines: dict[str, str] = {}
    for fields in tsv_rows(ctx.file(source, "*fulldata*")):
        if len(fields) < 13:
            continue
        gene = resolver.get(fields[1])
        text = next((value.strip() for value in (fields[12], fields[11]) if value.strip() not in {"", "-"}), "")
        if gene and text:
            deflines.setdefault(gene, text)
    ctx.temp_table("maize_deflines", {"gene_id": "VARCHAR", "defline": "VARCHAR"}, ({"gene_id": gene, "defline": text} for gene, text in deflines.items()))
    ctx.connection.execute(
        "UPDATE genes SET defline = d.defline FROM maize_deflines AS d WHERE genes.assembly = ? AND genes.gene_id = d.gene_id AND genes.defline IS NULL",
        [assembly],
    )
    ctx.connection.execute("DROP TABLE maize_deflines")
    ctx.count(source.id, "deflines", len(deflines))

    go_terms: set[tuple[str, str]] = set()
    go_file = ctx.optional_file(source, "*GOTerms.csv.gz")
    if go_file is not None:
        with open_text(go_file) as handle:
            for row in csv.DictReader(handle):
                gene = resolver.get(row.get("gene_model") or "")
                if gene:
                    go_terms.update((gene, go_id) for go_id in _GO.findall(row.get("obo_terms") or ""))
    ctx.count(
        source.id,
        "go_annot",
        ctx.insert(
            "go_annot",
            (
                {**base, "gene_id": gene, "go_id": go_id, "evidence_code": "IEA", "qualifier": None, "reference": "MaizeGDB GMs-GOTerms", "source_db": "MaizeGDB"}
                for gene, go_id in sorted(go_terms)
            ),
        ),
    )

    domains: dict[tuple[str, str, str], str | None] = {}
    interpro = ctx.optional_file(source, "*interproscan.tsv.gz")
    if interpro is not None:
        for fields in tsv_rows(interpro):
            if len(fields) < 5:
                continue
            match = _TRANSCRIPT.match(fields[0].strip())
            gene = resolver.get(match.group(1) if match else fields[0])
            if gene is None:
                continue
            kind = _ANALYSES.get(fields[3].strip())
            if kind and fields[4].strip():
                domains.setdefault((gene, kind, fields[4].strip()), (fields[5].strip() if len(fields) > 5 else "") or None)
            if len(fields) > 11 and fields[11].strip().startswith("IPR"):
                domains.setdefault((gene, "interpro", fields[11].strip()), (fields[12].strip() if len(fields) > 12 else "") or None)
    ctx.count(
        source.id,
        "annotation",
        ctx.insert(
            "annotation",
            (
                {**base, "gene_id": gene, "kind": kind, "value": value, "label": label, "source_db": "MaizeGDB InterProScan"}
                for (gene, kind, value), label in sorted(domains.items())
            ),
        ),
    )


def build_maizegdb_xref(ctx: BuildContext, source: Source) -> None:
    """Load older-version gene ids as synonyms of v5 genes."""
    canonical = ctx.registry.canonical_assembly
    resolver = GeneResolver.load(ctx)
    for path in ctx.files(source):
        match = _XREF.match(path.name)
        if match is None:
            continue
        try:
            old = ctx.registry.assembly(f"B73v{match.group(1)}").id
        except UnknownAssemblyError:
            ctx.count(source.id, f"skipped_unregistered_assembly:{path.name}")
            continue
        fan_out: Counter[int] = Counter()

        def rows(path: Any = path, old: str = old, fan_out: Counter[int] = fan_out) -> Iterator[dict[str, Any]]:
            for fields in tsv_rows(path):
                if len(fields) < 2 or not fields[0].strip():
                    continue
                targets = [gene for raw in split_list(fields[1]) if (gene := resolver.get(raw))]
                fan_out[min(len(targets), 3)] += 1
                for target in dict.fromkeys(targets):
                    yield {
                        **ctx.base(source, old),
                        "from_id": fields[0].strip(),
                        "to_id": target,
                        "to_assembly": canonical,
                        "relation": "synonym",
                        "source_db": "MaizeGDB B73_gene_xref",
                    }

        ctx.count(source.id, f"synonyms:{old}", ctx.insert("id_map", rows()))
        for size, count in sorted(fan_out.items()):
            ctx.count(source.id, f"{old}:v5_targets_{size if size < 3 else '3+'}", count)


def build_maizegdb_wallace_gwas(ctx: BuildContext, source: Source) -> None:
    """Load Wallace et al. 2014 NAM GWAS hits on v5."""
    assembly = ctx.registry.canonical_assembly
    base = ctx.base(source, assembly)
    hits: dict[str, dict[str, Any]] = {}
    for feature in features(ctx.file(source, "*Wallace*.gff"), {"SNP"}):
        chrom = ctx.chrom(assembly, feature.seqid)
        if chrom is None or not ctx.in_bounds(assembly, chrom, feature.start, feature.start):
            ctx.count(source.id, "skipped_position")
            continue
        trait = feature.attributes.get("trait", "").replace("_", " ").strip()
        marker = feature.attributes.get("RS") or feature.attributes.get("B73v2_pos") or f"{chrom}:{feature.start}"
        hit_id = f"Wallace2014|{marker}|{trait}"
        if not trait or hit_id in hits:
            ctx.count(source.id, "duplicates_or_blank")
            continue
        hits[hit_id] = {
            **base,
            "hit_id": hit_id,
            "source_db": "Wallace 2014 NAM GWAS",
            "study_id": "Wallace2014",
            "trait_name": trait,
            "trait_terms": [],
            "marker": marker,
            "chrom": chrom,
            "pos": feature.start,
            "p_value": None,
            "pmid": None,
            "doi": "10.1371/journal.pgen.1004845",
            "reported_genes": [],
            "placement": f"reported_window:{feature.end - feature.start + 1}bp",
        }
    ctx.count(source.id, "gwas_hits", ctx.insert("gwas_hits", hits.values()))
