"""Which parts of a review card need a person, and why (spec section 4, rulings R1 to R5)."""
from oms.domain.identity import SkillRef
from oms.sources.models import (Change, CheckFinding, Conflict, Evidence, MergePlan, OriginRef, PartKind,
                                PlanFingerprint, SnapshotRef)
from oms.sources.triage import part_label, review_parts, review_summary
from tests.sources.merge_cases import file as manifest_file, merge_case, part
from tests.sources.test_api import source_api  # noqa: F401

SKILL = SkillRef("acme", "invoices")
ORIGIN = OriginRef(skill=SKILL, origin_id="origin", kind="github", generation=1)
FINGERPRINT = PlanFingerprint(content_generation=1, binding_generation=1, update_generation=1,
                              policy_version="1", local_digest="local", candidate_digest="incoming")
BASE = SnapshotRef(snapshot_id="base", origin=ORIGIN)
ABSENT = Evidence(kind="absent", policy_version="1")
UNKNOWN = Evidence(kind="unknown", policy_version="1")
PROSE = ("keep_oms", "use_upstream", "merged_text")


def known(value):
    return Evidence(kind="known", value=value, policy_version="1")


def file_value(path, mode=0o100644):
    return {"path": path, "digest": "d-" + path, "size": 10, "mode": mode}


def change(part_id, kind, *, base=UNKNOWN, local=ABSENT, incoming=None, action="conflict", linked=()):
    if incoming is None:
        incoming = known(file_value(part_id.removeprefix("file:")) if kind == PartKind.FILE else {"body": "Text"})
    return Change(part_id=part_id, kind=kind, action=action, base=base, local=local, incoming=incoming,
                  linked_removals=linked)


def conflict(part_id, reason="unknown_base", allowed=("keep_oms", "use_upstream")):
    return Conflict(part_id=part_id, reason=reason, allowed_choices=allowed)


def plan(changes, conflicts=(), findings=(), *, base=None):
    return MergePlan(skill=SKILL, origin=ORIGIN, base=base, incoming=SnapshotRef(snapshot_id="incoming", origin=ORIGIN),
                     fingerprint=FINGERPRINT, changes=tuple(changes), conflicts=tuple(conflicts),
                     check_findings=tuple(findings))


def test_a_held_install_asks_only_about_the_flagged_file():
    # Review Focus 1: the held skill's empty placeholders lose nothing (ruling R1).
    phone = tuple(CheckFinding(path="visual-companion.md", line=line, category="phone") for line in (263, 264, 265))
    held = plan(
        [change("field:description", PartKind.FIELD, local=known(""), incoming=known("Use when designing")),
         change("field:tags", PartKind.FIELD, local=known([]), incoming=known(["design"])),
         change("field:document_mode", PartKind.FIELD, local=UNKNOWN, incoming=known(0o100644)),
         change("field:name", PartKind.FIELD, local=known("brainstorming"), incoming=known("brainstorming"),
                action="keep"),
         change("section:overview", PartKind.SECTION, incoming=known({"heading": "Overview", "body": "Text"})),
         change("rule:always", PartKind.RULE, incoming=known({"body": "Always write the design down first."})),
         change("file:visual-companion.md", PartKind.FILE)],
        [conflict("field:description", "unknown_base,unknown_ownership"),
         conflict("field:tags", "unknown_base,unknown_ownership"),
         conflict("field:document_mode", "unknown_base,unknown_ownership"),
         conflict("section:overview", allowed=PROSE), conflict("rule:always", allowed=PROSE),
         conflict("file:visual-companion.md", "unknown_base,source_text_hold")],
        phone)
    parts = review_parts(held, held_install=True)
    assert {key: row.attention for key, row in parts.items()} == {
        "field:description": "safe", "field:tags": "safe", "field:document_mode": "safe",
        "field:name": "automatic", "section:overview": "safe", "rule:always": "safe",
        "file:visual-companion.md": "decide"}
    assert parts["rule:always"].default == "use_upstream" and parts["rule:always"].risks == ()
    flagged = parts["file:visual-companion.md"]
    assert flagged.risks == ("personal_data",) and flagged.default is None
    assert flagged.overridable == ("phone",)
    assert flagged.label == "visual-companion.md"
    assert [(row.line, row.category) for row in flagged.findings] == [(263, "phone"), (264, "phone"), (265, "phone")]
    summary = review_summary(held, parts)
    assert (summary.decide, summary.safe, summary.automatic) == (1, 5, 0)
    assert summary.risks == {"personal_data": 1} and summary.whole_skill == ()


def test_the_same_placeholders_on_an_ordinary_update_are_the_readers_own():
    ordinary = plan([change("field:description", PartKind.FIELD, local=known(""), incoming=known("Use when designing"))],
                    [conflict("field:description", "unknown_base,unknown_ownership")])
    row = review_parts(ordinary, held_install=False)["field:description"]
    assert row.attention == "decide" and row.risks == ("your_edits", "unclear_identity")


def test_an_ordinary_update_takes_what_loses_nothing_and_asks_about_local_edits():
    old, new = known({"heading": "Steps", "body": "Old"}), known({"heading": "Steps", "body": "New"})
    mine = known({"heading": "Steps", "body": "Mine"})
    update = plan(
        [change("section:edited", PartKind.SECTION, base=old, local=mine, incoming=new),
         change("section:moved", PartKind.SECTION, base=old, local=old, incoming=new),
         change("section:clean", PartKind.SECTION, base=old, local=old, incoming=new, action="replace"),
         change("field:description", PartKind.FIELD, base=known("A"), local=known("A"), incoming=known("A"),
                action="keep")],
        [conflict("section:edited", "local_divergence", PROSE), conflict("section:moved", "changed_order", PROSE)],
        base=BASE)
    parts = review_parts(update, held_install=False)
    assert (parts["section:edited"].attention, parts["section:edited"].risks) == ("decide", ("your_edits",))
    assert parts["section:moved"].attention == "safe"
    assert parts["section:clean"].attention == "automatic"
    summary = review_summary(update, parts)
    assert (summary.decide, summary.safe, summary.automatic) == (1, 1, 1)


def test_removals_and_replacements_need_consent():
    kept = known({"body": "Keep receipts."})
    update = plan(
        [change("rule:gone", PartKind.RULE, base=kept, local=kept, incoming=ABSENT),
         change("rule:new", PartKind.RULE, base=ABSENT, local=ABSENT, incoming=known({"body": "Keep all receipts."}),
                linked=("rule:gone",))],
        [conflict("rule:gone", "deletion_consent", PROSE), conflict("rule:new", "ambiguous_replacement", PROSE)],
        base=BASE)
    parts = review_parts(update, held_install=False)
    assert parts["rule:gone"].risks == ("removed_upstream",)
    assert parts["rule:new"].risks == ("removed_upstream", "unclear_identity")
    assert {row.attention for row in parts.values()} == {"decide"}


def test_scripts_are_confirmed_even_when_they_pass():
    update = plan(
        [change("file:scripts/run.sh", PartKind.FILE),
         change("file:tool", PartKind.FILE, incoming=known(file_value("tool", 0o100755))),
         change("file:notes.md", PartKind.FILE),
         change("field:document_mode", PartKind.FIELD, local=UNKNOWN, incoming=known(0o100755))],
        [conflict("file:scripts/run.sh"), conflict("file:tool"), conflict("file:notes.md"),
         conflict("field:document_mode", "unknown_base,unknown_ownership")])
    parts = review_parts(update, held_install=True)
    assert parts["file:scripts/run.sh"].risks == ("script",)
    assert parts["file:tool"].risks == ("script",)
    assert parts["field:document_mode"].risks == ("script",)
    assert parts["file:notes.md"].attention == "safe"


def test_only_personal_data_alone_can_be_overridden():
    def flagged(path, *categories, reason="unknown_base,source_text_hold", allowed=("keep_oms", "use_upstream")):
        return (change("file:" + path, PartKind.FILE), conflict("file:" + path, reason, allowed),
                tuple(CheckFinding(path=path, line=index + 1, category=category)
                      for index, category in enumerate(categories)))
    rows = [flagged("mixed.md", "phone", "injection:instruction-override"),
            flagged("both.md", "phone", "email"),
            flagged("withdrawn.md", "phone", allowed=("keep_oms",)),
            flagged("big.bin", "file_size", reason="unknown_base,file_size_limit"),
            flagged("rule.md", "policy:source_constraint_conflict", reason="unknown_base,source_constraint_conflict"),
            flagged("bare.md", reason="unknown_base,source_constraint_conflict"),
            flagged("cut.md", "phone", reason="unknown_base,package_custody_unavailable"),
            flagged("unchecked.md", reason="unknown_base,file_check_unavailable")]
    update = plan([row[0] for row in rows], [row[1] for row in rows], [f for row in rows for f in row[2]])
    parts = review_parts(update, held_install=True)
    assert (parts["file:mixed.md"].risks, parts["file:mixed.md"].overridable) == (("injection",), ())
    assert (parts["file:both.md"].risks, parts["file:both.md"].overridable) == (("personal_data",), ("email", "phone"))
    assert (parts["file:withdrawn.md"].risks, parts["file:withdrawn.md"].overridable) == (("personal_data",), ())
    assert parts["file:big.bin"].risks == ("check_failed",)
    assert parts["file:rule.md"].risks == ("check_failed",)
    assert parts["file:bare.md"].risks == ("check_failed",)
    assert (parts["file:cut.md"].risks, parts["file:cut.md"].overridable) == (("check_unavailable",), ())
    assert parts["file:unchecked.md"].risks == ("check_unavailable",)
    assert all(row.attention == "decide" and row.default is None for row in parts.values())


def test_a_part_finding_is_matched_by_its_part_id():
    # Review Focus 4: the label of a flagged rule never quotes its text.
    rule = change("rule:phone", PartKind.RULE, incoming=known({"body": "Call +44 20 7946 0958 about receipts."}))
    update = plan([rule], [conflict("rule:phone", "unknown_base,source_text_hold", PROSE)],
                  [CheckFinding(path="SKILL.md", line=7, category="phone"),
                   CheckFinding(part_id="rule:phone", line=1, category="phone")])
    parts = review_parts(update, held_install=True)
    row = parts["rule:phone"]
    assert row.risks == ("personal_data",) and row.overridable == ("phone",)
    assert row.label == "Rule with flagged text"
    assert "7946" not in row.model_dump_json()
    assert review_summary(update, parts).whole_skill == ()


def test_labels_are_plain_and_quote_only_unflagged_text():
    long = "Always write the design down before you start coding it, and keep it short."
    assert part_label(change("field:description", PartKind.FIELD)) == "Description"
    assert part_label(change("field:license", PartKind.FIELD)) == "Licence"
    assert part_label(change("field:document_mode", PartKind.FIELD)) == "SKILL.md file mode"
    assert part_label(change("field:custom_thing", PartKind.FIELD)) == "Custom thing"
    assert part_label(change("file:docs/a.md", PartKind.FILE)) == "docs/a.md"
    assert part_label(change("section:o", PartKind.SECTION, incoming=known({"heading": "Overview", "body": "x"}))) == (
        "Section: Overview")
    assert part_label(change("rule:l", PartKind.RULE, incoming=known({"body": long}))) == (
        "Rule: Always write the design down before you start coding it, an…")
    assert part_label(change("example:e", PartKind.EXAMPLE, incoming=known({"body": "Attach the receipt.",
                                                                           "name": None}))) == (
        "Example: Attach the receipt.")
    assert part_label(change("rule:l", PartKind.RULE, incoming=known({"body": long})), flagged=True) == (
        "Rule with flagged text")


def test_a_hold_that_names_no_part_is_reported_for_the_whole_skill():
    # Review Focus 2 (ruling R3).
    update = plan([change("rule:a", PartKind.RULE)], [conflict("rule:a", allowed=PROSE)],
                  [CheckFinding(category="policy:source_constraint_conflict"),
                   CheckFinding(path="SKILL.md", line=3, category="phone"),
                   CheckFinding(path="gone.md", line=1, category="email")])
    parts = review_parts(update, held_install=True)
    assert parts["rule:a"].attention == "safe"
    assert [(row.category, row.summary) for row in review_summary(update, parts).whole_skill] == [
        ("policy:source_constraint_conflict", "Conflicts with a workspace safety rule (source constraint conflict)"),
        ("email", "gone.md, line 1: looks like an email address")]


def test_the_plan_keeps_each_parts_own_findings():
    # Ruling R2: a part's findings stay on the plan, and a file check's findings name the file.
    changed = merge_case(part("section:s", "normal", kind="section")).plan(
        incoming=merge_case(part("section:s", "Call +44 20 7946 0958 about receipts.", kind="section")).incoming)
    assert CheckFinding(part_id="section:s", line=1, category="phone") in changed.check_findings
    case = merge_case(manifest=(manifest_file("notes.md"),))
    with_file = case.change_file("notes.md", b"Call +44 20 7946 0958.")
    assert CheckFinding(path="notes.md", line=1, category="phone") in with_file.check_findings


def test_the_update_view_carries_parts_and_a_summary():
    from oms.sources.api_models import UpdateView
    from oms.sources.models import Update
    held = plan([change("rule:a", PartKind.RULE)], [conflict("rule:a", allowed=PROSE)],
                [CheckFinding(category="policy:fixture")])
    view = UpdateView(**Update(update_id="u", plan=held, generation=1, status="open", review_item_id="r").model_dump(),
                      stale=False, ownership={}, part_fingerprints={}, held_install=True)
    dumped = view.model_dump(mode="json")
    assert dumped["parts"]["rule:a"]["attention"] == "safe"
    assert dumped["parts"]["rule:a"]["default"] == "use_upstream"
    assert dumped["summary"]["safe"] == 1
    assert dumped["summary"]["whole_skill"][0]["summary"] == "Conflicts with a workspace safety rule (fixture)"


def test_the_card_route_returns_parts_and_a_summary(source_api):
    client, world, _ = source_api
    world.policy.held_skills = {"invoices"}
    world.install_two()
    update = world.sources.open_update(SKILL)
    body = client.get("/api/skill-updates/" + update.update_id).json()
    assert set(body["parts"]) == {row.part_id for row in update.plan.changes}
    assert body["parts"]["field:description"]["attention"] == "safe"
    assert body["parts"]["field:description"]["default"] == "use_upstream"
    assert body["summary"]["decide"] == 0
    assert [row["category"] for row in body["summary"]["whole_skill"]] == ["policy:fixture"]
