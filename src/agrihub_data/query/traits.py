"""Trait text to ontology terms: curated overrides, local TF-IDF, optional OLS4.

``map_trait`` uses a curator-editable profile from
``query/traits/<species>.yaml`` when the trait names one; that profile
overrides everything else. Otherwise it ranks ontology terms in the bundle
by TF-IDF cosine similarity over names and synonyms, and only when nothing
matches (and ``use_ols=True``) asks the EBI OLS4 search API.
"""

import math
import re
import threading
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from importlib import resources
from typing import Any, Literal

import httpx
import yaml
from pydantic import BaseModel, Field

from agrihub_data.bundle import Bundle

OLS_URL = "https://www.ebi.ac.uk/ols4/api/search"
TFIDF_MIN_SCORE = 0.55
TFIDF_PER_ONTOLOGY = 3
TRAIT_ONTOLOGIES = ("TO", "SOY", "CO_", "PPTO", "GO", "PO")
"""Ontologies TF-IDF matches are drawn from, in report order; ``CO_`` stands for the species' Crop Ontology."""
_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({"a", "an", "and", "of", "the", "in", "to", "for", "or", "on", "by", "with", "trait"})

TermOrigin = Literal["curated", "tfidf", "ols"]
_priors_off = False


class TraitTerm(BaseModel):
    """One ontology term in a trait profile."""

    term_id: str
    name: str
    ontology: str
    score: float
    origin: TermOrigin


class TraitProfile(BaseModel):
    """Ontology terms, keywords and gene families that describe one trait."""

    query: str
    key: str
    species: str
    terms: list[TraitTerm] = Field(default_factory=list)
    expanded_ids: list[str] = Field(default_factory=list)
    """Term ids plus their descendants, used for matching annotations."""
    keywords: list[str] = Field(default_factory=list)
    seed_families: list[str] = Field(default_factory=list)

    def ids(self, ontology: str | None = None) -> list[str]:
        """Return the profile's term ids, optionally for one ontology prefix."""
        return [term.term_id for term in self.terms if ontology is None or term.ontology == ontology]

    @property
    def to(self) -> list[str]:
        """Return Plant Trait Ontology ids."""
        return self.ids("TO")

    @property
    def go(self) -> list[str]:
        """Return Gene Ontology ids."""
        return self.ids("GO")

    @property
    def po(self) -> list[str]:
        """Return Plant Ontology ids."""
        return self.ids("PO")

    @property
    def crop_co(self) -> list[str]:
        """Return Crop Ontology and SoyBase ontology ids."""
        return [term.term_id for term in self.terms if term.ontology.startswith("CO_") or term.ontology == "SOY"]

    def matched_terms(self, term_ids: list[str]) -> list[str]:
        """Return the given ids that belong to the profile or its descendants."""
        wanted = set(self.expanded_ids) | set(self.ids())
        return [term for term in term_ids if term in wanted]

    def matched_keywords(self, *texts: str | None) -> list[str]:
        """Return profile keywords that occur as whole words in any text."""
        haystack = " ".join(text for text in texts if text).casefold()
        return [keyword for keyword in self.keywords if _contains_phrase(haystack, keyword)]


def map_trait(
    trait_text: str,
    species: str,
    bundle: Bundle | None = None,
    *,
    use_ols: bool = False,
    client: httpx.Client | None = None,
) -> TraitProfile:
    """Map free trait text to a :class:`TraitProfile`."""
    normalized = _normalize(trait_text)
    curated = _curated(species, normalized)
    profile = TraitProfile(
        query=trait_text,
        key=str(curated["key"]) if curated else _slug(normalized),
        species=species,
        keywords=list(dict.fromkeys([normalized, *(curated or {}).get("keywords", [])])),
        seed_families=list((curated or {}).get("seed_families", [])),
    )
    names = _term_names(bundle) if bundle is not None else {}
    for term_id in (curated or {}).get("terms", []):
        name, ontology = names.get(term_id, (term_id, term_id.split(":", 1)[0]))
        profile.terms.append(TraitTerm(term_id=term_id, name=name, ontology=ontology, score=1.0, origin="curated"))
    if bundle is not None and curated is None:
        profile.terms.extend(_index(bundle).search(normalized))
    if _priors_disabled():
        profile.keywords = [normalized] if normalized else []
        profile.seed_families = []
    if use_ols and not profile.terms:
        profile.terms.extend(_ols(normalized, client))
    if bundle is not None:
        profile.expanded_ids = sorted(_descendants(bundle, profile.ids()))
    for term in profile.terms:
        if term.origin != "ols" and term.ontology != "GO":
            phrase = _normalize(term.name)
            if phrase and phrase not in profile.keywords and len(phrase) > 3:
                profile.keywords.append(phrase)
    return profile


def _ontology_group(ontology: str) -> str:
    return "CO_" if ontology.startswith("CO_") else ontology


@contextmanager
def without_priors() -> Iterator[None]:
    """Map traits without curated keywords or seed families (benchmark ablation), process-wide.

    Ontology terms stay; the keywords shrink to the trait text itself. The
    benchmark runs one study at a time, so a process flag is enough.
    """
    global _priors_off
    previous, _priors_off = _priors_off, True
    try:
        yield
    finally:
        _priors_off = previous


def _priors_disabled() -> bool:
    return _priors_off


def _normalize(text: str) -> str:
    return " ".join(_TOKEN.findall(text.casefold()))


def _slug(text: str) -> str:
    return "_".join(text.split()) or "trait"


def _contains_phrase(haystack: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase.casefold())}(?![a-z0-9])", haystack) is not None


def _tokens(text: str) -> list[str]:
    tokens = []
    for token in _TOKEN.findall(text.casefold()):
        if token in _STOPWORDS:
            continue
        if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        tokens.append(token)
    return tokens


@cache
def _overrides(species: str) -> list[dict[str, Any]]:
    path = resources.files("agrihub_data.query") / "traits" / f"{species}.yaml"
    if not path.is_file():
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(loaded.get("profiles") or [])


def _curated(species: str, normalized: str) -> dict[str, Any] | None:
    for profile in _overrides(species):
        names = [_normalize(str(name)) for name in profile.get("names") or []]
        if normalized in names:
            return profile
    for profile in _overrides(species):
        for name in profile.get("names") or []:
            phrase = _normalize(str(name))
            if len(phrase) > 3 and _contains_phrase(normalized, phrase):
                return profile
    return None


class _TermIndex:
    """TF-IDF vectors over ontology term names and synonyms."""

    def __init__(self, rows: list[tuple[str, str, str, list[str]]]) -> None:
        self.terms = [(term_id, name, ontology) for term_id, name, ontology, _ in rows]
        documents = [_tokens(" ".join([name, *synonyms])) for _, name, _, synonyms in rows]
        self.exact: dict[str, list[int]] = defaultdict(list)
        for index, (_, name, _, synonyms) in enumerate(rows):
            for label in {name, *synonyms}:
                self.exact[_normalize(label)].append(index)
        frequency: Counter[str] = Counter(token for document in documents for token in set(document))
        total = len(documents)
        self.idf = {token: math.log((total + 1) / (count + 1)) + 1.0 for token, count in frequency.items()}
        self.postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for index, document in enumerate(documents):
            counts = Counter(document)
            weights = {token: count * self.idf[token] for token, count in counts.items()}
            norm = math.sqrt(sum(weight * weight for weight in weights.values())) or 1.0
            for token, weight in weights.items():
                self.postings[token].append((index, weight / norm))

    def search(self, normalized: str) -> list[TraitTerm]:
        query = Counter(_tokens(normalized))
        if not query:
            return []
        weights = {token: count * self.idf.get(token, 0.0) for token, count in query.items()}
        norm = math.sqrt(sum(weight * weight for weight in weights.values())) or 1.0
        scores: dict[int, float] = defaultdict(float)
        for token, weight in weights.items():
            for index, document_weight in self.postings.get(token, []):
                scores[index] += (weight / norm) * document_weight
        for index in self.exact.get(normalized, []):
            scores[index] = max(scores[index], 1.0)
        by_ontology: dict[str, list[tuple[float, int]]] = defaultdict(list)
        for index, score in scores.items():
            if score >= TFIDF_MIN_SCORE:
                by_ontology[_ontology_group(self.terms[index][2])].append((score, index))
        found: list[TraitTerm] = []
        for group in TRAIT_ONTOLOGIES:
            ranked = sorted(by_ontology.get(group, []), key=lambda item: (-item[0], self.terms[item[1]][0]))
            for score, index in ranked[:TFIDF_PER_ONTOLOGY]:
                term_id, name, ontology = self.terms[index]
                found.append(
                    TraitTerm(term_id=term_id, name=name, ontology=ontology, score=round(min(score, 1.0), 3), origin="tfidf")
                )
        return found


_indexes: dict[tuple[str, int], _TermIndex] = {}
_children: dict[tuple[str, int], dict[str, set[str]]] = {}
_names: dict[tuple[str, int], dict[str, tuple[str, str]]] = {}
_cache_lock = threading.Lock()


def _key(bundle: Bundle) -> tuple[str, int]:
    return str(bundle.path), bundle.mtime


def _index(bundle: Bundle) -> _TermIndex:
    key = _key(bundle)
    with _cache_lock:
        if key not in _indexes:
            rows = bundle.rows_raw(
                "SELECT term_id, name, ontology, synonyms FROM ontology_terms "
                "WHERE NOT is_obsolete AND ("
                "(ontology = 'GO' AND namespace = 'biological_process') "
                "OR (ontology LIKE 'CO\\_%' ESCAPE '\\' AND namespace = 'trait') "
                "OR ontology IN ('TO', 'SOY', 'PPTO', 'PO')) ORDER BY term_id"
            )
            _indexes[key] = _TermIndex(
                [(str(term_id), str(name), str(ontology), list(synonyms or [])) for term_id, name, ontology, synonyms in rows]
            )
        return _indexes[key]


def _term_names(bundle: Bundle) -> dict[str, tuple[str, str]]:
    key = _key(bundle)
    with _cache_lock:
        if key not in _names:
            _names[key] = {
                str(term_id): (str(name), str(ontology))
                for term_id, name, ontology in bundle.rows_raw("SELECT term_id, name, ontology FROM ontology_terms")
            }
        return _names[key]


def _descendants(bundle: Bundle, term_ids: list[str]) -> set[str]:
    key = _key(bundle)
    with _cache_lock:
        if key not in _children:
            children: dict[str, set[str]] = defaultdict(set)
            for term_id, parents in bundle.rows_raw("SELECT term_id, parents FROM ontology_terms WHERE NOT is_obsolete"):
                for parent in parents or []:
                    children[str(parent)].add(str(term_id))
            _children[key] = children
        children = _children[key]
    found = set(term_ids)
    frontier = list(term_ids)
    while frontier:
        for child in children.get(frontier.pop(), ()):
            if child not in found:
                found.add(child)
                frontier.append(child)
    return found


def _ols(normalized: str, client: httpx.Client | None) -> list[TraitTerm]:
    http = client or httpx.Client(timeout=20.0)
    try:
        response = http.get(
            OLS_URL,
            params={"q": normalized, "ontology": "to,po,go", "rows": 10, "fieldList": "obo_id,label,ontology_prefix"},
        )
        response.raise_for_status()
        documents = response.json().get("response", {}).get("docs", [])
    except (httpx.HTTPError, ValueError):
        return []
    finally:
        if client is None:
            http.close()
    return [
        TraitTerm(
            term_id=str(document["obo_id"]),
            name=str(document.get("label") or document["obo_id"]),
            ontology=str(document.get("ontology_prefix") or str(document["obo_id"]).split(":", 1)[0]).upper(),
            score=0.5,
            origin="ols",
        )
        for document in documents
        if document.get("obo_id")
    ][:5]
