"""One rule, one skill (spec 3): both stores refuse a second skill and a
placement in another skill's section, and write nothing when they do."""
import time
from threading import Event, Thread

import pytest

from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.copies import RuleSlot
from oms.domain.models import Edge, Rule, Section, Skill
from oms.domain.types import EdgeType, Mutability, SectionKind
from oms.ports.graph_store import RuleOwnershipError
from tests.legacy_rules import place_rule, share_rule
from tests.schema.test_critic_schema import schema_driver  # noqa: F401

TENANT = "acme"


@pytest.fixture(params=["memory", pytest.param("neo4j", marks=pytest.mark.integration)])
def store(request):
    if request.param == "memory":
        return InMemoryGraphStore()
    from oms.adapters.neo4j.store import Neo4jGraphStore
    driver = request.getfixturevalue("schema_driver")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
    graph = Neo4jGraphStore(driver)
    graph.ensure_schema()
    return graph


def _world(store):
    for skill_id in ("email", "blog"):
        store.upsert_skill(Skill(id=skill_id, name=skill_id, description="", domain="comms", tenant_id=TENANT))
        store.upsert_section(Section(id=f"{skill_id}-rules", skill_id=skill_id, kind=SectionKind.RULES,
                                     heading="Rules", order=0, mutability=Mutability.SYSTEM_AGGREGATED,
                                     tenant_id=TENANT))
    store.upsert_rule(Rule(id="rule-1", body="Use formal language.", tenant_id=TENANT))
    store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-1", "email"), tenant_id=TENANT)


def _skills(store, rule_id):
    return [s.id for s in store.skills_for_rule(rule_id, tenant_id=TENANT)]


def _placed(store, section_id):
    return [p.rule_id for p in store.rule_placements_for_section(section_id, tenant_id=TENANT)]


def test_a_second_skill_is_refused_and_nothing_is_written(store):
    _world(store)
    with pytest.raises(RuleOwnershipError):
        store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-1", "blog"), tenant_id=TENANT)
    assert _skills(store, "rule-1") == ["email"]


def test_the_rules_own_skill_may_be_attached_again(store):
    _world(store)
    store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-1", "email"), tenant_id=TENANT)
    assert _skills(store, "rule-1") == ["email"]


def test_a_placement_in_another_skills_section_is_refused(store):
    _world(store)
    with pytest.raises(RuleOwnershipError):
        store.attach_rule(store.get_rule("rule-1"), "blog-rules", order=0, tenant_id=TENANT)
    assert _placed(store, "blog-rules") == []
    store.attach_rule(store.get_rule("rule-1"), "email-rules", order=0, tenant_id=TENANT)
    assert _placed(store, "email-rules") == ["rule-1"]


def test_a_skill_is_refused_for_a_rule_placed_in_another_skill(store):
    store.upsert_skill(Skill(id="email", name="email", description="", domain="comms", tenant_id=TENANT))
    store.upsert_skill(Skill(id="blog", name="blog", description="", domain="comms", tenant_id=TENANT))
    store.upsert_section(Section(id="blog-rules", skill_id="blog", kind=SectionKind.RULES, heading="Rules",
                                 order=0, mutability=Mutability.SYSTEM_AGGREGATED, tenant_id=TENANT))
    store.upsert_rule(Rule(id="rule-2", body="Placed only.", tenant_id=TENANT))
    store.attach_rule(store.get_rule("rule-2"), "blog-rules", order=0, tenant_id=TENANT)
    with pytest.raises(RuleOwnershipError):
        store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-2", "email"), tenant_id=TENANT)
    store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-2", "blog"), tenant_id=TENANT)
    assert _skills(store, "rule-2") == ["blog"]


def test_a_placement_edge_written_directly_is_refused_in_another_skills_section(store) -> None:
    # `attach_edge` accepts CONTAINS_RULE as well; in Neo4j that edge is the
    # placement itself, so it must meet the same refusal as `attach_rule`.
    _world(store)
    with pytest.raises(RuleOwnershipError):
        store.attach_edge(Edge(EdgeType.CONTAINS_RULE, "blog-rules", "rule-1"), tenant_id=TENANT)
    assert _placed(store, "blog-rules") == []
    store.attach_edge(Edge(EdgeType.CONTAINS_RULE, "email-rules", "rule-1"), tenant_id=TENANT)
    assert _skills(store, "rule-1") == ["email"]


def test_moving_detaches_first_and_then_attaches(store):
    _world(store)
    store.detach_edge(Edge(EdgeType.BELONGS_TO, "rule-1", "email"), tenant_id=TENANT)
    store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-1", "blog"), tenant_id=TENANT)
    assert _skills(store, "rule-1") == ["blog"]


def test_a_copy_slot_in_another_skills_section_is_refused(store):
    _world(store)
    with pytest.raises(RuleOwnershipError):
        store.copy_rule("rule-1", "rule-1-copy-000000000000", tenant_id=TENANT, skill_id="blog",
                        slots=(RuleSlot("email-rules", 0),))
    assert store.get_rule("rule-1-copy-000000000000") is None


def test_inherit_placements_carries_only_within_the_successors_skill(store):
    _world(store)
    store.attach_rule(store.get_rule("rule-1"), "email-rules", order=3, group="Tone", tenant_id=TENANT)
    store.upsert_rule(Rule(id="rule-old-blog", body="Old blog rule.", tenant_id=TENANT))
    share_rule(store, "rule-old-blog", "blog", tenant_id=TENANT)
    place_rule(store, store.get_rule("rule-old-blog"), "blog-rules", order=1, tenant_id=TENANT)
    place_rule(store, store.get_rule("rule-old-blog"), "email-rules", order=5, tenant_id=TENANT)
    store.upsert_rule(Rule(id="rule-new", body="New email rule.", tenant_id=TENANT))
    store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-new", "email"), tenant_id=TENANT)
    store.inherit_placements("rule-old-blog", store.get_rule("rule-new"), tenant_id=TENANT)
    assert "rule-new" in _placed(store, "email-rules")
    assert "rule-new" not in _placed(store, "blog-rules")
    # The place it keeps is the retired rule's, order and group included.
    assert [(p.rule_id, p.order, p.group) for p in store.rule_placements_for_section(
        "email-rules", tenant_id=TENANT) if p.rule_id == "rule-new"] == [("rule-new", 5, None)]


def test_inherit_placements_carries_every_place_to_a_successor_in_no_skill_yet(store) -> None:
    # The supersession of a rule in one skill passes on exactly as before.
    _world(store)
    store.upsert_section(Section(id="email-more", skill_id="email", kind=SectionKind.RULES, heading="More",
                                 order=1, mutability=Mutability.SYSTEM_AGGREGATED, tenant_id=TENANT))
    store.attach_rule(store.get_rule("rule-1"), "email-rules", order=3, group="Tone", tenant_id=TENANT)
    store.attach_rule(store.get_rule("rule-1"), "email-more", order=7, tenant_id=TENANT)
    store.upsert_rule(Rule(id="rule-loose", body="Loose successor.", tenant_id=TENANT))
    store.inherit_placements("rule-1", store.get_rule("rule-loose"), tenant_id=TENANT)
    placed = {section: [(p.order, p.group) for p in store.rule_placements_for_section(section, tenant_id=TENANT)
                        if p.rule_id == "rule-loose"] for section in ("email-rules", "email-more", "blog-rules")}
    assert placed == {"email-rules": [(3, "Tone")], "email-more": [(7, None)], "blog-rules": []}


def test_the_memory_guard_opens_only_inside_legacy_shared_rules():
    store = InMemoryGraphStore()
    _world(store)
    with store.legacy_shared_rules():
        store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-1", "blog"), tenant_id=TENANT)
    assert _skills(store, "rule-1") == ["blog", "email"]
    store.upsert_rule(Rule(id="rule-3", body="Another.", tenant_id=TENANT))
    store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-3", "email"), tenant_id=TENANT)
    with pytest.raises(RuleOwnershipError):
        store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-3", "blog"), tenant_id=TENANT)


def test_the_memory_guard_closes_after_an_error_and_after_a_nested_block() -> None:
    store = InMemoryGraphStore()
    _world(store)
    with pytest.raises(RuntimeError):
        with store.legacy_shared_rules():
            raise RuntimeError("the fixture failed part way")
    with pytest.raises(RuleOwnershipError):
        store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-1", "blog"), tenant_id=TENANT)
    with store.legacy_shared_rules():
        with store.legacy_shared_rules():
            pass
        store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-1", "blog"), tenant_id=TENANT)
    assert _skills(store, "rule-1") == ["blog", "email"]


@pytest.mark.integration
def test_a_second_writer_waits_for_the_first_and_is_refused(schema_driver) -> None:
    # The first writer holds the rule's lock and its new skill, uncommitted.
    # The guard of the second takes the same lock before it reads, so it
    # waits for that commit and then sees the first skill. A guard that read
    # first would see no skill yet and write a second one.
    from oms.adapters.neo4j.store import Neo4jGraphStore
    with schema_driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
    store = Neo4jGraphStore(schema_driver)
    store.ensure_schema()
    for skill_id in ("email", "blog"):
        store.upsert_skill(Skill(id=skill_id, name=skill_id, description="", domain="comms", tenant_id=TENANT))
    store.upsert_rule(Rule(id="rule-race", body="Reply within a day.", tenant_id=TENANT))
    first_wrote, outcome = Event(), []

    def second() -> None:
        first_wrote.wait(30)
        try:
            store.attach_edge(Edge(EdgeType.BELONGS_TO, "rule-race", "blog"), tenant_id=TENANT)
            outcome.append("attached")
        except RuleOwnershipError:
            outcome.append("refused")

    thread = Thread(target=second)
    thread.start()
    with schema_driver.session() as session:
        first = session.begin_transaction()
        first.run("MATCH (r:Rule {id:'rule-race', tenant_id:$tenant}), (s:Skill {id:'email', tenant_id:$tenant}) "
                  "SET r._ownership_lock = true REMOVE r._ownership_lock "
                  "MERGE (r)-[:BELONGS_TO]->(s)", tenant=TENANT).consume()
        first_wrote.set()
        time.sleep(1.0)  # the second writer reaches its guard meanwhile
        first.commit()
    thread.join(30)
    assert outcome == ["refused"]
    assert _skills(store, "rule-race") == ["email"]
