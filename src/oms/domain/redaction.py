"""One span the sanitiser replaced, and what it replaced.

Here rather than beside the sanitiser because the vault port
(`oms/ports/redaction_vault.py`) carries this type, and a port importing from
`ingestion/` would invert the layering. `domain/held.py` sets the precedent: a
small value type both sides of a seam need gets its own domain module.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Redaction:
    """One replaced span.

    `start` and `end` index the SANITISED text, so a consumer can highlight the
    placeholder without re-running anything. `original` is the only field that
    is PII; everything else is safe to log, count and display.
    """
    start: int
    end: int
    label: str        # "<PERSON>", "<EMAIL_1>": the placeholder as written
    original: str
