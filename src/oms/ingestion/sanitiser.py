"""The sanitiser port, and the deterministic regex adapter behind it.

`sanitise` returns `Sanitised`, not a bare `ExecutionContext`. The change is
what makes the redaction vault possible at all: every adapter used to build a
token-to-original map and throw it away, because the return type had nowhere to
put it (redaction-vault design §5).

Changing the return type rather than adding a second method is deliberate. With
two methods, a caller that wanted redactions and called the old one would
silently get none, and that failure is invisible.
"""
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from oms.domain.redaction import Redaction
from oms.ingestion.schema import ExecutionContext

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\w)(\+?\d[\d\s()-]{7,}\d)(?!\w)")


@dataclass(frozen=True)
class Sanitised:
    """The scrubbed context, and what was taken out of the correction.

    `redactions` covers the proposed `learning` or `user_correction` only:
    the reviewer judges this guidance, which is what becomes a rule,
    and `user_input` / `agent_raw_output` are never rendered on a card, so
    retaining their originals would widen the exposure for no decision.

    Empty is the ordinary case - nothing matched - and must not be confused
    with "not computed".
    """
    context: ExecutionContext
    redactions: tuple[Redaction, ...] = ()


@runtime_checkable
class Sanitiser(Protocol):
    def sanitise(self, ctx: ExecutionContext) -> Sanitised: ...


def _substitute(text: str, pattern: re.Pattern, label: str,
                counters: dict[str, int], seen: dict[str, str],
                existing: tuple[Redaction, ...]) -> tuple[str, tuple[Redaction, ...]]:
    """One scrubbing pass, recording where each placeholder LANDED.

    Offsets are built during the substitution rather than found afterwards by
    searching the output, and that is the whole point of this function. A
    correction may legitimately contain the string `<EMAIL_1>` - security
    guidance about redaction says exactly that - and a scan cannot tell the
    author's own text from a replacement. It would report the author's
    occurrence as a redaction carrying somebody else's address, and a restore
    would then splice that address into a place it had never appeared.

    `existing` are the redactions from an earlier pass, with offsets into
    `text`. They are carried through and re-pointed, and this pass never
    replaces inside one: a placeholder is not PII and matching within it would
    corrupt it.
    """
    blocked = [(r.start, r.end) for r in existing]
    fresh = [m for m in pattern.finditer(text)
             if not any(start < m.end() and m.start() < end for start, end in blocked)]
    # Both kinds of span in one ordered walk, so the output is built once and
    # every offset in it is the offset of the string that was just appended.
    segments = sorted(
        [(r.start, r.end, r, None) for r in existing]
        + [(m.start(), m.end(), None, m) for m in fresh],
        key=lambda segment: segment[0])

    parts: list[str] = []
    found: list[Redaction] = []
    cursor = 0
    length = 0
    for start, end, carried, match in segments:
        head = text[cursor:start]
        parts.append(head)
        length += len(head)
        if carried is not None:
            piece = text[start:end]
            found.append(Redaction(start=length, end=length + len(piece),
                                   label=carried.label, original=carried.original))
        else:
            raw = match.group(0)
            if raw not in seen:
                counters[label] = counters.get(label, 0) + 1
                seen[raw] = f"<{label}_{counters[label]}>"
            piece = seen[raw]
            found.append(Redaction(start=length, end=length + len(piece),
                                   label=piece, original=raw))
        parts.append(piece)
        length += len(piece)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), tuple(found)


class RegexSanitiser:
    """Deterministic regex scrubber. Presidio is a drop-in replacement behind the same port."""

    def _scrub(self, text: str, counters: dict[str, int],
               seen: dict[str, str]) -> tuple[str, tuple[Redaction, ...]]:
        text, found = _substitute(text, _EMAIL, "EMAIL", counters, seen, ())
        return _substitute(text, _PHONE, "PHONE", counters, seen, found)

    def sanitise(self, ctx: ExecutionContext) -> Sanitised:
        counters: dict[str, int] = {}
        # `seen` was a local the call threw away; it is the redaction map, and
        # all that changed is that it now leaves the method. Shared across all
        # three fields, so one value keeps one placeholder - and the fields are
        # scrubbed in their declared order so the numbering is exactly what it
        # has always been.
        seen: dict[str, str] = {}
        user_input, _ = self._scrub(ctx.user_input, counters, seen)
        agent_raw_output, _ = self._scrub(ctx.agent_raw_output, counters, seen)
        correction, redactions = (
            self._scrub(ctx.user_correction, counters, seen)
            if ctx.user_correction is not None else (None, ()))
        learning, learning_redactions = (
            self._scrub(ctx.learning, counters, seen)
            if ctx.learning is not None else (None, ()))
        # Retain only the proposed guidance's map, not historical context.
        # A learning needs the same custody and review path as a correction.
        return Sanitised(
            context=ExecutionContext(user_input=user_input,
                                     agent_raw_output=agent_raw_output,
                                     user_correction=correction, learning=learning),
            redactions=learning_redactions if ctx.learning is not None else redactions)
