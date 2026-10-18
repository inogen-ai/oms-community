"""Copy primitives every graph store provides (one rule, one skill, spec 4)."""
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.copies import COPY_KIND, CopyCause, RuleSlot, copy_example_id, copy_rule_id
from oms.domain.identity import SkillRef
from oms.domain.models import ContentBlock, Constraint, Edge, Example, Rule, Section, Skill, Transaction
from oms.domain.types import (EdgeType, ExampleKind, Mutability, RuleStatus, SectionKind,
                              SignalType, SourceRuntime)
from oms.ports.graph_store import GraphStore, RuleOwnershipError, SectionMutabilityError
from oms.skills.copies import copy_group, link_copies
from oms.sources.mutation import ContentTracker
from tests.schema.test_critic_schema import schema_driver  # noqa: F401

TENANT = "acme"


@pytest.fixture(params=["memory", pytest.param("neo4j", marks=pytest.mark.integration)])
def store(request: pytest.FixtureRequest) -> GraphStore:
    if request.param == "memory":
        return InMemoryGraphStore()
    from oms.adapters.neo4j.store import Neo4jGraphStore
    driver = request.getfixturevalue("schema_driver")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
    graph = Neo4jGraphStore(driver)
    graph.ensure_schema()
    return graph


def _world(store: GraphStore) -> str:
    for tenant, skill_id in ((TENANT, "email"), (TENANT, "blog"), ("beta", "email")):
        store.upsert_skill(Skill(id=skill_id, name=skill_id.title(), description="", domain="comms",
                                 tenant_id=tenant))
    store.upsert_section(Section(id="blog-rules", skill_id="blog", kind=SectionKind.RULES, heading="Rules",
                                 order=0, mutability=Mutability.SYSTEM_AGGREGATED, tenant_id=TENANT))
    store.upsert_transaction(Transaction(id="t1", signal_type=SignalType.EXPLICIT_CORRECTION,
                                         source_runtime=SourceRuntime.MCP, sanitised_payload_ref="t1.json",
                                         timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc),
                                         tenant_id=TENANT))
    store.upsert_rule(Rule(id="rule-t1", body="Use formal language.", tenant_id=TENANT,
                           status=RuleStatus.FLAGGED, corroboration_count=3))
    store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-t1", "email", {"repo_fallback": True}), tenant_id=TENANT)
    store.attach_edge(Edge(EdgeType.DERIVED_FROM, "rule-t1", "t1"), tenant_id=TENANT)
    store.upsert_tag("tone", "tone")
    store.attach_edge(Edge(EdgeType.TAGGED_WITH, "rule-t1", "tone"), tenant_id=TENANT)
    store.observe_rule_in("rule-t1", "t1", "skills/email/SKILL.md")
    store.upsert_constraint(Constraint(id="constraint-acme-plain", body="Write plainly.", tenant_id=TENANT))
    store.attach_edge(Edge(EdgeType.CONFLICTS_WITH, "rule-t1", "constraint-acme-plain"), tenant_id=TENANT)
    for example_id, body in (("example-aaaaaaaaaaaaaaaa", "Dear Ms Smith"), ("example-bbbbbbbbbbbbbbbb", "Yours sincerely")):
        store.upsert_example(Example(id=example_id, body=body, kind=ExampleKind.POSITIVE, tenant_id=TENANT,
                                     parent_rule_id="rule-t1"), tenant_id=TENANT)
    return copy_rule_id("rule-t1", SkillRef(TENANT, "blog"))


def test_a_copy_carries_text_state_lineage_tags_examples_and_constraint_conflicts(store: GraphStore) -> None:
    copy_id = _world(store)
    copy = store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog",
                           slots=(RuleSlot("blog-rules", order=4, group="Tone"),))
    original = store.get_rule("rule-t1")
    assert (copy.body, copy.status, copy.corroboration_count, copy.polarity, copy.plane,
            copy.reference_only, copy.created_at) == (original.body, original.status,
            original.corroboration_count, original.polarity, original.plane,
            original.reference_only, original.created_at)
    assert [s.id for s in store.skills_for_rule(copy_id, tenant_id=TENANT)] == ["blog"]
    assert [s.id for s in store.skills_for_rule("rule-t1", tenant_id=TENANT)] == ["email"]
    assert [t.id for t in store.lineage(copy_id)] == ["t1"]
    assert {r.id for r in store.rules_by_tags({"tone"}, TENANT)} == {"rule-t1", copy_id}
    assert {r.id for r in store.rules_conflicting_with_constraints(TENANT)} == {"rule-t1", copy_id}
    assert [e.id for e in store.examples_for_rule(copy_id, tenant_id=TENANT)] == [
        copy_example_id("example-aaaaaaaaaaaaaaaa", copy_id), copy_example_id("example-bbbbbbbbbbbbbbbb", copy_id)]
    assert len(store.examples_for_rule("rule-t1", tenant_id=TENANT)) == 2
    placements = store.rule_placements_for_section("blog-rules", tenant_id=TENANT)
    assert [(p.rule_id, p.order, p.group) for p in placements] == [(copy_id, 4, "Tone")]


def test_copying_again_returns_the_copy_and_writes_nothing_new(store: GraphStore) -> None:
    copy_id = _world(store)
    first = store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog")
    again = store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog")
    assert again.id == first.id
    assert len(store.examples_for_rule(copy_id, tenant_id=TENANT)) == 2


def test_a_copy_can_take_a_new_status(store: GraphStore) -> None:
    copy_id = _world(store)
    copy = store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog", status=RuleStatus.ACTIVE)
    assert copy.status is RuleStatus.ACTIVE and store.get_rule("rule-t1").status is RuleStatus.FLAGGED


def test_a_copy_can_be_placed_and_belong_nowhere(store: GraphStore) -> None:
    copy_id = _world(store)
    store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id=None, slots=(RuleSlot("blog-rules", 0),))
    assert store.skills_for_rule(copy_id, tenant_id=TENANT) == []
    assert [p.rule_id for p in store.rule_placements_for_section("blog-rules", tenant_id=TENANT)] == [copy_id]


def test_a_copy_id_held_by_another_tenant_is_refused(store: GraphStore) -> None:
    copy_id = _world(store)
    store.upsert_rule(Rule(id=copy_id, body="Theirs.", tenant_id="beta"))
    with pytest.raises(ValueError):
        store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog")


def test_copy_groups_answers_many_rules_in_one_read_and_reads_copy_edges_only(store: GraphStore) -> None:
    copy_id = _world(store)
    store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog")
    store.upsert_rule(Rule(id="rule-other", body="Use plain words.", tenant_id=TENANT))
    link_copies(store, ["rule-t1", copy_id], cause=CopyCause.LEARNING, tenant_id=TENANT)
    store.upsert_cross_skill_edge(EdgeType.EQUIVALENT_TO, "rule-t1", "rule-other", 0.97)
    groups = store.copy_groups(["rule-t1", copy_id, "rule-other"], tenant_id=TENANT)
    assert groups == {"rule-t1": (copy_id,), copy_id: ("rule-t1",)}
    assert store.copy_groups([], tenant_id=TENANT) == {}
    assert store.copy_groups(["rule-t1"], tenant_id="beta") == {}
    assert copy_group(store, "rule-t1", tenant_id=TENANT) == tuple(sorted(("rule-t1", copy_id)))


def test_an_equivalence_written_onto_a_copy_pair_keeps_the_copy_edge(store: GraphStore) -> None:
    # Review Focus 4: import enrichment writes EQUIVALENT_TO with no kind
    # between similar rules of different skills, and two copies are exactly that.
    copy_id = _world(store)
    store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog")
    link_copies(store, ["rule-t1", copy_id], cause=CopyCause.SPLIT, tenant_id=TENANT)
    store.upsert_cross_skill_edge(EdgeType.EQUIVALENT_TO, copy_id, "rule-t1", 0.96)
    assert store.cross_skill_neighbours(copy_id, tenant_id=TENANT, kind=COPY_KIND) == [
        (EdgeType.EQUIVALENT_TO, "rule-t1", 1.0)]
    assert store.copy_groups([copy_id], tenant_id=TENANT) == {copy_id: ("rule-t1",)}


def test_cross_skill_neighbours_reads_one_tenant_only(store: GraphStore) -> None:
    _world(store)
    store.upsert_rule(Rule(id="beta-rule", body="Theirs.", tenant_id="beta"))
    store.upsert_rule(Rule(id="rule-near", body="Formal words.", tenant_id=TENANT))
    store.upsert_cross_skill_edge(EdgeType.RELATES_TO, "rule-t1", "rule-near", 0.9, kind="similar")
    assert store.cross_skill_neighbours("rule-t1", tenant_id=TENANT) == [(EdgeType.RELATES_TO, "rule-near", 0.9)]
    assert store.cross_skill_neighbours("rule-t1", tenant_id="beta") == []
    assert store.cross_skill_neighbours("rule-t1", tenant_id=TENANT, kind=COPY_KIND) == []


def test_link_copies_joins_every_member_both_ways_and_extends_an_existing_group(store: GraphStore) -> None:
    _world(store)
    blog = store.copy_rule("rule-t1", copy_rule_id("rule-t1", SkillRef(TENANT, "blog")),
                           tenant_id=TENANT, skill_id="blog")
    link_copies(store, ["rule-t1", blog.id], cause=CopyCause.SPLIT, tenant_id=TENANT)
    store.upsert_skill(Skill(id="press", name="Press", description="", domain="comms", tenant_id=TENANT))
    press = store.copy_rule(blog.id, copy_rule_id(blog.id, SkillRef(TENANT, "press")),
                            tenant_id=TENANT, skill_id="press")
    members = link_copies(store, [blog.id, press.id], cause=CopyCause.REVIEW, tenant_id=TENANT)
    assert members == tuple(sorted(("rule-t1", blog.id, press.id)))
    for member in members:
        assert set(store.copy_groups([member], tenant_id=TENANT)[member]) == set(members) - {member}
    assert link_copies(store, ["rule-t1"], cause=CopyCause.REVIEW, tenant_id=TENANT) == members


def test_rule_ids_derived_from_lists_the_first_copy_first(store: GraphStore) -> None:
    _world(store)
    for rule_id in ("rule-t1-0000aaaa", "rule-a-survivor"):
        store.upsert_rule(Rule(id=rule_id, body="x", tenant_id=TENANT))
        store.attach_edge(Edge(EdgeType.DERIVED_FROM, rule_id, "t1"), tenant_id=TENANT)
    assert store.rule_ids_derived_from("t1", tenant_id=TENANT) == ["rule-t1", "rule-a-survivor", "rule-t1-0000aaaa"]
    assert store.rule_ids_derived_from("t1", tenant_id="beta") == []
    assert store.rules_derived_from(["t1"]) == {"t1": "rule-t1"}


@pytest.mark.integration
def test_the_neo4j_copy_carries_the_embedding_property(request: pytest.FixtureRequest) -> None:
    from oms.adapters.neo4j.store import Neo4jGraphStore
    driver = request.getfixturevalue("schema_driver")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
    graph = Neo4jGraphStore(driver)
    graph.ensure_schema()
    copy_id = _world(graph)
    with driver.session() as session:
        session.run("MATCH (r:Rule {id:'rule-t1'}) SET r.embedding = [0.25, 0.5, 0.75]").consume()
    graph.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog")
    with driver.session() as session:
        row = session.run("MATCH (c:Rule {id:$id}) RETURN c.embedding AS e", id=copy_id).single()
    assert list(row["e"]) == [0.25, 0.5, 0.75]


# -- The same contract, read closely ------------------------------------------
#
# The port has no read for an edge's own properties, nor for a copy edge's
# kind and cause, so the helpers below ask each store directly.


def _transaction(transaction_id: str) -> Transaction:
    return Transaction(id=transaction_id, signal_type=SignalType.EXPLICIT_CORRECTION,
                       source_runtime=SourceRuntime.MCP, sanitised_payload_ref=f"{transaction_id}.json",
                       timestamp=datetime(2026, 10, 2, tzinfo=timezone.utc), tenant_id=TENANT)


def _more_sections(store: GraphStore) -> None:
    """A rules section of the source's own skill and a prose section of the copy's skill."""
    store.upsert_section(Section(id="email-rules", skill_id="email", kind=SectionKind.RULES, heading="Rules",
                                 order=0, mutability=Mutability.SYSTEM_AGGREGATED, tenant_id=TENANT))
    store.upsert_section(Section(id="blog-prose", skill_id="blog", kind=SectionKind.PROSE, heading="About",
                                 order=1, mutability=Mutability.AUTHORIAL_PASSTHROUGH, tenant_id=TENANT))


def _edge_row(store: GraphStore, edge_type: EdgeType, from_id: str,
              to_id: str) -> tuple[str | None, float, str | None] | None:
    """The kind, confidence and cause stored on one rule-to-rule informational edge."""
    if isinstance(store, InMemoryGraphStore):
        rows = [(kind, confidence, cause) for (et, frm, to, confidence, kind, cause) in store.cross_edges
                if et is edge_type and (frm, to) == (from_id, to_id)]
    else:
        with store._driver.session() as session:
            rows = [(row["kind"], row["confidence"], row["cause"]) for row in session.run(
                f"MATCH (:Rule {{id:$frm}})-[r:{edge_type.value}]->(:Rule {{id:$to}}) "
                "RETURN r.kind AS kind, r.confidence AS confidence, r.cause AS cause",
                frm=from_id, to=to_id)]
    assert len(rows) <= 1, rows
    return rows[0] if rows else None


def _edge_properties(store: GraphStore, edge_type: EdgeType, from_id: str,
                     to_id: str) -> dict[str, Any] | None:
    """The properties stored on one edge out of a rule, or None when there is no such edge."""
    if isinstance(store, InMemoryGraphStore):
        rows = [dict(edge.properties) for edge in store.edges
                if edge.type is edge_type and (edge.from_id, edge.to_id) == (from_id, to_id)]
    else:
        with store._driver.session() as session:
            rows = [dict(row["properties"]) for row in session.run(
                f"MATCH (:Rule {{id:$frm}})-[r:{edge_type.value}]->({{id:$to}}) "
                "RETURN properties(r) AS properties", frm=from_id, to=to_id)]
    assert len(rows) <= 1, rows
    return rows[0] if rows else None


def _traces(store: GraphStore, rule_id: str) -> list[str]:
    """Everything a store still holds that names the rule: the rule itself, an
    edge at either end, an example under it, a placement or an observation."""
    if isinstance(store, InMemoryGraphStore):
        found = [f"rule {rule_id}"] if rule_id in store.rules else []
        found += [f"{edge.type.value} edge" for edge in store.edges if rule_id in (edge.from_id, edge.to_id)]
        found += [f"{row[0].value} edge" for row in store.cross_edges if rule_id in (row[1], row[2])]
        found += [f"example {example.id}" for example in store.examples.values()
                  if example.parent_rule_id == rule_id]
        found += [f"placement in {key.skill_id}" for key, ids in store.section_to_rules.items() if rule_id in ids]
        found += ["observations"] if rule_id in store.observations else []
        return found
    # A Neo4j relationship cannot outlive its nodes, so the nodes are enough.
    with store._driver.session() as session:
        return [row["what"] for row in session.run(
            "MATCH (n) WHERE n.id = $id OR n.parent_rule_id = $id RETURN n.id AS what", id=rule_id)]


def _mark_split(store: GraphStore, rule_id: str, original: str, at: datetime) -> None:
    """Give a rule the split fields the way each store gets them: the schema 3
    split sets them with a statement of its own, and the in-memory store keeps
    the fields of the rule it is given."""
    if isinstance(store, InMemoryGraphStore):
        store.upsert_rule(replace(store.get_rule(rule_id), split_from=original, split_at=at))
        return
    with store._driver.session() as session:
        session.run("MATCH (r:Rule {id:$id}) SET r.split_from = $original, r.split_at = $at",
                    id=rule_id, original=original, at=at).consume()


def _as_read(store: GraphStore, rule_id: str) -> tuple[object, ...]:
    """What the readers say about one rule, with the properties of its own edges."""
    return (store.get_rule(rule_id),
            [skill.id for skill in store.skills_for_rule(rule_id, tenant_id=TENANT)],
            [transaction.id for transaction in store.lineage(rule_id)],
            store.examples_for_rule(rule_id, tenant_id=TENANT),
            store.cross_skill_neighbours(rule_id, tenant_id=TENANT),
            [_edge_properties(store, edge_type, rule_id, target) for edge_type, target in (
                (EdgeType.BELONGS_TO, "email"), (EdgeType.DERIVED_FROM, "t1"), (EdgeType.OBSERVED_IN, "t1"),
                (EdgeType.TAGGED_WITH, "tone"), (EdgeType.CONFLICTS_WITH, "constraint-acme-plain"))])


def test_no_later_write_changes_a_copy_edge(store: GraphStore) -> None:
    # Review Focus 4 in full. Enrichment reads each rule of the skill it
    # imports, so it reaches a copy pair from both ends, with no kind; another
    # writer may name a kind or a cause. The copy edge stays as it was written.
    copy_id = _world(store)
    store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog")
    link_copies(store, ["rule-t1", copy_id], cause=CopyCause.SPLIT, tenant_id=TENANT)
    store.upsert_cross_skill_edge(EdgeType.EQUIVALENT_TO, "rule-t1", copy_id, 0.96)
    store.upsert_cross_skill_edge(EdgeType.EQUIVALENT_TO, copy_id, "rule-t1", 0.97)
    store.upsert_cross_skill_edge(EdgeType.EQUIVALENT_TO, copy_id, "rule-t1", 0.5, kind="similar",
                                  cause=CopyCause.REVIEW.value)
    for pair in (("rule-t1", copy_id), (copy_id, "rule-t1")):
        assert _edge_row(store, EdgeType.EQUIVALENT_TO, *pair) == (COPY_KIND, 1.0, CopyCause.SPLIT.value)


def test_an_informational_edge_takes_a_new_confidence_and_keeps_what_the_write_leaves_out(
        store: GraphStore) -> None:
    _world(store)
    store.upsert_rule(Rule(id="rule-near", body="Formal words.", tenant_id=TENANT))
    store.upsert_cross_skill_edge(EdgeType.RELATES_TO, "rule-t1", "rule-near", 0.86, kind="similar", cause="import")
    store.upsert_cross_skill_edge(EdgeType.RELATES_TO, "rule-t1", "rule-near", 0.9)
    assert _edge_row(store, EdgeType.RELATES_TO, "rule-t1", "rule-near") == ("similar", 0.9, "import")


def test_linking_copies_turns_an_earlier_equivalence_into_a_copy_edge(store: GraphStore) -> None:
    copy_id = _world(store)
    store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog")
    store.upsert_cross_skill_edge(EdgeType.EQUIVALENT_TO, "rule-t1", copy_id, 0.96)
    link_copies(store, ["rule-t1", copy_id], cause=CopyCause.REVIEW, tenant_id=TENANT)
    assert _edge_row(store, EdgeType.EQUIVALENT_TO, "rule-t1", copy_id) == (COPY_KIND, 1.0, CopyCause.REVIEW.value)
    assert store.copy_groups(["rule-t1"], tenant_id=TENANT) == {"rule-t1": (copy_id,)}


def test_extending_a_group_keeps_each_edge_with_the_cause_it_was_written_with(store: GraphStore) -> None:
    _world(store)
    blog = store.copy_rule("rule-t1", copy_rule_id("rule-t1", SkillRef(TENANT, "blog")),
                           tenant_id=TENANT, skill_id="blog")
    link_copies(store, ["rule-t1", blog.id], cause=CopyCause.SPLIT, tenant_id=TENANT)
    store.upsert_skill(Skill(id="press", name="Press", description="", domain="comms", tenant_id=TENANT))
    press = store.copy_rule(blog.id, copy_rule_id(blog.id, SkillRef(TENANT, "press")),
                            tenant_id=TENANT, skill_id="press")
    link_copies(store, [press.id, blog.id], cause=CopyCause.REVIEW, tenant_id=TENANT)
    for pair in (("rule-t1", blog.id), (blog.id, "rule-t1")):
        assert _edge_row(store, EdgeType.EQUIVALENT_TO, *pair) == (COPY_KIND, 1.0, CopyCause.SPLIT.value)
    for pair in (("rule-t1", press.id), (press.id, "rule-t1"), (blog.id, press.id), (press.id, blog.id)):
        assert _edge_row(store, EdgeType.EQUIVALENT_TO, *pair) == (COPY_KIND, 1.0, CopyCause.REVIEW.value)


def test_one_rule_alone_is_its_own_group_and_links_nothing(store: GraphStore) -> None:
    _world(store)
    assert link_copies(store, ["rule-t1", "rule-t1"], cause=CopyCause.LEARNING, tenant_id=TENANT) == ("rule-t1",)
    assert store.cross_skill_neighbours("rule-t1", tenant_id=TENANT) == []
    assert copy_group(store, "rule-t1", tenant_id=TENANT) == ("rule-t1",)
    assert store.copy_groups(["rule-t1", "rule-absent"], tenant_id=TENANT) == {}


def test_cross_skill_neighbours_are_the_informational_edges_by_type_and_then_id(store: GraphStore) -> None:
    _world(store)
    for rule_id in ("rule-c", "rule-a", "rule-b"):
        store.upsert_rule(Rule(id=rule_id, body=rule_id, tenant_id=TENANT))
    store.upsert_cross_skill_edge(EdgeType.RELATES_TO, "rule-t1", "rule-c", 0.9, kind="similar")
    store.upsert_cross_skill_edge(EdgeType.EQUIVALENT_TO, "rule-t1", "rule-b", 0.96)
    store.upsert_cross_skill_edge(EdgeType.CROSS_SKILL_DIVERGENCE, "rule-t1", "rule-c", 0.95)
    store.upsert_cross_skill_edge(EdgeType.EQUIVALENT_TO, "rule-t1", "rule-a", 0.97)
    # Not one of the three cross-skill kinds, so not a neighbour in either store.
    store.upsert_cross_skill_edge(EdgeType.RELATED_TO, "rule-t1", "rule-b", 1.0)
    assert store.cross_skill_neighbours("rule-t1", tenant_id=TENANT) == [
        (EdgeType.CROSS_SKILL_DIVERGENCE, "rule-c", 0.95), (EdgeType.EQUIVALENT_TO, "rule-a", 0.97),
        (EdgeType.EQUIVALENT_TO, "rule-b", 0.96), (EdgeType.RELATES_TO, "rule-c", 0.9)]
    assert store.cross_skill_neighbours("rule-t1", tenant_id=TENANT, kind="similar") == [
        (EdgeType.RELATES_TO, "rule-c", 0.9)]


def test_a_copy_writes_nothing_to_its_source(store: GraphStore) -> None:
    copy_id = _world(store)
    _more_sections(store)
    store.attach_rule(store.get_rule("rule-t1"), "email-rules", order=2, group="Tone", tenant_id=TENANT)
    before = _as_read(store, "rule-t1")
    store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog", slots=(RuleSlot("blog-rules", 0),))
    assert _as_read(store, "rule-t1") == before
    assert [(p.rule_id, p.order, p.group) for p in store.rule_placements_for_section(
        "email-rules", tenant_id=TENANT)] == [("rule-t1", 2, "Tone")]


def test_every_carried_edge_keeps_its_properties_and_no_rule_to_rule_edge_is_carried(store: GraphStore) -> None:
    _world(store)
    store.upsert_rule(Rule(id="rule-t9", body="Sign with your name.", tenant_id=TENANT))
    store.upsert_rule(Rule(id="rule-rival", body="Sign with a nickname.", tenant_id=TENANT))
    store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-t9", "email"), tenant_id=TENANT)
    carried = {(EdgeType.DERIVED_FROM, "t1"): {"weight": 2},
               (EdgeType.TAGGED_WITH, "tone"): {"origin": "import"},
               (EdgeType.CONFLICTS_WITH, "constraint-acme-plain"): {"verdict": "conflict"}}
    for (edge_type, target), properties in carried.items():
        store.attach_edge(Edge(edge_type, "rule-t9", target, properties), tenant_id=TENANT)
    for edge_type in (EdgeType.CONFLICTS_WITH, EdgeType.SUPERSEDES, EdgeType.RELATED_TO):
        store.attach_edge(Edge(edge_type, "rule-t9", "rule-rival"), tenant_id=TENANT)
    copy_id = copy_rule_id("rule-t9", SkillRef(TENANT, "blog"))
    store.copy_rule("rule-t9", copy_id, tenant_id=TENANT, skill_id="blog",
                    belongs_properties={"repo_fallback": True})
    for (edge_type, target), properties in carried.items():
        assert _edge_properties(store, edge_type, copy_id, target) == properties
    assert _edge_properties(store, EdgeType.BELONGS_TO, copy_id, "blog") == {"repo_fallback": True}
    assert _edge_properties(store, EdgeType.BELONGS_TO, copy_id, "email") is None
    for edge_type in (EdgeType.CONFLICTS_WITH, EdgeType.SUPERSEDES, EdgeType.RELATED_TO):
        assert _edge_properties(store, edge_type, copy_id, "rule-rival") is None


def test_a_copy_has_already_seen_every_source_its_original_saw(store: GraphStore) -> None:
    copy_id = _world(store)
    copy = store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog")
    store.observe_rule_in(copy_id, "t1", "skills/email/SKILL.md")
    assert store.get_rule(copy_id).corroboration_count == copy.corroboration_count
    store.observe_rule_in(copy_id, "t1", "skills/blog/SKILL.md")
    assert store.get_rule(copy_id).corroboration_count == copy.corroboration_count + 1


@pytest.mark.parametrize(("skill_id", "slots", "error"), [
    ("blog", (RuleSlot("blog-rules", 0), RuleSlot("email-rules", 1)), RuleOwnershipError),
    ("email", (RuleSlot("blog-rules", 0),), RuleOwnershipError),
    (None, (RuleSlot("blog-rules", 0), RuleSlot("email-rules", 1)), RuleOwnershipError),
    ("blog", (RuleSlot("blog-rules", 0), RuleSlot("blog-prose", 1)), SectionMutabilityError),
    ("blog", (RuleSlot("blog-rules", 0), RuleSlot("absent", 1)), KeyError),
    ("absent", (), KeyError),
], ids=["second-slot-in-another-skill", "slot-outside-its-own-skill", "slots-in-two-skills",
        "second-slot-in-prose", "second-slot-missing", "skill-missing"])
def test_a_refused_copy_leaves_nothing_behind(store: GraphStore, skill_id: str | None,
                                              slots: tuple[RuleSlot, ...], error: type[Exception]) -> None:
    copy_id = _world(store)
    _more_sections(store)
    with pytest.raises(error):
        store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id=skill_id, slots=slots)
    assert _traces(store, copy_id) == []
    assert store.rule_placements_for_section("blog-rules", tenant_id=TENANT) == []
    assert {rule.id for rule in store.rules_by_tags({"tone"}, TENANT)} == {"rule-t1"}


def test_a_copy_needs_its_source_in_the_same_tenant(store: GraphStore) -> None:
    copy_id = _world(store)
    with pytest.raises(KeyError):
        store.copy_rule("rule-absent", "rule-absent-copy", tenant_id=TENANT, skill_id="blog")
    with pytest.raises(KeyError):
        store.copy_rule("rule-t1", copy_id, tenant_id="beta", skill_id="email")
    assert _traces(store, "rule-absent-copy") == [] and _traces(store, copy_id) == []


def test_a_copy_never_carries_the_split_fields_of_its_source(store: GraphStore) -> None:
    # A review copy of a split copy was not made by the split.
    copy_id = _world(store)
    at = datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc)
    _mark_split(store, "rule-t1", "rule-t0", at)
    copy = store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog")
    stored = store.get_rule(copy_id)
    assert (copy.split_from, copy.split_at, stored.split_from, stored.split_at) == (None, None, None, None)
    source = store.get_rule("rule-t1")
    assert (source.split_from, source.split_at) == ("rule-t0", at)


def test_no_rule_write_clears_the_split_fields(store: GraphStore) -> None:
    _world(store)
    at = datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc)
    _mark_split(store, "rule-t1", "rule-t0", at)
    store.upsert_rule(Rule(id="rule-t1", body="Use formal language, always.", tenant_id=TENANT))
    store.create_rule_if_absent(Rule(id="rule-t1", body="Something else.", tenant_id=TENANT))
    rule = store.get_rule("rule-t1")
    assert (rule.body, rule.split_from, rule.split_at) == ("Use formal language, always.", "rule-t0", at)


def test_the_first_copy_comes_first_whatever_was_written_first(store: GraphStore) -> None:
    _world(store)
    for transaction_id in ("t2", "t3"):
        store.upsert_transaction(_transaction(transaction_id))
    # Rules it corroborated are written before the rule t2 made, and t3 made
    # none: the order of the writes must not decide which rule comes first.
    for rule_id, transaction_id in (("rule-a-survivor", "t2"), ("rule-t2", "t2"), ("rule-z", "t3"), ("rule-b", "t3")):
        store.upsert_rule(Rule(id=rule_id, body="x", tenant_id=TENANT))
        store.attach_edge(Edge(EdgeType.DERIVED_FROM, rule_id, transaction_id), tenant_id=TENANT)
    store.upsert_content_block(ContentBlock(id="block-t2", content_ref="digest", kind=SectionKind.PROSE,
                                            tenant_id=TENANT, source_ref="blog/SKILL.md", body="Prose."))
    store.attach_edge(Edge(EdgeType.DERIVED_FROM, "block-t2", "t2"), tenant_id=TENANT)
    assert store.rules_derived_from(["t1", "t2", "t3"]) == {"t1": "rule-t1", "t2": "rule-t2", "t3": "rule-b"}
    assert store.rule_ids_derived_from("t2", tenant_id=TENANT) == ["rule-t2", "rule-a-survivor"]
    assert store.rule_ids_derived_from("t3", tenant_id=TENANT) == ["rule-b", "rule-z"]


def test_a_tracked_copy_fingerprints_the_skills_of_its_source_and_of_the_copy_only() -> None:
    store = InMemoryGraphStore()
    copy_id = _world(store)
    # A third skill: a write nobody classified would fingerprint it too.
    store.upsert_skill(Skill(id="press", name="Press", description="", domain="comms", tenant_id=TENANT))
    tracker = ContentTracker(store, TENANT)
    with tracker.active() as graph:
        graph.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog", slots=(RuleSlot("blog-rules", 0),))
    assert not tracker.whole_tenant
    assert set(tracker.before) == {SkillRef(TENANT, "email"), SkillRef(TENANT, "blog")}
    assert tracker.changed() == (SkillRef(TENANT, "blog"),)


def test_a_copy_placed_only_by_a_section_fingerprints_that_sections_skill() -> None:
    store = InMemoryGraphStore()
    copy_id = _world(store)
    store.upsert_skill(Skill(id="press", name="Press", description="", domain="comms", tenant_id=TENANT))
    tracker = ContentTracker(store, TENANT)
    with tracker.active() as graph:
        graph.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id=None, slots=(RuleSlot("blog-rules", 0),))
    assert not tracker.whole_tenant
    assert set(tracker.before) == {SkillRef(TENANT, "email"), SkillRef(TENANT, "blog")}


def test_reading_and_linking_copies_fingerprints_nothing() -> None:
    store = InMemoryGraphStore()
    copy_id = _world(store)
    store.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog")
    tracker = ContentTracker(store, TENANT)
    with tracker.active() as graph:
        link_copies(graph, ["rule-t1", copy_id], cause=CopyCause.LEARNING, tenant_id=TENANT)
        copy_group(graph, "rule-t1", tenant_id=TENANT)
        graph.rule_ids_derived_from("t1", tenant_id=TENANT)
        graph.cross_skill_neighbours("rule-t1", tenant_id=TENANT, kind=COPY_KIND)
    assert (tracker.before, tracker.whole_tenant) == ({}, False)
    assert store.copy_groups([copy_id], tenant_id=TENANT) == {copy_id: ("rule-t1",)}


class _StopsAt:
    """A driver whose write transactions fail at the first statement naming
    `marker`, after noting how many copies already exist inside them."""

    def __init__(self, driver: Any, marker: str, copy_id: str, seen: list[int]) -> None:
        self._driver, self._marker, self._copy_id, self._seen = driver, marker, copy_id, seen

    @contextmanager
    def session(self, **kwargs: Any) -> Iterator["_StopsAt._Session"]:
        with self._driver.session(**kwargs) as session:
            yield _StopsAt._Session(session, self)

    class _Session:
        def __init__(self, session: Any, outer: "_StopsAt") -> None:
            self._session, self._outer = session, outer

        def run(self, *args: Any, **kwargs: Any) -> Any:
            return self._session.run(*args, **kwargs)

        def execute_write(self, work: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            return self._session.execute_write(
                lambda tx: work(_StopsAt._Transaction(tx, self._outer), *args, **kwargs))

    class _Transaction:
        def __init__(self, tx: Any, outer: "_StopsAt") -> None:
            self._tx, self._outer = tx, outer

        def run(self, query: str, *args: Any, **kwargs: Any) -> Any:
            if self._outer._marker in query:
                self._outer._seen.append(self._tx.run("MATCH (c:Rule {id:$id}) RETURN count(c) AS n",
                                                      id=self._outer._copy_id).single()["n"])
                raise RuntimeError("stopped in the middle of the copy")
            return self._tx.run(query, *args, **kwargs)


@pytest.mark.integration
def test_the_neo4j_copy_is_one_transaction(request: pytest.FixtureRequest) -> None:
    from oms.adapters.neo4j.store import Neo4jGraphStore
    driver = request.getfixturevalue("schema_driver")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
    graph = Neo4jGraphStore(driver)
    graph.ensure_schema()
    copy_id = _world(graph)
    seen: list[int] = []
    # The placement is the copy's last write: stopped there, the copy, its
    # edges and its examples are already written inside the transaction.
    stopped = Neo4jGraphStore(_StopsAt(driver, "CONTAINS_RULE", copy_id, seen))
    with pytest.raises(RuntimeError):
        stopped.copy_rule("rule-t1", copy_id, tenant_id=TENANT, skill_id="blog",
                          slots=(RuleSlot("blog-rules", 0),))
    assert seen == [1]
    assert _traces(graph, copy_id) == []
