"""A part kept after a personal-data review does not hold the skill on every later check (ruling F1).

The whole-package screen finds the personal data again on each check of the same commit. When every
finding is personal data and every part it names stays as it is, the plan changes nothing that was
flagged, so the screen's hold is released. Any other finding, or a finding on a part that changes,
still holds.
"""
from dataclasses import replace
from hashlib import sha256

from oms.sources.models import (ApplyRequest, BulkApplyRequest, CheckFinding, CheckRequest, CheckResult, DraftChoice,
                                ManifestEntry, PlanFlag)
from oms.sources.review import part_fingerprint
from oms.sources.safety import eligibility
from tests.sources.merge_cases import file, merge_case, part
from tests.sources.test_community_safety import community_world  # noqa: F401
from tests.sources.test_personal_data_override import INVOICES, PHONE, _request, _save, _take_everything, _with_file

FLAGGED = "file:visual-companion.md"


def _held(*findings: CheckFinding, reasons: tuple[str, ...] = ("text_requires_sanitisation",)) -> CheckResult:
    return CheckResult(state="held", code="source_text_hold", reasons=reasons, findings=findings)


def _plan(check: CheckResult, *, incoming_rule: str = "Keep receipts.", incoming_file: bytes = PHONE):
    case = merge_case(part("rule:r", {"body": "Keep receipts.", "section_id": None}, kind="rule"),
                      manifest=(file("a.md", PHONE),))
    incoming = merge_case(part("rule:r", {"body": incoming_rule, "section_id": None}, kind="rule"),
                          manifest=(file("a.md", incoming_file),)).incoming
    return case.plan(incoming=incoming, policy=replace(case.policy, required_checks=(check,)))


def test_personal_data_on_a_kept_file_does_not_hold_the_plan():
    plan = _plan(_held(CheckFinding(path="a.md", line=1, category="phone")))
    assert plan.flags == () and plan.conflicts == () and plan.check_findings == ()
    assert all(change.action == "keep" for change in plan.changes)


def test_personal_data_on_a_kept_file_does_not_hold_a_later_rule_change():
    plan = _plan(_held(CheckFinding(path="a.md", line=1, category="phone")), incoming_rule="Keep every receipt.")
    assert PlanFlag.SAFETY_HOLD not in plan.flags
    assert {change.part_id: change.action for change in plan.changes} == {"file:a.md": "keep", "rule:r": "replace"}


def test_a_finding_on_a_changing_part_still_holds():
    plan = _plan(_held(CheckFinding(path="a.md", line=1, category="phone")), incoming_file=b"Call +44 20 7946 0959.\n")
    assert PlanFlag.SAFETY_HOLD in plan.flags
    assert CheckFinding(path="a.md", line=1, category="phone") in plan.check_findings


def test_any_other_finding_on_a_kept_part_still_holds():
    for check in (
            _held(CheckFinding(path="a.md", line=1, category="injection:instruction-override"),
                  reasons=("injection_screen_hold",)),
            _held(CheckFinding(path="a.md", line=1, category="phone"),
                  CheckFinding(path="a.md", line=2, category="injection:instruction-override"),
                  reasons=("text_requires_sanitisation", "injection_screen_hold")),
            _held(CheckFinding(path="a.md", line=1, category="phone"), reasons=("findings_truncated",)),
            CheckResult(state="held", code="source_constraint_conflict"),
            CheckResult(state="held", code="file_size_limit", findings=(CheckFinding(path="a.md", category="file_size"),))):
        assert PlanFlag.SAFETY_HOLD in _plan(check).flags, check


def test_a_finding_that_names_no_kept_part_still_holds():
    for finding in (CheckFinding(category="phone"), CheckFinding(path="other.md", line=1, category="phone"),
                    CheckFinding(part_id="rule:gone", line=1, category="phone")):
        assert PlanFlag.SAFETY_HOLD in _plan(_held(finding)).flags, finding


def test_a_skill_md_finding_counts_through_the_part_that_holds_it():
    in_rule = CheckFinding(part_id="rule:r", line=1, category="phone")
    in_skill = CheckFinding(path="SKILL.md", line=7, category="phone")
    assert PlanFlag.SAFETY_HOLD not in _plan(_held(in_skill, in_rule)).flags
    # A SKILL.md hit no kept part accounts for is not known to be on a kept part.
    assert PlanFlag.SAFETY_HOLD in _plan(_held(in_skill)).flags
    assert PlanFlag.SAFETY_HOLD in _plan(_held(in_skill, in_rule.model_copy(update={"category": "email"}))).flags
    # The part that holds it is changing, so the hold stays.
    assert PlanFlag.SAFETY_HOLD in _plan(_held(in_skill, in_rule), incoming_rule="Call +44 20 7946 0958.").flags


def _serve_again(world, *, description: str = "Invoices description", revision: str = "a"):
    """Serve the installed package again, at the same commit or a later one, with the flagged file unchanged."""
    binding = world.sources.get_binding(INVOICES)
    package = world.package(revision, description).model_copy(update={"package_path": "invoices"})
    entry = ManifestEntry(path="visual-companion.md", blob_ref=world.blobs.put(PHONE), digest=sha256(PHONE).hexdigest(),
                          size=len(PHONE), mode=0o100644)
    world.incoming = package.model_copy(update={
        "manifest": (*package.manifest, entry), "history_evidence": "proven_ancestor",
        "resolved_ref": binding.ref.model_copy(update={"commit": revision * 40})})


def _check(world, key: str):
    binding = world.sources.get_binding(INVOICES)
    source = world.sources.get_source(binding.source_id, tenant_id="acme")
    return world.service.check(world.context, CheckRequest(source_id=source.source_id,
        expected_source_generation=source.generation, skills=(INVOICES,)), idempotency_key=key)


def _taken_anyway(world):
    update = _take_everything(world, _with_file(world), "acknowledged")
    assert world.service.apply(world.context, ApplyRequest(**_request(update).model_dump()),
                               idempotency_key="apply-acknowledged").state == "complete"


def _left_out(world):
    held = _with_file(world)
    choices = tuple(DraftChoice(part_id=row.part_id, choice="keep_oms" if row.part_id == FLAGGED else "use_upstream",
                                part_fingerprint=part_fingerprint(held, row.part_id))
                    for row in held.plan.conflicts if "use_upstream" in row.allowed_choices)
    update = _save(world, held, choices, "left-out")
    assert world.service.apply(world.context, ApplyRequest(**_request(update).model_dump()),
                               idempotency_key="apply-left-out").state == "complete"


def _flagged_file(world) -> bytes | None:
    binding = world.sources.get_binding(INVOICES)
    local = world.sources.get_snapshot(binding.baseline)
    entry = next((row for row in local.manifest if row.path == "visual-companion.md"), None)
    return entry.digest if entry else None


def test_a_check_of_the_same_commit_after_taking_it_anyway_is_unchanged(community_world):
    world = community_world
    _taken_anyway(world)
    _serve_again(world)
    result = _check(world, "same-commit")
    assert [row.state for row in result.outcomes] == ["unchanged"]
    assert world.sources.open_update(INVOICES) is None


def test_a_check_of_the_same_commit_after_leaving_it_out_is_unchanged(community_world):
    world = community_world
    _left_out(world)
    _serve_again(world)
    result = _check(world, "same-commit")
    assert [row.state for row in result.outcomes] == ["unchanged"]
    assert world.sources.open_update(INVOICES) is None


def test_a_later_rule_change_is_eligible_for_unattended_application(community_world):
    world = community_world
    _taken_anyway(world)
    kept = _flagged_file(world)
    _serve_again(world, description="Invoices description, revised", revision="b")
    result = _check(world, "later-change")
    update = world.sources.get_update(result.outcomes[0].update_id, tenant_id="acme")
    assert PlanFlag.SAFETY_HOLD not in update.plan.flags and update.plan.conflicts == ()
    assert eligibility(update.plan, mode="automatic", automatic_enabled=True, edition="paid").state == "passed"
    applied = world.service.bulk_apply(world.context, BulkApplyRequest(updates=(ApplyRequest(
        **_request(update).model_dump()),)), idempotency_key="bulk-later")
    assert applied.committed
    assert world.store.get_skill("invoices", tenant_id="acme").description == "Invoices description, revised"
    assert _flagged_file(world) == kept


def test_a_later_change_to_the_flagged_file_still_holds(community_world):
    world = community_world
    _taken_anyway(world)
    _serve_again(world, revision="b")
    body = b"Call +44 20 7946 0959 for help.\n"
    world.incoming = world.incoming.model_copy(update={"manifest": tuple(
        row.model_copy(update={"blob_ref": world.blobs.put(body), "digest": sha256(body).hexdigest(), "size": len(body)})
        if row.path == "visual-companion.md" else row for row in world.incoming.manifest)})
    result = _check(world, "flagged-change")
    update = world.sources.get_update(result.outcomes[0].update_id, tenant_id="acme")
    assert PlanFlag.SAFETY_HOLD in update.plan.flags
    assert FLAGGED in {row.part_id for row in update.plan.conflicts}
