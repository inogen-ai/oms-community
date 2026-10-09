"""Plain descriptions of where a source safety check held and why.

A finding keeps a place (file or part, line) and a category. It never keeps the
matched text (spec D5), so a stored plan cannot leak what the sanitiser removed.
"""
from oms.sources.models import CheckFinding

_LABELS = {
    "phone": "looks like a phone number",
    "email": "looks like an email address",
    "file_size": "is larger than 10 MB",
}
_INJECTION_THRESHOLD = 0.5


def _label(category: str) -> str:
    if category in _LABELS:
        return _LABELS[category]
    kind, _, detail = category.partition(":")
    if kind == "injection" and detail:
        return f"looks like an instruction to an AI model ({detail})"
    if kind == "check_unavailable" and detail:
        return f"a required check could not run ({detail})"
    if kind == "policy" and detail:
        return f"conflicts with a workspace safety rule ({detail})"
    return f"was held ({category})"


def describe_finding(finding: CheckFinding) -> str:
    place = finding.path or ("SKILL.md text" if finding.part_id else "")
    if finding.line is not None:
        place = f"{place}, line {finding.line}" if place else f"line {finding.line}"
    label = _label(finding.category)
    return f"{place}: {label}" if place else label[:1].upper() + label[1:]


def describe_hold(findings: tuple[CheckFinding, ...]) -> str:
    if not findings:
        return "Held for safety review"
    text = f"Held: {describe_finding(findings[0])}"
    more = len(findings) - 1
    return f"{text} (and {more} more)" if more else text


def text_findings(text: str, redactions, screened, *, path: str | None = None,
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
        for name in sorted(screened.categories):
            found.append(CheckFinding(path=path, part_id=part_id, line=line, category=f"injection:{name}"))
    return tuple(found)
