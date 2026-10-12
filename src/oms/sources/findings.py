"""Plain descriptions of where a source safety check held and why.

A finding keeps a place (file or part, line) and a category. It never keeps the
matched text (spec D5), so a stored plan cannot leak what the sanitiser removed.
"""
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from oms.domain.redaction import Redaction
from oms.ports.injection_screen import ScreenResult
from oms.sources.models import CheckFinding

_LABELS = {
    "phone": "looks like a phone number",
    "email": "looks like an email address",
    "file_size": "is larger than 10 MB",
}
_INJECTION_THRESHOLD = 0.5
PERSONAL_DATA: frozenset[str] = frozenset({"phone", "email"})
# The only codes a personal-data hold may carry. Anything else (a truncated list, a screen hold,
# a size limit, a check that could not run) means the findings are not the whole story.
_PERSONAL_DATA_CODES: frozenset[str] = frozenset({"source_text_hold", "text_requires_sanitisation"})


def personal_data_only(codes: Iterable[str], findings: Iterable[CheckFinding]) -> bool:
    """The one rule for a hold that is personal data and nothing else (spec D2, ruling R6).

    `codes` is the check's code plus its reasons. The hold must be a text hold, carry only the
    reasons a phone number or email address causes, and every finding it lists must be personal
    data. A truncated list is never personal data alone: the dropped findings are unknown."""
    code_set, found = set(codes), tuple(findings)
    return ("source_text_hold" in code_set and code_set <= _PERSONAL_DATA_CODES and bool(found)
            and all(finding.category in PERSONAL_DATA for finding in found))


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
    found: list[CheckFinding] = []
    shift = 0
    for redaction in redactions:
        # Offsets index the sanitised output, so undo earlier length changes.
        start = redaction.start - shift
        shift += len(redaction.label) - len(redaction.original)
        category = redaction.label.strip("<>").split("_")[0].lower()
        found.append(CheckFinding(path=path, part_id=part_id, line=text.count("\n", 0, start) + 1,
                                  category=category))
    if screened.score >= _INJECTION_THRESHOLD:
        spans = tuple(screened.spans)
        line = text.count("\n", 0, min(spans)[0]) + 1 if spans else None
        # A screen hold that names no category is still an injection finding, never personal data.
        for name in sorted(screened.categories) or ("unclassified",):
            found.append(CheckFinding(path=path, part_id=part_id, line=line, category=f"injection:{name}"))
    return tuple(found)
