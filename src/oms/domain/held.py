"""The wire format of a held transaction's `ReviewItem.reason`, in one place.

Ingestion packs four things into that one string: why the transaction was held,
what each button will do, and the sanitised correction itself
(ingestion/service.py). It has to stay packed, because `reason` is the only
field a `ReviewItem` carries that can hold the text at all - the raw correction
is discarded by design and the transaction node keeps only a short `summary` -
and because an older client that renders nothing but `reason` must keep showing
the whole story.

So the string stays as it is and this module is the seam: ingestion builds it
here, the review service takes it apart here, and the two can never drift into
two different formats. Everything the parser understands is optional, and
anything it does not recognise survives in `raw`, so an item written by an
older or newer OMS still renders - with less structure, never with less text.
"""
import re
from dataclasses import dataclass, field

# What separates the machine's reason from the correction a human has to read.
# Written by `pack`, matched by `parse`, and phrased for the reader because
# until there was a card that split it apart, this sentence WAS the card.
HOLD_MARKER = "; approve to compile the text as-is, reject to discard: "

# What the sanitiser's hold reads as (ingestion/service.py). Matched as a
# substring rather than an equality so it keeps working when the injection
# screen's reason is joined onto it.
SANITISER_REASON = "sanitiser altered the correction text"

# The injection screen's own summary, from `ScreenResult.reason()`
# (ports/injection_screen.py). That method and this pattern are one format
# described twice; change either and change both.
_INJECTION = re.compile(r"possible prompt injection \((?P<score>[0-9.]+)\):\s*(?P<cats>[^;]*)")


@dataclass(frozen=True)
class HeldReason:
    """A held transaction's `reason`, taken apart.

    `text` is the sanitised correction awaiting a decision. `raw` is the reason
    exactly as stored, so a caller that wants the original sentence - an audit
    record, a log line - never has to reassemble it from the parts.
    """
    raw: str
    text: str | None = None
    sanitiser_altered: bool = False
    injection_score: float | None = None
    categories: tuple[str, ...] = field(default_factory=tuple)

    @property
    def cause(self) -> str:
        """The machine's reason on its own, with the button sentence and the
        correction stripped off."""
        return self.raw.split(HOLD_MARKER, 1)[0]


def pack(held_reason: str, correction: str) -> str:
    """The `reason` string ingestion stores on a held transaction's review item."""
    return f"{held_reason}{HOLD_MARKER}{correction}"


def parse(reason: str | None) -> HeldReason:
    """Take a held item's `reason` apart. Never raises and never returns None:
    an unrecognised string comes back whole in `raw` with nothing else set,
    because a card with the text and no structure is the status quo, and a card
    with neither is the defect this module exists to fix."""
    if not reason:
        return HeldReason(raw="")
    head, marker, tail = reason.partition(HOLD_MARKER)
    # `partition` puts everything in `head` when the marker is absent, which is
    # the right split for an older item: cause only, no text located.
    text = tail if marker else None
    score: float | None = None
    categories: tuple[str, ...] = ()
    match = _INJECTION.search(head)
    if match is not None:
        try:
            score = float(match.group("score"))
        except ValueError:      # a malformed number must not cost the card
            score = None
        categories = tuple(c.strip() for c in match.group("cats").split(",") if c.strip())
    return HeldReason(
        raw=reason,
        text=text or None,
        sanitiser_altered=SANITISER_REASON in head,
        injection_score=score,
        categories=categories,
    )
