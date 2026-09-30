"""Ontology terms from OBO files and the Crop Ontology OWL export."""

import re
import xml.etree.ElementTree as ElementTree
from collections import Counter
from pathlib import Path
from typing import Any

from agrihub_data.build.context import BuildContext, BuildError, open_text
from agrihub_data.registry import NON_GENOMIC_ASSEMBLY, Source

_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
_CROP_TERM = re.compile(r"(CO_\d+:\d+)$")
_OWL = "{http://www.w3.org/2002/07/owl#}"
_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_RDFS = "{http://www.w3.org/2000/01/rdf-schema#}"
_SKOS = "{http://www.w3.org/2004/02/skos/core#}"
_CROP_NAMESPACES = {
    "Trait": "trait",
    "Agronomic": "trait",
    "Morphological": "trait",
    "Phenological": "trait",
    "Biochemical": "trait",
    "Abiotic_stress": "trait",
    "Biotic_stress": "trait",
    "Variable": "variable",
    "Method": "method",
    "Estimation": "method",
    "Measurement": "method",
    "Counting": "method",
    "Computation": "method",
    "Scale": "scale",
    "Nominal": "scale",
    "Ordinal": "scale",
    "Numerical": "scale",
    "Duration": "scale",
}


def build_ontology(ctx: BuildContext, source: Source) -> None:
    """Load every ontology file of a source."""
    for path in ctx.files(source):
        if path.name.endswith((".obo", ".obo.gz")):
            load_obo(ctx, source, path)
        elif path.name.endswith(".owl"):
            load_crop_owl(ctx, source, path)
        else:
            raise BuildError(f"{source.id}: unsupported ontology file {path.name}")


def load_obo(
    ctx: BuildContext,
    source: Source,
    path: Path,
    *,
    prefixes: set[str] | None = None,
) -> int:
    """Insert ``[Term]`` stanzas of one OBO file, keeping its own id prefix."""
    header: dict[str, str] = {}
    terms: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_header = True
    with open_text(path) as handle:
        for raw in handle:
            line = raw.strip()
            if line.startswith("["):
                in_header = False
                current = _new_term() if line == "[Term]" else None
                if current is not None:
                    terms.append(current)
                continue
            key, separator, value = line.partition(": ")
            if not separator:
                continue
            if in_header:
                header.setdefault(key, value)
            elif current is not None:
                _apply(current, key, value)
    kept = [term for term in terms if term["term_id"] and term["name"]]
    wanted = prefixes or _main_prefix(kept)
    version = ctx.source_version(source)
    data_version = header.get("data-version")
    if data_version:
        version = f"{version}; {data_version}"
    seen = {
        str(row[0])
        for row in ctx.connection.execute(
            f"SELECT term_id FROM ontology_terms WHERE ontology IN ({', '.join('?' for _ in wanted)})",
            sorted(wanted),
        ).fetchall()
    }
    rows = []
    for term in kept:
        if term["term_id"].split(":", 1)[0] not in wanted:
            continue
        if term["term_id"] in seen:
            ctx.count(source.id, f"duplicate_terms:{path.name}")
            continue
        seen.add(term["term_id"])
        rows.append(
            {
                "species": ctx.species,
                "assembly": NON_GENOMIC_ASSEMBLY,
                "source_version": version,
                "ontology": term["term_id"].split(":", 1)[0],
                **term,
                "source_db": source.name,
            }
        )
    inserted = ctx.insert("ontology_terms", rows)
    ctx.count(source.id, f"terms:{path.name}", inserted)
    return inserted


def load_crop_owl(ctx: BuildContext, source: Source, path: Path) -> int:
    """Insert Crop Ontology classes (traits, variables, methods, scales) from RDF/XML."""
    rows: list[dict[str, Any]] = []
    root = ElementTree.parse(path).getroot()
    for element in root.iter(f"{_OWL}Class"):
        match = _CROP_TERM.search(element.get(f"{_RDF}about", ""))
        label = element.findtext(f"{_RDFS}label")
        if match is None or not label:
            continue
        parents: list[str] = []
        namespace = None
        for parent in element.findall(f"{_RDFS}subClassOf"):
            resource = parent.get(f"{_RDF}resource", "")
            tail = resource.rsplit("/", 1)[-1]
            if _CROP_TERM.search(resource):
                parents.append(tail)
            elif tail in _CROP_NAMESPACES:
                namespace = _CROP_NAMESPACES[tail]
        rows.append(
            {
                "species": ctx.species,
                "assembly": NON_GENOMIC_ASSEMBLY,
                "source_version": ctx.source_version(source),
                "ontology": match.group(1).split(":", 1)[0],
                "term_id": match.group(1),
                "name": label.strip(),
                "namespace": namespace,
                "definition": element.findtext(f"{_SKOS}definition"),
                "synonyms": sorted(
                    {text.strip() for text in (alt.text for alt in element.findall(f"{_SKOS}altLabel")) if text and text.strip()}
                ),
                "parents": sorted(set(parents)),
                "is_obsolete": False,
                "source_db": source.name,
            }
        )
    rows.sort(key=lambda row: row["term_id"])
    inserted = ctx.insert("ontology_terms", rows)
    ctx.count(source.id, f"terms:{path.name}", inserted)
    return inserted


def _new_term() -> dict[str, Any]:
    return {
        "term_id": "",
        "name": "",
        "namespace": None,
        "definition": None,
        "synonyms": [],
        "parents": [],
        "is_obsolete": False,
    }


def _apply(term: dict[str, Any], key: str, value: str) -> None:
    if key == "id":
        term["term_id"] = value.strip()
    elif key == "name":
        term["name"] = value.strip()
    elif key == "namespace":
        term["namespace"] = value.strip()
    elif key == "def":
        quoted = _QUOTED.match(value)
        term["definition"] = quoted.group(1) if quoted else value
    elif key == "synonym":
        quoted = _QUOTED.match(value)
        if quoted and quoted.group(1) not in term["synonyms"]:
            term["synonyms"].append(quoted.group(1))
    elif key == "is_a":
        parent = value.split("!", 1)[0].split("{", 1)[0].strip()
        if parent and parent not in term["parents"]:
            term["parents"].append(parent)
    elif key == "is_obsolete":
        term["is_obsolete"] = value.strip() == "true"


def _main_prefix(terms: list[dict[str, Any]]) -> set[str]:
    tally = Counter(term["term_id"].split(":", 1)[0] for term in terms if ":" in term["term_id"])
    return {tally.most_common(1)[0][0]} if tally else set()
