"""Copy ids (one rule, one skill, spec 4.2 and plan decision D1)."""
import dataclasses
import hashlib
from collections.abc import Callable
from datetime import datetime, timezone

import pytest
from neo4j import Driver

from oms.activity.models import AdminAction
from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.copies import (
    COPY_CONFIDENCE, COPY_KIND, CopyCause, RuleSlot, copy_example_id, copy_rule_id,
    created_by_transaction, first_rule_id, learning_copy_id, split_rule_id,
)
from oms.domain.identity import SkillRef
from oms.domain.models import Edge, Rule, Skill
from oms.domain.types import EdgeType, SkillVersionCause
from oms.ports.graph_store import RuleOwnershipError
from oms.sources.mutation import content_digest
from oms.sources.undo import record
from tests.community.test_critic_workflow import critic_neo4j_driver  # noqa: F401

EMAIL, BLOG = SkillRef("acme", "email"), SkillRef("acme", "blog")
CopyId = Callable[[str, SkillRef], str]


def test_the_compiler_and_community_ids_are_the_spec_ids() -> None:
    assert first_rule_id("t1") == "rule-t1"
    expected = "rule-t1-" + hashlib.sha256(BLOG.storage_key.encode("utf-8")).hexdigest()[:8]
    assert learning_copy_id("t1", BLOG) == expected


@pytest.mark.parametrize("make", [copy_rule_id, split_rule_id])
def test_a_copy_id_is_deterministic_and_differs_per_skill_and_source(make: CopyId) -> None:
    assert make("rule-a", BLOG) == make("rule-a", BLOG)
    assert make("rule-a", BLOG) != make("rule-a", EMAIL)
    assert make("rule-a", BLOG) != make("rule-b", BLOG)
    assert make("rule-a", BLOG) != make("rule-a", SkillRef("beta", "blog"))


@pytest.mark.parametrize("make,marker", [(copy_rule_id, "-copy-"), (split_rule_id, "-split-")])
def test_a_copy_keeps_its_source_id_as_its_prefix(make: CopyId, marker: str) -> None:
    source = "rule-import-0123456789abcdef"
    copy = make(source, BLOG)
    assert copy.startswith(source + marker) and len(copy) == len(source) + len(marker) + 12


@pytest.mark.parametrize("make", [copy_rule_id, split_rule_id])
def test_a_copy_sorts_exactly_where_its_source_sorted(make: CopyId) -> None:
    # Against another rule of the skill, a copy compares as its source did,
    # with one exception that the next test pins: a rule whose id is the source
    # id plus a suffix that sorts below the copy's own suffix. None of these
    # neighbours is one. The ids OMS makes itself cannot be one either:
    # rule-import- plus 16 hex digits, source-rule- plus a digest and rule-
    # plus a generated uuid each have one length, and the copies of one
    # learning sit in different skills. Only a transaction id that a caller
    # chose can, when it extends another transaction's id with a hyphen
    # (rule-t2 and rule-t2-abc).
    neighbours = ["rule-import-0000000000000000", "rule-import-ffffffffffffffff", "rule-a1b2",
                  "rule-t1", "rule-t1x", "rule-zzz", "source-rule-" + "9" * 64, "example-x"]
    for source in ("rule-import-7777777777777777", "rule-t1", "rule-m", "source-rule-" + "5" * 64):
        copy = make(source, BLOG)
        for other in neighbours:
            if other != source:
                assert (other < source) == (other < copy), (source, other)


# A sibling whose id is "rule-t2" plus the suffix, and whether it swaps places with the copy of
# rule-t2: it does when the suffix sorts below the copy's own suffix (the marker, then 12 hex
# digits). The three that swap have a character below "c" after the hyphen, so they swap for
# both markers. The three that do not swap sort above both.
SIBLING_SUFFIXES: list[tuple[str, bool]] = [
    ("-abc", True), ("-2", True), ("-Z", True), ("-zzz", False), ("x", False), (".1", False)]


@pytest.mark.parametrize("make", [copy_rule_id, split_rule_id])
@pytest.mark.parametrize("suffix,swaps", SIBLING_SUFFIXES)
def test_the_sort_claim_has_one_limit_a_sibling_that_extends_the_source_id(
        make: CopyId, suffix: str, swaps: bool) -> None:
    # D1's condition, stated as a test. "A copy sorts where its source sorted" holds while no
    # rule of the same skill has an id that is the source id plus a suffix sorting below the
    # copy's own suffix. Such a rule sorted after the source and now sorts before the copy, so
    # two tied rules (equal count, or equal placement order) would publish in the other order.
    # Callers choose transaction ids, so "t2" and "t2-abc" can both exist in one skill.
    source = "rule-t2"
    sibling = source + suffix
    copy = make(source, BLOG)
    assert (source < sibling < copy) is swaps
    assert (source < copy < sibling) is not swaps


def test_the_ids_never_change_between_releases() -> None:
    # The split stores these ids, and a retried split or compile must find the copy it made,
    # so the hash inputs, their order and the lengths are fixed. The values were computed
    # with hashlib alone: sha256 over the parts joined by the unit separator, storage key last.
    assert BLOG.storage_key == '["acme","blog"]'
    assert learning_copy_id("t1", BLOG) == "rule-t1-2ad0243b"
    assert copy_rule_id("rule-t1", BLOG) == "rule-t1-copy-95e0ef614e77"
    assert split_rule_id("rule-t1", BLOG) == "rule-t1-split-95e0ef614e77"
    assert (copy_example_id("example-aaaaaaaaaaaaaaaa", "rule-t1-split-95e0ef614e77")
            == "example-aaaaaaaaaaaaaaaa-834f913c3168")


def test_a_copied_example_keeps_its_source_order() -> None:
    copy = split_rule_id("rule-t1", BLOG)
    first, second = "example-aaaaaaaaaaaaaaaa", "example-bbbbbbbbbbbbbbbb"
    assert copy_example_id(first, copy).startswith(first + "-")
    assert copy_example_id(first, copy) < copy_example_id(second, copy)
    assert copy_example_id(first, copy) != copy_example_id(first, split_rule_id("rule-t1", EMAIL))


def test_created_by_transaction_knows_every_copy_a_transaction_made() -> None:
    made = ["rule-t1", learning_copy_id("t1", BLOG), split_rule_id("rule-t1", EMAIL),
            split_rule_id(learning_copy_id("t1", BLOG), EMAIL), copy_rule_id("rule-t1", BLOG)]
    assert all(created_by_transaction(rule_id, "t1") for rule_id in made)
    for other in ("rule-t10", "rule-import-abc", "rule-t1-xyz", "rule-t1-split-short", "rule-t2"):
        assert not created_by_transaction(other, "t1"), other


def test_created_by_transaction_wants_the_exact_hash_lengths_in_lower_case() -> None:
    for other in ("rule-t1-0123456", "rule-t1-012345678", "rule-t1-0123ABCD",
                  "rule-t1-split-0123456789a", "rule-t1-split-0123456789abc",
                  "rule-t1-copy-0123456789AB", "rule-t1-01234567-split-0123456789",
                  "rule-t1-split-0123456789ab-"):
        assert not created_by_transaction(other, "t1"), other
    for other in ("rule-t1-01234567", "rule-t1-01234567-copy-0123456789ab",
                  "rule-t1-split-0123456789ab-copy-0123456789ab"):
        assert created_by_transaction(other, "t1"), other


def test_the_vocabulary_values_are_the_stored_strings() -> None:
    assert COPY_KIND == "copy"
    assert COPY_CONFIDENCE == 1.0
    assert [cause.value for cause in CopyCause] == ["learning", "manual", "review", "split"]


def test_a_slot_is_a_frozen_place_in_a_section_with_an_optional_order_and_group() -> None:
    slot = RuleSlot("blog-rules")
    assert (slot.section_id, slot.order, slot.group) == ("blog-rules", None, None)
    # Later code writes RuleSlot("blog-rules", 0), so the order is the second field.
    assert RuleSlot("blog-rules", 2, "tone") == RuleSlot("blog-rules", order=2, group="tone")
    with pytest.raises(dataclasses.FrozenInstanceError):
        slot.order = 1


def test_the_split_cause_and_action_are_the_stored_strings() -> None:
    assert SkillVersionCause.RULE_SPLIT.value == "rule_split"
    assert AdminAction.RULES_SPLIT.value == "rules_split"


def test_the_ownership_refusal_is_not_a_value_error() -> None:
    # A handler that turns ValueError into a message for the person must not
    # turn a broken writer into advice.
    assert issubclass(RuleOwnershipError, Exception)
    assert not issubclass(RuleOwnershipError, ValueError)


def test_a_rule_records_what_it_was_split_from() -> None:
    rule = Rule(id="rule-t1-split-0123456789ab", body="Keep receipts.", tenant_id="acme")
    assert rule.split_from is None and rule.split_at is None


def test_the_split_fields_stay_out_of_a_skills_content_digest() -> None:
    # The source write guard hashes every rule of a skill as JSON. A datetime cannot be
    # hashed that way, and where a copy came from says nothing about what the skill tells
    # an agent, so the fields are left out like the creation time is.
    store = InMemoryGraphStore()
    store.upsert_skill(Skill(id="blog", name="Blog", description="", domain="comms", tenant_id="acme"))
    copy = Rule(id=split_rule_id("rule-t1", BLOG), body="Keep receipts.", tenant_id="acme")
    store.upsert_rule(copy)
    store.attach_edge(Edge(EdgeType.BELONGS_TO, copy.id, "blog"), tenant_id="acme")
    before = content_digest(store, BLOG)
    split_at = datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc)
    store.upsert_rule(dataclasses.replace(copy, split_from="rule-t1", split_at=split_at))
    assert content_digest(store, BLOG) == before


def test_the_split_fields_stay_out_of_an_undo_record() -> None:
    # An undo receipt holds the rule as it was, and the undo is offered only while the rule
    # still matches it exactly. A receipt written before the fields existed holds neither key
    # (this is the record as that version wrote it), so the record must not hold them either,
    # or every such receipt would stop matching.
    legacy = {"body": "Keep receipts.", "corroboration_count": 1, "id": "rule-t1", "plane": "data",
              "polarity": "prescribe", "reference_only": False, "status": "active", "tenant_id": "acme"}
    rule = Rule(id="rule-t1", body="Keep receipts.", tenant_id="acme")
    split_at = datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc)
    assert record(rule) == legacy
    assert record(dataclasses.replace(rule, split_from="rule-t0", split_at=split_at)) == legacy


@pytest.mark.integration
def test_neo4j_reads_the_split_fields_and_an_upsert_leaves_them(critic_neo4j_driver: Driver) -> None:
    from oms.adapters.neo4j.store import Neo4jGraphStore
    with critic_neo4j_driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
    store = Neo4jGraphStore(critic_neo4j_driver)
    store.ensure_schema()
    copy = Rule(id=split_rule_id("rule-t1", BLOG), body="Keep receipts.", tenant_id="acme")
    store.upsert_rule(copy)
    # A node written without the two properties, which is every rule today, reads as None.
    before = store.get_rule(copy.id)
    assert (before.split_from, before.split_at) == (None, None)
    # Only the split writes them, with its own query. The store hands back a timezone-aware time.
    split_at = datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc)
    with critic_neo4j_driver.session() as session:
        session.run("MATCH (n:Rule {id:$id}) SET n.split_from = $original, n.split_at = $at",
                    id=copy.id, original="rule-t1", at=split_at).consume()
    after = store.get_rule(copy.id)
    assert (after.split_from, after.split_at) == ("rule-t1", split_at)
    # An upsert writes named properties only, so it cannot clear them.
    store.upsert_rule(copy)
    again = store.get_rule(copy.id)
    assert (again.split_from, again.split_at) == ("rule-t1", split_at)
