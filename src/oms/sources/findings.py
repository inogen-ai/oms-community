"""Plain descriptions of where a source safety check held and why.

A finding keeps a place (file or part, line) and a category. It never keeps the
matched text (spec D5), so a stored plan cannot leak what the sanitiser removed.
"""
from collections.abc import Iterable
import re
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict

from oms.domain.redaction import Redaction
from oms.ports.injection_screen import ScreenResult
from oms.security.injection import ESCAPE_RUN, category_spans
from oms.sources.models import CheckFinding
from oms.sources.primitives import ACKNOWLEDGEABLE

_LABELS = {
    "phone": "looks like a phone number",
    "email": "looks like an email address",
    "file_size": "is larger than 10 MB",
}
_INJECTION_THRESHOLD = 0.5
PERSONAL_DATA: frozenset[str] = frozenset({"phone", "email"})
# An "encoding" match that holds an escape run: the escapes are on the line, what they spell is not.
# Words about an encoding ("a base64 encoded data URI") stay `injection:encoding`.
ESCAPED_TEXT = "injection:escaped-text"
# Screen findings a reviewer cannot read as words: hidden characters, a score that names no
# category, and escaped text. They are never acceptable as they are, wherever they are.
ACCEPTABLE_INJECTION_EXCLUDED: frozenset[str] = frozenset({"injection:invisible-characters",
                                                           "injection:unclassified", ESCAPED_TEXT})
# Lines kept per finding category of one text. A category on more lines than this marks the check
# truncated, so the hold is never acceptable with lines the reviewer was not shown.
INJECTION_LINES = 20
# The only codes an acceptable hold may carry: the reasons a phone number, an email address or a
# visible instruction cause. Anything else (a truncated list, a size limit, a check that could not
# run, a workspace rule) means the findings are not the whole story.
ACCEPTABLE_CODES: frozenset[str] = frozenset({"source_text_hold", "text_requires_sanitisation",
                                               "injection_screen_hold"})


def _acceptable(finding: CheckFinding) -> bool:
    if finding.category in PERSONAL_DATA:
        return True
    # Visible: the category's own pattern matched the raw text, so the finding has a line the
    # reviewer can read. A category found only in a disguised or decoded view has none.
    return (finding.category.startswith("injection:") and finding.category not in ACCEPTABLE_INJECTION_EXCLUDED
            and finding.line is not None and re.fullmatch(ACKNOWLEDGEABLE, finding.category) is not None)


def acceptable_hold(codes: Iterable[str], findings: Iterable[CheckFinding]) -> bool:
    """The one rule for a hold a reviewer may take as it is (ruling R6, widened on 14 October 2026).

    `codes` is the check's code plus its reasons. The hold must be a text hold, carry only the
    reasons personal data or a screen hit cause, and every finding it lists must be personal data
    or a visible instruction to an AI model. A truncated list is never acceptable: the dropped
    findings are unknown."""
    code_set, found = set(codes), tuple(findings)
    return ("source_text_hold" in code_set and code_set <= ACCEPTABLE_CODES and bool(found)
            and all(_acceptable(finding) for finding in found))


class CheckFindingView(BaseModel):
    """A finding as the card shows it: place, line, category and a plain summary, never the matched text."""
    model_config = ConfigDict(extra="forbid")
    path: str | None = None
    part_id: str | None = None
    line: int | None = None
    category: str
    summary: str


def _label(category: str) -> str:
    if category in _LABELS:
        return _LABELS[category]
    kind, _, detail = category.partition(":")
    if kind == "injection" and detail:
        return f"looks like an instruction to an AI model ({detail})"
    # Codes are snake_case; the reviewer reads them as words.
    if kind == "check_unavailable" and detail:
        return f"a required check could not run ({detail.replace('_', ' ')})"
    if kind == "policy" and detail:
        return f"conflicts with a workspace safety rule ({detail.replace('_', ' ')})"
    return f"was held ({category})"


def describe_finding(finding: CheckFinding) -> str:
    place = finding.path or ("SKILL.md text" if finding.part_id else "")
    if finding.line is not None:
        place = f"{place}, line {finding.line}" if place else f"line {finding.line}"
    label = _label(finding.category)
    return f"{place}: {label}" if place else label[:1].upper() + label[1:]


def finding_view(finding: CheckFinding) -> CheckFindingView:
    return CheckFindingView(path=finding.path, part_id=finding.part_id, line=finding.line,
                            category=finding.category, summary=describe_finding(finding))


def distinct_findings(findings: Iterable[CheckFinding]) -> tuple[CheckFinding, ...]:
    """The findings to show, each hit once.

    A hit in SKILL.md is found twice: by the file check, at the file's line, and
    by the check of the projected part that holds it, at a line within that part.
    The file's finding is the one to show, so a part finding is dropped when
    SKILL.md has a finding of the same category. Only the list shown changes;
    each part keeps its own check result.
    """
    found = tuple(dict.fromkeys(findings))
    in_skill = {finding.category for finding in found if finding.path == "SKILL.md"}
    return tuple(finding for finding in found
                 if not (finding.path is None and finding.part_id is not None and finding.category in in_skill))


def describe_hold(findings: tuple[CheckFinding, ...]) -> str:
    if not findings:
        return "Held for safety review"
    text = f"Held: {describe_finding(findings[0])}"
    more = len(findings) - 1
    return f"{text} (and {more} more)" if more else text


def text_findings(text: str, redactions: Iterable[Redaction], screened: ScreenResult, *, path: str | None = None,
                  part_id: str | None = None) -> tuple[CheckFinding, ...]:
    return checked_findings(text, redactions, screened, path=path, part_id=part_id)[0]


class TextFindings(NamedTuple):
    findings: tuple[CheckFinding, ...]
    # Whether a screen category had more lines than are kept.
    truncated: bool
    # Where in the text each located AI-instruction finding's words are, for the card to show.
    spans: tuple[tuple[int, int], ...]


def checked_findings(text: str, redactions: Iterable[Redaction], screened: ScreenResult, *,
                     path: str | None = None, part_id: str | None = None) -> TextFindings:
    """The findings of one text check."""
    found: list[CheckFinding] = []
    located: list[tuple[int, int]] = []
    truncated = False
    shift = 0
    # The redactions are in text order, so line ends are counted on from the previous one,
    # not again from the start: a long text with many findings stays linear. One out of
    # order is counted from the start, as before.
    line, counted = 1, 0
    for redaction in redactions:
        # Offsets index the sanitised output, so undo earlier length changes.
        start = redaction.start - shift
        shift += len(redaction.label) - len(redaction.original)
        category = redaction.label.strip("<>").split("_")[0].lower()
        if start < counted:
            line, counted = 1, 0
        line += text.count("\n", counted, start)
        counted = start
        found.append(CheckFinding(path=path, part_id=part_id, line=line, category=category))
    if screened.score >= _INJECTION_THRESHOLD:
        # A screen hold that names no category is still an injection finding, never personal data.
        names = sorted(screened.categories) or ["unclassified"]
        # Each category marks every line that holds one of its own matches in the text as written,
        # so a harmful copy far down a long file is shown, not accepted behind the first one. It has
        # a single finding with no line when a disguised or decoded view matched it there or a
        # disguised copy remains beside its visible matches. It never takes another category's
        # line, so a hidden instruction cannot pass for the visible one beside it.
        visible = category_spans(text, names)
        for name in names:
            spans = visible.get(name, ())
            if not spans:
                found.append(CheckFinding(path=path, part_id=part_id, line=None, category=f"injection:{name}"))
                continue
            located.extend(spans)
            lines: dict[str, list[int]] = {}
            line, counted = 1, 0
            for start, end in spans:
                line += text.count("\n", counted, start)
                counted = start
                # An escape run anywhere in the match, not only one that is the whole match: the
                # words form ("base64 ... string") can take a run along with it.
                category = (ESCAPED_TEXT if name == "encoding" and ESCAPE_RUN.search(text, start, end)
                            else f"injection:{name}")
                marked = lines.setdefault(category, [])
                if not marked or marked[-1] != line:
                    marked.append(line)
            for category, numbers in lines.items():
                truncated = truncated or len(numbers) > INJECTION_LINES
                found.extend(CheckFinding(path=path, part_id=part_id, line=number, category=category)
                             for number in numbers[:INJECTION_LINES])
    return TextFindings(tuple(found), truncated, tuple(located))
