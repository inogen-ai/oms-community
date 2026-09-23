"""Lucene-safe reduction of free text into fulltext query terms.

The fulltext index parses its query with Lucene's classic syntax, in which
raw agent or user text is executable ("A/B" opens a regex, ":" a field
query). Everything that feeds free text into db.index.fulltext.queryNodes
must reduce it to plain lowercase terms (Lucene's default OR) first - free
text must never be able to crash the parser.
"""
import re

_FULLTEXT_TOKEN = re.compile(r"[a-z0-9]+")


def fulltext_terms(query: str) -> str | None:
    """Plain lowercase terms joined by spaces, or None when nothing survives."""
    terms = _FULLTEXT_TOKEN.findall(query.lower())
    return " ".join(terms) if terms else None
