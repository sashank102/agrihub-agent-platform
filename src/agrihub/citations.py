"""Keep report citations pointed at evidence and sources that exist."""

import re

_CITATION = re.compile(r"\[(E\d+|source:[A-Za-z0-9_.-]+)\]")


def validate_citations(markdown: str, known: set[str]) -> tuple[str, list[str]]:
    """Strip citation markers that are not in ``known`` and return the orphans.

    A marker is ``[E12]`` or ``[source:atted_soybean]``. Orphans are removed
    from the text and returned in first-seen order so the writer can list
    them under limitations. The function never adds a citation.
    """
    orphans: list[str] = []

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token in known or token.removeprefix("source:") in known:
            return match.group(0)
        orphans.append(token)
        return ""

    cleaned = _CITATION.sub(replace, markdown)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" +([.,;:])", r"\1", cleaned)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    return cleaned, list(dict.fromkeys(orphans))


def citation_tokens(markdown: str) -> list[str]:
    """Return every citation token in ``markdown``, in order."""
    return [match.group(1) for match in _CITATION.finditer(markdown)]
