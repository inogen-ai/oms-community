"""Tests only: build the graph as it was before rules stopped being shared.

Both stores refuse a second skill for a rule and a placement in another
skill's section (one rule, one skill). Tests of the split migration, and
tests of guards kept for old data (a source fork of a shared rule), need
that old state. The in-memory store opens its guard for the one write; the
Neo4j store is written to directly, as an older OMS left it.

Either store may be the change-tracking view a repository block hands its
operation. A direct Neo4j write still names the skills it reaches first, so
the block advances the same generations a store write would have."""
from oms.domain.models import Edge, Rule
from oms.domain.types import EdgeType
from oms.ports.graph_store import GraphStore
from oms.sources.mutation import reach_skills


def share_rule(store: GraphStore, rule_id: str, skill_id: str, *, tenant_id: str,
               properties: dict | None = None) -> None:
    """Give the rule `skill_id` as one more skill, whatever skill it is in."""
    if not hasattr(store, "_driver"):
        with store.legacy_shared_rules():
            store.attach_edge(Edge(EdgeType.BELONGS_TO, rule_id, skill_id, dict(properties or {})),
                              tenant_id=tenant_id)
        return
    reach_skills(tenant_id, skill_ids=(skill_id,), rule_ids=(rule_id,))
    with store._driver.session() as session:
        written = session.run(
            "MATCH (r:Rule {id:$rule, tenant_id:$tenant}), (s:Skill {id:$skill, tenant_id:$tenant}) "
            "MERGE (r)-[b:BELONGS_TO]->(s) SET b += $props RETURN count(b) AS n",
            rule=rule_id, skill=skill_id, tenant=tenant_id, props=dict(properties or {})).single()["n"]
    assert written == 1, (rule_id, skill_id)


def place_rule(store: GraphStore, rule: Rule, section_id: str, *, tenant_id: str,
               order: int | None = None, group: str | None = None) -> None:
    """Place the rule in `section_id` as well, whatever skill the section is in."""
    if not hasattr(store, "_driver"):
        with store.legacy_shared_rules():
            store.attach_rule(rule, section_id, order=order, group=group, tenant_id=tenant_id)
        return
    section = store.get_section(section_id, tenant_id=tenant_id)
    reach_skills(tenant_id, skill_ids=(section.skill_id,) if section else (), rule_ids=(rule.id,))
    with store._driver.session() as session:
        written = session.run(
            "MATCH (s:Section {id:$section, tenant_id:$tenant}), (r:Rule {id:$rule, tenant_id:$tenant}) "
            "MERGE (s)-[c:CONTAINS_RULE]->(r) SET c.order = $order, c.group = $group RETURN count(c) AS n",
            section=section_id, rule=rule.id, tenant=tenant_id, order=order, group=group).single()["n"]
    assert written == 1, (rule.id, section_id)
