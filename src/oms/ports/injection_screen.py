"""Replaceable prompt-injection screening for untrusted contributions.

A screen returns a score and supporting details. Suspicious input is held for
review rather than discarded: legitimate guidance may quote attack examples.
Screening is one defence layer and does not establish that content is safe.
"""
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ScreenResult:
    """How suspicious a piece of untrusted text looks.

    `score` runs 0.0 (nothing found) to 1.0 (unmistakable), so a caller can
    threshold it. `categories` names what was recognised, so a reviewer sees
    why rather than a bare number.

    `spans` says WHERE, as (start, end) offsets into the text that was passed
    in. A reviewer deciding whether a held correction is an attack or an
    ordinary rule needs the words, not only the verdict, and on a long
    correction finding them by eye is most of the work.

    Spans are BEST EFFORT and may be empty on a text that scored. Detection
    runs against a normalised probe (security/normalise.py) that undoes
    spacing, homoglyphs and encoding, and whose offsets do not map back to the
    original; a disguised attack is therefore still detected but cannot be
    located. Empty spans mean "we cannot point at it", never "it is clean" -
    read `score` for that. Consumers must treat the offsets as advisory and
    keep them inside the text they screened.
    """
    score: float
    categories: frozenset[str] = field(default_factory=frozenset)
    spans: tuple[tuple[int, int], ...] = ()

    @property
    def clean(self) -> bool:
        return self.score <= 0.0

    def reason(self) -> str:
        """Reviewer-facing summary; empty when nothing was found.

        `domain/held.py` parses this string back into a score and a category
        list for the held-transaction review card. The two are one format
        described twice: change this wording and change that pattern.
        """
        if self.clean:
            return ""
        return (f"possible prompt injection ({self.score:.2f}): "
                f"{', '.join(sorted(self.categories))}")


CLEAN = ScreenResult(score=0.0)


@runtime_checkable
class InjectionScreen(Protocol):
    def screen(self, text: str) -> ScreenResult:
        """Judge untrusted text. Never raises: a screen that cannot run must
        degrade to CLEAN rather than block ingestion, because losing a
        correction is worse than missing one detection (the G2 hold and the
        publish gate both remain in front of anything a screen lets past)."""
        ...
