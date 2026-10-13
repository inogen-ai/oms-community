"""The sanitiser port, and the deterministic regex adapter behind it.

`sanitise` returns `Sanitised`, not a bare `ExecutionContext`. The change is
what makes the redaction vault possible at all: every adapter used to build a
token-to-original map and throw it away, because the return type had nowhere to
put it (redaction-vault design §5).

Changing the return type rather than adding a second method is deliberate. With
two methods, a caller that wanted redactions and called the old one would
silently get none, and that failure is invisible.
"""
from bisect import bisect_left
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import accumulate
import re
from typing import Protocol, runtime_checkable

from oms.domain.redaction import Redaction
from oms.ingestion.schema import ExecutionContext, clip_context_fields

# The caps are the address limits (64 before the "@", 63 per label, 253 for a
# domain). They keep the scan linear on long runs of dots or hyphens.
_EMAIL_CANDIDATE = re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,63}\.[\w.-]{1,252}\b")
_PHONE_CANDIDATE = re.compile(r"(?<![\w/])(\+?\d[\d \t()-]{7,}\d)(?![\w/])")
# A date, or a date and a time after a space or a `T`. The time is matched from
# the candidate's start in the text, since the candidate itself stops at the colon.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)?")
_RANGE = re.compile(r"[1-9]\d*-\d+")
# A bare ten-digit run in this range may be a Unix time in seconds (July 2017 to May 2033).
_UNIX_TIME = re.compile(r"1[5-9]\d{8}")


def _ends_in_a_letter_label(candidate: str) -> bool:
    return candidate.rsplit(".", 1)[-1][:1].isalpha()


class _EmailPattern:
    """Email candidates whose last domain label starts with a letter.

    A real address ends in an alphabetic top-level domain (`xn--` forms
    included), and `react@18.3.1` is a package version. The check runs after the
    match, not inside the expression, so a long dotted tail cannot make the
    expression backtrack.

    Trailing labels that do not start with a letter are trimmed, so the address
    in `jane@company.com.2` is still found. The candidate is matched again up to
    the cut, which is always a dot, so the word boundary at the new end holds in
    the text as well. A package version keeps fewer than two labels and is kept.
    """

    def finditer(self, text: str) -> Iterator[re.Match[str]]:
        for match in _EMAIL_CANDIDATE.finditer(text):
            candidate = match.group(0)
            if _ends_in_a_letter_label(candidate):
                yield match
                continue
            local, _, domain = candidate.partition("@")
            labels = domain.split(".")
            while labels and not labels[-1][:1].isalpha():
                labels.pop()
            if len(labels) < 2:
                continue
            end = match.start() + len(local) + 1 + len(".".join(labels))
            trimmed = _EMAIL_CANDIDATE.match(text, match.start(), end)
            if trimmed is not None and _ends_in_a_letter_label(trimmed.group(0)):
                yield trimmed


_EMAIL = _EmailPattern()


class _PhonePattern:
    """Phone candidates minus shapes that are never phone numbers.

    A number does not run across a line, and a date or date-time (`2025-10-03`,
    `2025-10-03 09:00:00`), a plain range (`300000-600000`, `2019-2024`) or a path
    segment (`/12345-1706000000/`) is not a phone number. Real numbers keep a
    leading `+` or `0`, spaces, brackets or more than one hyphen. A run of fewer
    than seven digits (SVG coordinates such as `0 0 12 12`) is not a phone number.

    A bare Unix time in seconds (`1706000000`) is kept only with
    `keep_unix_times`, for skill packages, where timestamps are common. Real
    numbers share that shape (a Brazilian landline written bare, an Argentine
    local mobile), so a correction, which is personal data, keeps redacting it.
    """

    def __init__(self, *, keep_unix_times: bool = False) -> None:
        self.keep_unix_times = keep_unix_times

    def finditer(self, text: str) -> Iterator[re.Match[str]]:
        for match in _PHONE_CANDIDATE.finditer(text):
            candidate = match.group(0)
            date = _ISO_DATE.match(text, match.start())
            if date is not None and date.end() >= match.end() or _RANGE.fullmatch(candidate):
                continue
            if sum(char.isdigit() for char in candidate) < 7:
                continue
            if self.keep_unix_times and _UNIX_TIME.fullmatch(candidate):
                continue
            yield match


_PHONE = _PhonePattern()


@dataclass(frozen=True)
class Sanitised:
    """The scrubbed context, and what was taken out of the correction.

    `redactions` covers the proposed `learning` or `user_correction` only:
    the reviewer judges this guidance, which is what becomes a rule,
    and `user_input` / `agent_raw_output` are never rendered on a card, so
    retaining their originals would widen the exposure for no decision. The
    session context (`session_summary`, `project_name`, `reuse_case`,
    `learning_evidence`) may be shown beside the guidance, but it never
    becomes the rule either, so its originals are not retained for the same
    reason.

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
    # A match overlaps an earlier redaction when one starting before the match's end also
    # ends after its start. With the redactions sorted by start, those starting before the
    # end are a prefix found by bisection, and the furthest end in that prefix decides, so
    # each match costs a lookup rather than a pass over every earlier redaction.
    blocked = sorted((r.start, r.end) for r in existing)
    starts = [start for start, _ in blocked]
    reach = list(accumulate((end for _, end in blocked), max))
    fresh = [m for m in pattern.finditer(text)
             if not ((before := bisect_left(starts, m.end())) and m.start() < reach[before - 1])]
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
    """Deterministic regex scrubber. Presidio is a drop-in replacement behind the same port.

    `keep_unix_times` keeps a bare Unix time in seconds, as `_PhonePattern` says;
    the source checks of skill packages set it, and everything else leaves it off.
    """

    def __init__(self, *, keep_unix_times: bool = False) -> None:
        self.keep_unix_times = keep_unix_times
        self._phone = _PhonePattern(keep_unix_times=True) if keep_unix_times else _PHONE

    def _scrub(self, text: str, counters: dict[str, int],
               seen: dict[str, str]) -> tuple[str, tuple[Redaction, ...]]:
        text, found = _substitute(text, _EMAIL, "EMAIL", counters, seen, ())
        return _substitute(text, self._phone, "PHONE", counters, seen, found)

    def sanitise(self, ctx: ExecutionContext) -> Sanitised:
        counters: dict[str, int] = {}
        # `seen` was a local the call threw away; it is the redaction map, and
        # all that changed is that it now leaves the method. Shared across all
        # eight fields, so one value keeps one placeholder - and the fields are
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
        # Session context comes after the fields above, so their numbering is
        # unchanged by it, and through the same `seen`, so an address quoted
        # in both the learning and the evidence keeps one placeholder. Its
        # redaction maps are discarded, as the task's and output's are.
        # Named one by one, like every field here: a field this method does
        # not name is dropped from the result, never passed through unscrubbed.
        session_summary = self._scrub_context(ctx.session_summary, counters, seen)
        project_name = self._scrub_context(ctx.project_name, counters, seen)
        reuse_case = self._scrub_context(ctx.reuse_case, counters, seen)
        learning_evidence = self._scrub_context(ctx.learning_evidence, counters, seen)
        # Retain only the proposed guidance's map, not historical context.
        # A learning needs the same custody and review path as a correction.
        # Built unvalidated and then clipped, because a placeholder can take
        # context past its limit (`clip_context_fields` says why).
        return Sanitised(
            context=clip_context_fields(ExecutionContext.model_construct(
                user_input=user_input, agent_raw_output=agent_raw_output,
                user_correction=correction, learning=learning,
                session_summary=session_summary, project_name=project_name,
                reuse_case=reuse_case, learning_evidence=learning_evidence)),
            redactions=learning_redactions if ctx.learning is not None else redactions)

    def _scrub_context(self, text: str | None, counters: dict[str, int],
                       seen: dict[str, str]) -> str | None:
        return None if text is None else self._scrub(text, counters, seen)[0]
