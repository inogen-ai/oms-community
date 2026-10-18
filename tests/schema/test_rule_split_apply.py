"""Splitting a shared rule (spec 8.4, 8.5; plan "Edge treatment" and "Item treatment")."""
import json
from datetime import datetime, timezone

import pytest

from oms.adapters.neo4j.review_queue import Neo4jReviewQueue
from oms.adapters.neo4j.store import Neo4jGraphStore
from oms.domain.copies import COPY_KIND, copy_example_id, split_rule_id
from oms.domain.identity import SkillRef
from oms.domain.models import Edge
from oms.domain.types import EdgeType, RuleStatus
from oms.publish.render import in_force
from oms.schema.metadata import SchemaCompatibilityError
from oms.schema.migrations.rule_split import (NO_LONGER_RAISED, NO_LONGER_RAISED_REASON, close_divergence,
                                              plan_split, split_rule)
from tests.legacy_rules import place_rule, share_rule
from tests.schema import split_world
from tests.schema.split_world import IMPORTED
from tests.schema.test_critic_schema import _snapshot, schema_driver  # noqa: F401

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _split_all(driver):
    for plan in plan_split(driver):
        with driver.session() as session:
            session.execute_write(lambda tx, tenant=plan.tenant_id: close_divergence(tx, tenant))
            for rule in plan.rules:
                session.execute_write(lambda tx, rule=rule: split_rule(tx, rule, now=NOW))


def _copy(rule_id, skill, tenant="acme"):
    return split_rule_id(rule_id, SkillRef(tenant, skill))


def _skills(store, rule_id, tenant="acme"):
    return [skill.id for skill in store.skills_for_rule(rule_id, tenant_id=tenant)]


def _placed(store, section_id):
    return {(p.rule_id, p.order, p.group) for p in store.rule_placements_for_section(section_id, tenant_id="acme")}


@pytest.fixture
def world(schema_driver):
    store = split_world.build(schema_driver)
    with schema_driver.session() as session:
        session.run("MATCH (r:Rule {id:$id}) SET r.embedding = [0.1, 0.2, 0.3]", id=IMPORTED).consume()
    _split_all(schema_driver)
    return store


def test_the_keeper_keeps_its_id_and_each_other_skill_gets_its_own_copy(world):
    assert _skills(world, IMPORTED) == ["email"] and _skills(world, _copy(IMPORTED, "blog")) == ["blog"]
    assert _skills(world, "rule-t-hint") == ["press"]
    assert _skills(world, _copy("rule-t-hint", "blog")) == ["blog"]
    assert _skills(world, _copy("rule-t-hint", "email")) == ["email"]
    assert _skills(world, "rule-t-placed") == ["email"]
    assert _skills(world, _copy("rule-t-placed", "press")) == []
    assert ("rule-t-placed", 7, "Orders") not in _placed(world, "press-rules")
    assert (_copy("rule-t-placed", "press"), 7, "Orders") in _placed(world, "press-rules")


def test_a_copy_carries_the_originals_text_state_lineage_tags_examples_and_vector(world, schema_driver):
    original, copy = world.get_rule(IMPORTED), world.get_rule(_copy(IMPORTED, "blog"))
    assert (copy.body, copy.status, copy.corroboration_count, copy.created_at, copy.polarity) == (
        original.body, original.status, original.corroboration_count, original.created_at, original.polarity)
    assert (copy.split_from, copy.split_at) == (IMPORTED, NOW)
    assert [t.id for t in world.lineage(copy.id)] == ["t-import"]
    assert copy.id in {r.id for r in world.rules_by_tags({"tone"}, "acme")}
    assert [e.id for e in world.examples_for_rule(copy.id, tenant_id="acme")] == [
        copy_example_id("example-aaaaaaaaaaaaaaaa", copy.id), copy_example_id("example-bbbbbbbbbbbbbbbb", copy.id)]
    assert len(world.examples_for_rule(IMPORTED, tenant_id="acme")) == 2
    with schema_driver.session() as session:
        row = session.run("MATCH (c:Rule {id:$id})-[b:BELONGS_TO]->(:Skill {id:'blog'}) "
                          "RETURN c.embedding AS e, properties(b) AS props", id=copy.id).single()
    assert list(row["e"]) == [0.1, 0.2, 0.3] and row["props"] == {"repo_fallback": True}


def test_a_copy_takes_the_originals_place_and_the_original_leaves_that_skill(world):
    assert _placed(world, "blog-rules") >= {(_copy(IMPORTED, "blog"), 2, "Voice")}
    assert not any(rule_id == IMPORTED for rule_id, _, _ in _placed(world, "blog-rules"))
    assert (IMPORTED, 0, "Tone") in _placed(world, "email-rules")


def test_rule_relations_follow_the_skill_they_belong_to(world, schema_driver):
    def edges(kind, source):
        with schema_driver.session() as session:
            return {row["id"] for row in session.run(
                f"MATCH (:Rule {{id:$id}})-[:{kind}]->(x:Rule) RETURN x.id AS id", id=source)}
    assert edges("CONFLICTS_WITH", _copy("rule-t-hint", "blog")) == {"rule-blog-a"}
    assert edges("CONFLICTS_WITH", "rule-t-hint") == set()
    assert edges("RELATED_TO", _copy("rule-t-hint", "email")) == {"rule-email-a"}
    assert edges("RELATED_TO", "rule-t-hint") == {"rule-press-a"}
    assert edges("RELATED_TO", _copy("rule-t-hint", "blog")) == set()


def test_a_rule_another_skills_rule_superseded_stays_out_of_force_in_every_skill(world):
    # Review Focus 2.
    for rule_id in ("rule-sup", _copy("rule-sup", "email")):
        rule = world.get_rule(rule_id)
        superseders = world.superseders_of(rule_id)
        assert rule.status is RuleStatus.SUPERSEDED and superseders == ["rule-blog-new-footer"], rule_id
        assert not in_force(rule, superseders)


def test_a_constraint_conflict_and_its_item_are_copied(world):
    copy_id = _copy("rule-flagged", "blog")
    assert world.get_rule(copy_id).status is RuleStatus.FLAGGED
    assert copy_id in {r.id for r in world.rules_conflicting_with_constraints("acme")}
    queue = Neo4jReviewQueue(world._driver)
    item = queue.get(f"review-{copy_id}-constraint-acme-plain")
    assert (item.subject_id, item.other_id, item.resolved) == (copy_id, "constraint-acme-plain", False)
    assert queue.get("review-rule-flagged-constraint-acme-plain").subject_id == "rule-flagged"


def test_a_held_rules_item_is_copied_and_relation_items_follow_their_counterpart(world):
    queue = Neo4jReviewQueue(world._driver)
    press = _copy("rule-t-plain", "press")
    original, copied = queue.get("review-pending-rule-t-plain"), queue.get(f"review-pending-{press}")
    assert original.subject_id == "rule-t-plain" and not original.resolved
    assert (copied.kind, copied.subject_id, copied.group_id, copied.transaction_id, copied.resolved) == (
        "pending_rule", press, "session-1", "t-plain", False)
    assert queue.get("review-rule-t-hint-rule-blog-a").subject_id == _copy("rule-t-hint", "blog")
    edit = queue.get("proposed-edit-000000000001")
    assert (edit.subject_id, edit.other_id, edit.base_revision) == (
        "blog", f"rule:{_copy('rule-t-hint', 'blog')}", "sha256:unchanged")


def test_divergence_items_are_closed_and_their_edges_kept(world):
    item = Neo4jReviewQueue(world._driver).get("review-cross-rule-blog-z-rule-email-z")
    assert (item.resolved, item.resolution, item.reason, item.decided_by) == (
        True, NO_LONGER_RAISED, NO_LONGER_RAISED_REASON, None)
    assert [n[1] for n in world.cross_skill_neighbours("rule-blog-z", tenant_id="acme")] == ["rule-email-z"]


def test_after_the_split_nothing_is_shared_and_copies_are_linked_both_ways(world, schema_driver):
    assert plan_split(schema_driver) == ()
    members = sorted(("rule-t-hint", _copy("rule-t-hint", "blog"), _copy("rule-t-hint", "email")))
    with schema_driver.session() as session:
        rows = {(row["a"], row["b"], row["cause"]) for row in session.run(
            "MATCH (a:Rule)-[e:EQUIVALENT_TO {kind:$kind}]->(b:Rule) WHERE a.id IN $ids "
            "RETURN a.id AS a, b.id AS b, e.cause AS cause", kind=COPY_KIND, ids=members)}
    assert rows == {(a, b, "split") for a in members for b in members if a != b}


def test_the_other_tenant_is_split_on_its_own(world):
    assert _skills(world, "rule-beta-shared", tenant="beta") == ["blog"]
    assert _skills(world, _copy("rule-beta-shared", "email", tenant="beta"), tenant="beta") == ["email"]
    assert world.get_rule(_copy("rule-beta-shared", "email")) is None


def test_the_counts_and_pending_versions_are_recorded(world, schema_driver):
    import json
    with schema_driver.session() as session:
        row = session.run("MATCH (m:OMSMetadata {id:'core'}) RETURN m.rule_split_counts AS c, "
                          "m.rule_split_pending_captures AS p").single()
    counts = json.loads(row["c"])
    assert {k: v for k, v in counts["acme"].items() if k != "skills"} == {
        "shared_rules": 6, "copies": 7, "held_items": 4, "divergence_closed": 1}
    assert counts["acme"]["skills"] == ["aardvark", "blog", "email", "press"]
    assert sorted(row["p"]) == sorted(SkillRef(t, s).storage_key for t, s in (
        ("acme", "blog"), ("acme", "email"), ("acme", "press"), ("beta", "email")))


def test_a_second_split_changes_nothing(schema_driver):
    split_world.build(schema_driver)
    plans = plan_split(schema_driver)
    _split_all(schema_driver)
    after = split_world.normalised(schema_driver)
    _split_all(schema_driver)
    with schema_driver.session() as session:
        for plan in plans:
            for rule in plan.rules:
                assert session.execute_write(lambda tx, rule=rule: split_rule(tx, rule, now=NOW)) is False
    assert split_world.normalised(schema_driver) == after


def test_a_rule_still_shared_whose_copy_exists_is_refused_and_nothing_is_written(schema_driver):
    split_world.build(schema_driver)
    plan = next(rule for tenant in plan_split(schema_driver) for rule in tenant.rules
                if rule.rule_id == "rule-t-hint")
    with schema_driver.session() as session:
        session.run("CREATE (:Rule:Entity {id:$id, tenant_id:'acme', body:'x', status:'active', "
                    "corroboration_count:1, created_at:datetime()})", id=plan.copies[0].copy_id).consume()
    before = _snapshot(schema_driver)
    with pytest.raises(SchemaCompatibilityError, match="still shared"):
        with schema_driver.session() as session:
            session.execute_write(lambda tx: split_rule(tx, plan, now=NOW))
    assert _snapshot(schema_driver) == before


def _plan_for(driver, rule_id):
    return next(rule for tenant in plan_split(driver) for rule in tenant.rules if rule.rule_id == rule_id)


def _ends(driver, kind, source):
    """The far ends of the source rule's outgoing edges of one type, copy edges left out."""
    with driver.session() as session:
        return {row["id"] for row in session.run(
            f"MATCH (:Rule {{id:$id}})-[e:{kind}]->(x) WHERE coalesce(e.kind, '') <> $copy RETURN x.id AS id",
            id=source, copy=COPY_KIND)}


def test_every_other_rule_edge_follows_its_row_of_the_table(schema_driver):
    # The rows of plan "Edge treatment" the world leaves out, on rule-t-hint:
    # press keeps it, blog and email get copies.
    store = split_world.build(schema_driver)
    for edge in (Edge(EdgeType.OBSERVED_IN, "rule-t-hint", "t-later"),
                 Edge(EdgeType.DUPLICATE_OF, "rule-t-hint", "rule-email-z"),
                 Edge(EdgeType.SUPERSEDES, "rule-t-hint", "rule-blog-z"),
                 Edge(EdgeType.CONFLICTS_WITH, "rule-email-a", "rule-t-hint"),
                 Edge(EdgeType.SUPERSEDES, "rule-press-z", "rule-t-hint"),
                 Edge(EdgeType.SUPERSEDES, "rule-blog-a", "rule-t-hint")):
        store.attach_edge(edge, tenant_id="acme")
    store.upsert_cross_skill_edge(EdgeType.EQUIVALENT_TO, "rule-t-hint", "rule-blog-a", 0.9)
    store.upsert_cross_skill_edge(EdgeType.RELATES_TO, "rule-t-hint", "rule-email-a", 0.8)
    _split_all(schema_driver)
    members = ("rule-t-hint", _copy("rule-t-hint", "blog"), _copy("rule-t-hint", "email"))

    def ends(kind):
        return tuple(_ends(schema_driver, kind, rule) for rule in members)
    assert ends("OBSERVED_IN") == ({"t-later"}, {"t-later"}, {"t-later"})
    assert ends("DUPLICATE_OF") == (set(), set(), {"rule-email-z"})
    assert ends("SUPERSEDES") == (set(), {"rule-blog-z"}, set())
    assert store.superseders_of("rule-blog-z") == [members[1]]
    assert _ends(schema_driver, "CONFLICTS_WITH", "rule-email-a") == {members[2]}
    # Superseded by a press rule and by a blog rule: every member keeps both
    # superseders, the keeper included (Review Focus 2).
    assert [store.superseders_of(rule) for rule in members] == [["rule-blog-a", "rule-press-z"]] * 3
    assert ends("EQUIVALENT_TO") == ({"rule-blog-a"}, set(), set())
    assert ends("RELATES_TO") == ({"rule-email-a"}, set(), set())


def _set_fallback_false(store, driver):
    with driver.session() as session:
        session.run("MATCH (:Rule {id:$id})-[b:BELONGS_TO]->(:Skill {id:'blog', tenant_id:'acme'}) "
                    "SET b.repo_fallback = false", id=IMPORTED).consume()


def _placed_only(store, driver):
    with driver.session() as session:
        session.run("MATCH (:Rule {id:$id})-[b:BELONGS_TO]->(:Skill {id:'blog', tenant_id:'acme'}) DELETE b",
                    id=IMPORTED).consume()


# Each change leaves the rule shared, and makes the plan say something the
# graph no longer holds.
_CHANGES = {
    "another skill": (IMPORTED, lambda store, driver: share_rule(store, IMPORTED, "press", tenant_id="acme")),
    "another membership": (IMPORTED, _set_fallback_false),
    "another place": (IMPORTED, lambda store, driver: place_rule(
        store, store.get_rule(IMPORTED), "blog-rules", tenant_id="acme", order=3, group="Voice")),
    "placed only": (IMPORTED, _placed_only),
    "belongs where it was placed": ("rule-t-placed", lambda store, driver: share_rule(
        store, "rule-t-placed", "press", tenant_id="acme")),
}


@pytest.mark.parametrize("change", sorted(_CHANGES))
def test_a_plan_the_graph_no_longer_matches_is_refused_and_nothing_is_written(schema_driver, change):
    store = split_world.build(schema_driver)
    rule_id, make = _CHANGES[change]
    plan = _plan_for(schema_driver, rule_id)
    make(store, schema_driver)
    before = _snapshot(schema_driver)
    with pytest.raises(SchemaCompatibilityError, match="changed after the split was planned"):
        with schema_driver.session() as session:
            session.execute_write(lambda tx: split_rule(tx, plan, now=NOW))
    assert _snapshot(schema_driver) == before


def test_a_split_that_fails_at_its_last_write_leaves_nothing_behind(schema_driver, monkeypatch):
    from oms.schema.migrations import rule_split
    split_world.build(schema_driver)
    plan = _plan_for(schema_driver, "rule-t-hint")
    before = _snapshot(schema_driver)

    def stop(*args, **kwargs):
        raise RuntimeError("stopped before the counters")
    monkeypatch.setattr(rule_split, "_record", stop)
    with pytest.raises(RuntimeError, match="stopped before the counters"):
        with schema_driver.session() as session:
            session.execute_write(lambda tx: split_rule(tx, plan, now=NOW))
    assert _snapshot(schema_driver) == before


def test_closing_divergence_again_closes_and_counts_nothing_more(schema_driver):
    split_world.build(schema_driver)
    with schema_driver.session() as session:
        closed = [session.execute_write(lambda tx, tenant=tenant: close_divergence(tx, tenant))
                  for tenant in ("acme", "acme", "beta")]
        row = session.run("MATCH (m:OMSMetadata {id:'core'}) RETURN m.rule_split_counts AS c").single()
    assert closed == [1, 0, 0]
    assert json.loads(row["c"]) == {"acme": {"shared_rules": 0, "copies": 0, "held_items": 0,
                                             "divergence_closed": 1, "skills": []}}
