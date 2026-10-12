"""Which parts of a review card need a person, and why (spec 2026-10-12, section 4).

`review_parts` reads the persisted plan only, so the card's grouping is computed at
read time and nothing new is stored. A part is "automatic" when the plan applies it
without a decision, "safe" when taking GitHub's version passes the checks and loses
nothing of the reader's, and "decide" otherwise, with every risk that applies. A held
install's skill is an empty record made by the hold, so nothing in it can be lost.
"""
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from oms.sources.compare import same_value
from oms.sources.files import script_change
from oms.sources.findings import PERSONAL_DATA, CheckFindingView, distinct_findings, finding_view, personal_data_only
from oms.sources.models import Change, CheckFinding, Conflict, MergePlan, PartKind
from oms.sources.primitives import NonEmpty, Record

RiskCode = Literal["personal_data", "injection", "check_failed", "check_unavailable", "your_edits",
                   "removed_upstream", "unclear_identity", "script"]
RISK_ORDER: tuple[RiskCode, ...] = ("injection", "check_failed", "check_unavailable", "personal_data",
                                   "removed_upstream", "your_edits", "unclear_identity", "script")
# Reasons the comparison gives (compare.py, files.py, merge.py). Any other code in a
# conflict's reason was added by a check that did not pass.
DECISION_REASONS = frozenset({
    "unknown_base", "unknown_incoming", "unknown_local", "deletion_consent", "local_divergence",
    "independent_ownership", "curated_field", "unknown_identity", "opposite_polarity", "independent_corroboration",
    "unknown_file_mode", "changed_order", "local_placement", "changed_placement", "unknown_placement",
    "unknown_ownership", "ambiguous_replacement"})
IDENTITY_REASONS = frozenset({"unknown_ownership", "independent_ownership", "local_placement", "changed_placement",
                              "unknown_placement", "unknown_identity", "ambiguous_replacement"})
LOCAL_REASONS = frozenset({"local_divergence", "curated_field", "independent_corroboration"})
# Never a reason on their own to ask: no base to compare with, or an upstream
# reorder or change of polarity that loses nothing of the reader's (ruling R4).
QUIET_REASONS = frozenset({"unknown_base", "changed_order", "opposite_polarity"})
FIELD_LABELS = {"description": "Description", "tags": "Tags", "license": "Licence",
                "document_mode": "SKILL.md file mode", "name": "Name", "domain": "Domain"}
NOUNS = {PartKind.SECTION: "Section", PartKind.RULE: "Rule", PartKind.EXAMPLE: "Example"}


class PartReview(Record):
    part_id: NonEmpty
    kind: PartKind
    label: NonEmpty
    attention: Literal["decide", "safe", "automatic"]
    risks: tuple[RiskCode, ...] = ()
    default: Literal["use_upstream"] | None = None
    overridable: tuple[Literal["phone", "email"], ...] = ()
    findings: tuple[CheckFindingView, ...] = ()


class ReviewSummary(Record):
    decide: int
    safe: int
    automatic: int
    risks: dict[RiskCode, int]
    # Findings that name no part (a workspace rule, a live hold): they hold the whole skill.
    whole_skill: tuple[CheckFindingView, ...] = ()


@dataclass(frozen=True)
class PartCheck:
    findings: tuple[CheckFinding, ...]
    codes: tuple[str, ...]
    risks: frozenset[str]
    overridable: tuple[str, ...]

    @property
    def held(self) -> bool:
        return bool(self.findings or self.codes)


def part_findings(plan: MergePlan, change: Change) -> tuple[CheckFinding, ...]:
    path = change.part_id.removeprefix("file:") if change.kind == PartKind.FILE else None
    return tuple(dict.fromkeys(finding for finding in plan.check_findings
                               if finding.part_id == change.part_id or path is not None and finding.path == path))


def _unavailable(code: str) -> bool:
    return code == "text_check_limit" or code.endswith("_unavailable")


def part_check(plan: MergePlan, change: Change, conflict: Conflict | None) -> PartCheck:
    """The part's own check state, read from the plan's findings and the conflict's check codes."""
    findings = part_findings(plan, change)
    codes = tuple(code for code in (conflict.reason.split(",") if conflict is not None else ())
                  if code and code not in DECISION_REASONS)
    risks: set[str] = set()
    for finding in findings:
        if finding.category.startswith("injection:"):
            risks.add("injection")
        elif finding.category.startswith("check_unavailable:"):
            risks.add("check_unavailable")
        elif finding.category not in PERSONAL_DATA:
            risks.add("check_failed")
    if any(_unavailable(code) for code in codes):
        risks.add("check_unavailable")
    elif codes and not findings:
        risks.add("check_failed")
    if findings and not risks:
        risks.add("personal_data")
    # Ruling R6: only personal data alone, and only where the plan still allows taking it.
    overridable = (tuple(sorted({finding.category for finding in findings}))
                   if risks == {"personal_data"} and conflict is not None and personal_data_only(codes, findings)
                   and "use_upstream" in conflict.allowed_choices else ())
    return PartCheck(findings=findings, codes=codes, risks=frozenset(risks), overridable=overridable)


def _excerpt(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def part_label(change: Change, *, flagged: bool = False) -> str:
    if change.kind == PartKind.FILE:
        return change.part_id.removeprefix("file:")
    if change.kind == PartKind.FIELD:
        name = change.part_id.removeprefix("field:")
        return FIELD_LABELS.get(name, name.replace("_", " ").capitalize())
    noun = NOUNS.get(change.kind)
    if noun is None:
        return "Order of parts"
    if flagged:
        # The text may hold what the check matched; the label never quotes it (ruling R5).
        return f"{noun} with flagged text"
    value = next((side.value for side in (change.incoming, change.local, change.base)
                  if side.kind == "known" and isinstance(side.value, dict)), None)
    text = None
    if value is not None:
        text = value.get("heading") if change.kind == PartKind.SECTION else value.get("body")
        if change.kind == PartKind.EXAMPLE and isinstance(value.get("name"), str) and value["name"].strip():
            text = value["name"]
    return f"{noun}: {_excerpt(text)}" if isinstance(text, str) and text.strip() else noun


def _loses_nothing(change: Change, held_install: bool) -> bool:
    local = change.local
    return held_install or local.kind == "absent" or same_value(local, change.base) or same_value(local, change.incoming)


def _replaces_local(change: Change) -> bool:
    return (change.local.kind == "known" and change.incoming.kind != "absent"
            and not same_value(change.local, change.base) and not same_value(change.local, change.incoming))


def _script(change: Change) -> bool:
    if change.kind == PartKind.FILE:
        return script_change(change.part_id.removeprefix("file:"), change.base, change.local, change.incoming)
    return (change.part_id == "field:document_mode" and change.incoming.kind == "known"
            and change.incoming.value == 0o100755)


def review_parts(plan: MergePlan, *, held_install: bool) -> dict[str, PartReview]:
    conflicts = {conflict.part_id: conflict for conflict in plan.conflicts}
    placeholder = frozenset({"unknown_ownership"}) if held_install else frozenset()
    result: dict[str, PartReview] = {}
    for change in plan.changes:
        conflict = conflicts.get(change.part_id)
        check = part_check(plan, change, conflict)
        row = {"part_id": change.part_id, "kind": change.kind, "label": part_label(change, flagged=check.held),
               "findings": tuple(finding_view(finding) for finding in check.findings)}
        if conflict is None:
            result[change.part_id] = PartReview(**row, attention="automatic")
            continue
        reasons = {code for code in conflict.reason.split(",") if code in DECISION_REASONS}
        script = _script(change)
        if ("use_upstream" in conflict.allowed_choices and change.incoming.kind == "known" and not check.held
                and _loses_nothing(change, held_install) and not change.linked_removals and not script
                and reasons <= QUIET_REASONS | placeholder):
            result[change.part_id] = PartReview(**row, attention="safe", default="use_upstream")
            continue
        risks = set(check.risks)
        if (change.incoming.kind == "absent" and change.local.kind != "absent") or change.linked_removals \
                or "deletion_consent" in reasons:
            risks.add("removed_upstream")
        if not held_install and (reasons & LOCAL_REASONS or _replaces_local(change)):
            risks.add("your_edits")
        if reasons & (IDENTITY_REASONS - placeholder):
            risks.add("unclear_identity")
        if script:
            risks.add("script")
        if not risks:
            # Anything else OMS cannot judge: an unknown file mode, an unknown side (ruling R4).
            risks.add("unclear_identity")
        result[change.part_id] = PartReview(**row, attention="decide",
                                           risks=tuple(risk for risk in RISK_ORDER if risk in risks),
                                           overridable=check.overridable)
    return result


def whole_skill_findings(plan: MergePlan) -> tuple[CheckFinding, ...]:
    """Findings no part owns. SKILL.md's own hits are shown on the parts that hold them."""
    ids = {change.part_id for change in plan.changes}
    return distinct_findings(finding for finding in plan.check_findings
                             if finding.part_id not in ids
                             and (finding.path is None or finding.path != "SKILL.md" and "file:" + finding.path not in ids))


def review_summary(plan: MergePlan, parts: Mapping[str, PartReview]) -> ReviewSummary:
    actions = {change.part_id: change.action for change in plan.changes}
    # Ruling R8: a "keep" change writes nothing, so it is not counted.
    counted = Counter(row.attention for row in parts.values()
                      if not (row.attention == "automatic" and actions.get(row.part_id) == "keep"))
    risks = Counter(risk for row in parts.values() if row.attention == "decide" for risk in row.risks)
    return ReviewSummary(decide=counted["decide"], safe=counted["safe"], automatic=counted["automatic"],
                         risks={risk: risks[risk] for risk in RISK_ORDER if risks[risk]},
                         whole_skill=tuple(finding_view(finding) for finding in whole_skill_findings(plan)))
