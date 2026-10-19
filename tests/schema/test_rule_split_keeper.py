"""Which skill keeps a shared rule's id, and what happens to each open item (spec 8.3, 8.5; plan D2, D4)."""
from datetime import datetime, timezone

import pytest

from oms.domain.models import ReviewItem, Skill, Transaction
from oms.domain.types import SignalType, SourceRuntime, Verdict
from oms.schema.metadata import SchemaCompatibilityError
from oms.schema.migrations.rule_split import _earliest, _Resolvers, choose_keeper, item_treatment


def _skill(skill_id, name=None, source=None):
    return Skill(id=skill_id, name=name or skill_id.title(), description="", domain="comms",
                 tenant_id="acme", import_source_ref=source)


SKILLS = [_skill("press", "Press office"), _skill("email", source="skills/email/SKILL.md"), _skill("blog")]


def test_the_skill_the_first_contribution_named_keeps_the_rule():
    assert choose_keeper(SKILLS, hint="press office", import_source_ref=None,
                         source_claimed=frozenset({"blog"})) == ("press", "skill_hint")
    assert choose_keeper(SKILLS, hint="EMAIL", import_source_ref=None, source_claimed=frozenset()) == ("email", "skill_hint")


def test_an_imported_sentence_stays_with_the_skill_whose_file_it_is():
    assert choose_keeper(SKILLS, hint=None, import_source_ref="skills/email/SKILL.md",
                         source_claimed=frozenset({"blog"})) == ("email", "import")


def test_the_one_skill_a_source_tracks_it_in_keeps_it():
    assert choose_keeper(SKILLS, hint="no such skill", import_source_ref="skills/other/SKILL.md",
                         source_claimed=frozenset({"press"})) == ("press", "source")


def test_otherwise_the_lowest_storage_key_keeps_it():
    assert choose_keeper(SKILLS, hint=None, import_source_ref=None,
                         source_claimed=frozenset({"press", "email"})) == ("blog", "lowest_key")


def test_with_no_skill_to_keep_it_the_choice_is_refused_by_name():
    # A damaged graph: none of a shared rule's skills has a Skill node, so
    # there is nothing to choose from. A named refusal, never an IndexError.
    with pytest.raises(SchemaCompatibilityError, match="none of its skills exists"):
        choose_keeper([], hint="press office", import_source_ref=None, source_claimed=frozenset())


def _item(kind, **fields):
    values = dict(id=f"{kind}-1", kind=kind, subject_id="rule-r", other_id=None, verdict=Verdict.AMBIGUOUS,
                  reason="", tenant_id="acme")
    values.update(fields)
    return ReviewItem(**values)


SKILLS_OF = {"rule-blog": frozenset({"blog"}), "rule-press": frozenset({"press"}),
             "rule-both": frozenset({"press", "blog"})}


def _treat(item, keeper="press", copies=("blog", "email")):
    return item_treatment(item, rule_id="rule-r", keeper=keeper, copy_skills=frozenset(copies),
                          skills_of=lambda rule_id: SKILLS_OF.get(rule_id, frozenset()),
                          is_constraint=lambda node_id: node_id.startswith("constraint-"),
                          section_skill=lambda section_id: section_id.removesuffix("-rules"),
                          block_skill=lambda block_id: "blog" if block_id == "block-blog" else "press",
                          import_skill=lambda txn_id: "email" if txn_id == "t-import-email" else None)


def test_holds_are_copied_to_every_copy():
    for kind in ("pending_rule", "injection", "control_plane"):
        assert _treat(_item(kind)) == {"blog": "copy", "email": "copy"}
    assert _treat(_item("conflict", other_id="constraint-acme-plain")) == {"blog": "copy", "email": "copy"}


def test_a_withdrawn_repeat_is_copied_only_where_its_successor_lives():
    assert _treat(_item("withdrawn_repeat", other_id="rule-blog")) == {"blog": "copy", "email": "pending"}


def test_a_relation_follows_its_counterpart_unless_the_keeper_shares_it():
    assert _treat(_item("conflict", other_id="rule-blog")) == {"blog": "move"}
    assert _treat(_item("supersession", subject_id="rule-blog", other_id="rule-r")) == {"blog": "move"}
    assert _treat(_item("conflict", other_id="rule-press")) == {}
    assert _treat(_item("conflict", other_id="rule-both")) == {}


def test_document_items_follow_their_skill():
    assert _treat(_item("block_conflict", other_id="block-blog")) == {"blog": "move"}
    assert _treat(_item("block_revision", subject_id="block-1", rule_id="rule-r", other_id="email-rules")) == {"email": "move"}
    assert _treat(_item("proposed_edit", subject_id="blog", other_id="rule:rule-r")) == {"blog": "move"}
    assert _treat(_item("proposed_edit", subject_id="press", other_id="rule:rule-r")) == {}


def test_import_items_follow_the_skill_that_imported_them():
    assert _treat(_item("removal", transaction_id="t-import-email")) == {"email": "move"}
    assert _treat(_item("polarity_conflict", id="polarity-rule-r-t-import-email")) == {"email": "move"}
    assert _treat(_item("removal", transaction_id="t-unknown")) == {}


def test_assignments_and_divergence_are_left_to_their_own_handling():
    assert _treat(_item("skill_assignment", candidate_skill_ids=["press", "blog"])) == {}
    assert _treat(_item("cross_skill_divergence", other_id="rule-blog")) == {}


def test_an_item_no_row_covers_stays_as_it_is():
    # A withdrawn repeat whose successor is the shared rule: the table covers
    # the withdrawn rule's side only.
    assert _treat(_item("withdrawn_repeat", subject_id="rule-blog", other_id="rule-r")) == {}


def _txn(txn_id, day, *, tenant="acme", source_ref=None):
    return Transaction(id=txn_id, signal_type=SignalType.EXPLICIT_CORRECTION, source_runtime=SourceRuntime.MCP,
                       sanitised_payload_ref=f"{txn_id}.json", timestamp=datetime(2026, 9, day, tzinfo=timezone.utc),
                       tenant_id=tenant, source_ref=source_ref)


def test_the_first_contribution_is_the_earliest_of_the_rules_own_tenant_and_ties_break_on_id():
    lineage = [_txn("t-other", 1, tenant="beta"), _txn("t-b", 2), _txn("t-a", 2), _txn("t-c", 3)]
    assert _earliest(lineage, "acme").id == "t-a"
    assert _earliest(lineage[:1], "acme") is None


class _Store:
    """The two reads an import item needs, over fixed data."""
    def skills_for_tenant(self, tenant_id):
        return [_skill("email", source="skills/email/SKILL.md"), _skill("zeta", source="skills/email/SKILL.md")]

    def get_transaction(self, transaction_id):
        return {"t-acme": _txn("t-acme", 1, source_ref="skills/email/SKILL.md"),
                "t-beta": _txn("t-beta", 1, tenant="beta", source_ref="skills/email/SKILL.md")}.get(transaction_id)


def test_an_import_item_follows_only_its_own_tenants_transaction_to_one_skill():
    resolvers = _Resolvers(_Store(), "acme")
    assert resolvers.import_skill("t-acme") == "email"
    assert resolvers.import_skill("t-beta") is None
    assert resolvers.import_skill("t-missing") is None
