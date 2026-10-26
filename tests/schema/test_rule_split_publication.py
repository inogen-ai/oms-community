"""Acceptance 2: the split changes no published file (core publisher).

The split gives some rules new ids and moves their places, so the proof is the
published bytes themselves, before and after, on a graph built the way OMS 1.5
left it and taken through the real staged split. Two things are compared: what
each skill renders (with the digest the Tier 2 catalogue and the mutes key on),
and every file of the published tree for a tenant the publish gate lets through.

`split_world` supplies the graph. `_world` adds what it leaves out and could
still change a file:

* Tie partners. The renderer breaks ties (equal counts, equal places, no
  example order) on id, and a copy's id sorts right after its original's. Beside
  each tied copy the skill holds two rules of its own whose ids sort just before
  and just after the original's, so a copy id that sorted anywhere else would
  change places with one of them.
* Two skills with no description of their own, `digest` (flat) and `press`
  (sectioned): the published description comes from the first rule of the top
  tie, and reaches the root index and the Tier 2 table.
* A skill that places a rule it does not hold: `press` has a prose block, so it
  renders its sections, and `rule-t-placed` (held by email) sits in one. That
  rule has the highest count, so it and its partners are press's top tie.
* A rule shared by two skills that says what to avoid and belongs on the
  references page, so its copy must keep both.
* A control-plane rule shared by two skills.
* Six examples with no order on the imported rule.

No skill publishes a control-plane rule, nor the held, flagged and superseded
rules `split_world` holds (one of them superseded by a rule of another skill).
They show only as absence: `_assert_published` pins it before the split and the
comparison pins it after. A superseded rule that lost its superseder would read
as absorbed into prose, which only an edition that states the rules in force
renders, so `_in_force` compares that set directly.
"""
import difflib
from pathlib import Path

import pytest
from neo4j import Driver

from oms.adapters.neo4j.store import Neo4jGraphStore
from oms.domain.models import ContentBlock, Edge, Example, Rule, Section, Skill
from oms.domain.types import EdgeType, ExampleKind, Mutability, Plane, Polarity, SectionKind
from oms.publish.gate import GateResult
from oms.publish.publisher import Publisher
from oms.publish.render import RenderedSkill, in_force
from oms.publish.versions import skill_version
from oms.schema.metadata import SchemaManager
from oms.schema.migrations.rule_split import plan_split
from tests.legacy_rules import share_rule
from tests.schema import split_world
from tests.schema.test_critic_schema import schema_driver  # noqa: F401

pytestmark = pytest.mark.integration

ACME, BETA = split_world.TENANT, split_world.OTHER
TENANTS = (ACME, BETA)
IMPORTED = split_world.IMPORTED
# Per (tenant, skill): the rendering, the fetch view, its resources and its version.
Rendered = dict[tuple[str, str], tuple[RenderedSkill, str, tuple[str, ...], str]]

# Where `_world` puts tie partners: (skill, shared rule, section, order, group).
# The skill gets two rules of its own beside the shared rule's copy. They tie
# with it on count when there is no section, and on place when there is one.
_TIES = (
    ("digest", "rule-t-hint", None, None, None),
    ("blog", IMPORTED, "blog-rules", 2, "Voice"),
    ("press", "rule-t-placed", "press-rules", 7, "Orders"),
)
_PARTNERS = ("Open with a greeting.", "End with a thank you.")


def _around(rule_id: str) -> tuple[str, str]:
    """Ids that sort just before, and just after, `rule_id` and every id that extends it."""
    return rule_id[:-1], rule_id[:-1] + chr(ord(rule_id[-1]) + 1)


def _own_rule(store: Neo4jGraphStore, rule_id: str, body: str, skill_id: str, *, count: int = 2,
              **fields: object) -> None:
    store.upsert_rule(Rule(id=rule_id, body=body, tenant_id=ACME, corroboration_count=count, **fields))
    store.attach_edge(Edge(EdgeType.BELONGS_TO, rule_id, skill_id), tenant_id=ACME)
    store.attach_edge(Edge(EdgeType.DERIVED_FROM, rule_id, "t-later"), tenant_id=ACME)


def _world(driver: Driver) -> Neo4jGraphStore:
    """`split_world` (schema 2, rules shared between skills) and the shapes in the module docstring."""
    store = split_world.build(driver)
    with driver.session() as session:
        session.run("MATCH (s:Skill {id:'press', tenant_id:$tenant}) SET s.description = ''", tenant=ACME).consume()
        session.run("MATCH (r:Rule {id:'rule-t-placed', tenant_id:$tenant}) SET r.corroboration_count = 3",
                    tenant=ACME).consume()
    store.upsert_skill(Skill(id="digest", name="Digest", description="", domain="comms", tenant_id=ACME))
    share_rule(store, "rule-t-hint", "digest", tenant_id=ACME)
    store.upsert_section(Section(id="press-prose", skill_id="press", kind=SectionKind.PROSE, heading="Prose",
                                 order=0, mutability=Mutability.AUTHORIAL_PASSTHROUGH, tenant_id=ACME))
    block = ContentBlock(id="block-press-intro", content_ref="sha256-" + "2" * 64, kind=SectionKind.PROSE,
                         tenant_id=ACME, source_ref="skills/press/SKILL.md#overview",
                         body="The press office speaks for the company.")
    store.upsert_content_block(block)
    store.attach_block(block, "press-prose", tenant_id=ACME)
    _own_rule(store, "rule-t-edge", "Promise delivery dates.", "blog", polarity=Polarity.PROSCRIBE,
              reference_only=True)
    share_rule(store, "rule-t-edge", "press", tenant_id=ACME)
    _own_rule(store, "rule-t-ops", "Route refunds to finance.", "email", plane=Plane.CONTROL)
    share_rule(store, "rule-t-ops", "press", tenant_id=ACME)
    for letter, body in zip("cdef", ("Kind regards,", "With thanks,", "Best wishes,", "Warm regards,"), strict=True):
        store.upsert_example(Example(id=f"example-{letter * 16}", body=body, kind=ExampleKind.POSITIVE,
                                     tenant_id=ACME, parent_rule_id=IMPORTED), tenant_id=ACME)
    for skill_id, rule_id, section_id, order, group in _TIES:
        count = store.get_rule(rule_id).corroboration_count
        for partner_id, body in zip(_around(rule_id), _PARTNERS, strict=True):
            _own_rule(store, partner_id, body, skill_id, count=count)
            if section_id is not None:
                store.attach_rule(store.get_rule(partner_id), section_id, order=order, group=group,
                                  tenant_id=ACME)
    return store


def _split(driver: Driver) -> None:
    """The real staged split, and proof that it split something."""
    planned = sum(plan.copies for plan in plan_split(driver))
    assert planned, "the world must hold shared rules"
    metadata = SchemaManager(driver).split_rules(old_writers_stopped=True)
    assert (metadata["schema_version"], metadata["migration_state"]) == (3, "ready")
    with driver.session() as session:
        made = session.run("MATCH (c:Rule) WHERE c.split_from IS NOT NULL RETURN count(c) AS n").single()["n"]
    assert made == planned and plan_split(driver) == ()


def _rendered(driver: Driver, body_budget: int = 200) -> Rendered:
    store = Neo4jGraphStore(driver)
    publisher = Publisher(store, body_budget=body_budget)
    out: Rendered = {}
    for tenant in TENANTS:
        for skill in store.skills_for_tenant(tenant):
            package = publisher.render_package(skill)
            body, resources = publisher.fetch_view(skill, package)
            out[(tenant, skill.id)] = (package, body, tuple(resources), skill_version(publisher, skill))
    return out


def _assert_same(after: Rendered, before: Rendered) -> None:
    """Equal renderings, or the unified diff of every skill whose text changed."""
    assert after.keys() == before.keys()
    changed = []
    for key in sorted(before):
        if after[key] == before[key]:
            continue
        then, now = before[key][0], after[key][0]
        lines = [*difflib.unified_diff(then.skill_md.splitlines(), now.skill_md.splitlines(),
                                       "SKILL.md before", "SKILL.md after", lineterm=""),
                 *difflib.unified_diff((then.references_md or "").splitlines(),
                                       (now.references_md or "").splitlines(),
                                       "references before", "references after", lineterm="")]
        changed.append(f"{key} changed:\n" + ("\n".join(lines) or "only the resources or the version differ"))
    assert not changed, "\n\n".join(changed)


def _tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def _skill_files(root: Path) -> set[str]:
    return {name for name in _tree(root) if name.startswith("skills/")}


# Lines each skill publishes before the split, in its SKILL.md or its references
# page. Equal afterwards is then never two empty documents, and each shape in
# the module docstring is there.
_PUBLISHED = {
    (ACME, "blog"): ("* Write in a formal register.", "  Example: Warm regards,", "* Sign off with the team name.",
                     "* Avoid: Promise delivery dates."),
    (ACME, "email"): ("* Write in a formal register.", "  Example: Warm regards,", "* Sign off with the team name."),
    (ACME, "press"): ("description: Use when handling comms tasks such as: open with a greeting.",
                      "### Orders", "* Quote the order number.", "* Avoid: Promise delivery dates."),
    (ACME, "digest"): ("description: Use when handling comms tasks such as: open with a greeting.",
                       "* Sign off with the team name."),
    (BETA, "blog"): ("* Reply within a day.",),
}
# Held for review, flagged, superseded, control-plane: no skill publishes them.
_UNPUBLISHED = ("Name the product in the first line.", "Use ornate language.", "Use the old footer.",
                "Route refunds to finance.")


def _assert_published(rendered: Rendered) -> None:
    texts = {key: package.skill_md + (package.references_md or "") for key, (package, *_rest) in rendered.items()}
    for key, lines in _PUBLISHED.items():
        missing = [line for line in lines if line not in texts[key]]
        assert not missing, f"{key} does not publish {missing}"
    for key, text in texts.items():
        leaked = [line for line in _UNPUBLISHED if line in text]
        assert not leaked, f"{key} publishes {leaked}"


@pytest.mark.parametrize("body_budget", (200, 2), ids=("default budget", "room for two rules"))
def test_every_skill_renders_byte_for_byte_the_same_after_the_split(schema_driver, body_budget):
    # With room for two rules the cut falls inside a tie, and the rest go to the references page.
    _world(schema_driver)
    before = _rendered(schema_driver, body_budget)
    _assert_published(before)
    _split(schema_driver)
    _assert_same(_rendered(schema_driver, body_budget), before)


def _publish_around_the_split(driver: Driver, tmp_path: Path) -> dict[str, GateResult]:
    """Publish every tenant, split, publish again into a fresh folder: the gate's answer and every
    file must agree. Returns the gates of the first publish."""
    def publish_all(root: Path) -> dict[str, GateResult]:
        publisher = Publisher(Neo4jGraphStore(driver))
        return {tenant: publisher.publish(tenant, root / tenant) for tenant in TENANTS}
    gates = publish_all(tmp_path / "before")
    _split(driver)
    for tenant, after in publish_all(tmp_path / "after").items():
        assert after.passed == gates[tenant].passed, tenant
        assert _tree(tmp_path / "after" / tenant) == _tree(tmp_path / "before" / tenant), tenant
    return gates


def test_every_published_tree_is_byte_for_byte_the_same_after_the_split(schema_driver, tmp_path):
    # acme is blocked by `rule-flagged`'s constraint conflict, before and after the split alike; the
    # next test compares its tree as well.
    _world(schema_driver)
    gates = _publish_around_the_split(schema_driver, tmp_path)
    assert gates[BETA].passed  # at least one tenant's comparison is not vacuous
    assert _skill_files(tmp_path / "before" / BETA) == {"skills/blog/SKILL.md", "skills/email/SKILL.md"}


def test_the_busiest_tenants_tree_is_the_same_once_nothing_blocks_its_publish(schema_driver, tmp_path):
    # Without the link from `rule-flagged` to its constraint, acme publishes.
    _world(schema_driver)
    with schema_driver.session() as session:
        session.run("MATCH (:Rule {id:'rule-flagged'})-[e:CONFLICTS_WITH]->(:Constraint) DELETE e").consume()
    gates = _publish_around_the_split(schema_driver, tmp_path)
    assert all(gate.passed for gate in gates.values())
    assert _skill_files(tmp_path / "before" / ACME) == {
        "skills/blog/SKILL.md", "skills/blog/references/edge-cases.md", "skills/digest/SKILL.md",
        "skills/email/SKILL.md", "skills/press/SKILL.md", "skills/press/references/edge-cases.md"}
    # The derived description is in the root index the trees were compared on.
    index = (tmp_path / "before" / ACME / "AGENTS.md").read_text(encoding="utf-8")
    assert "* **Digest**: Use when handling comms tasks such as: open with a greeting." in index


def _in_force(driver: Driver) -> dict[tuple[str, str], list[str]]:
    """Per skill, the text of the rules its document states (`in_force`)."""
    store = Neo4jGraphStore(driver)
    return {(tenant, skill.id): sorted(rule.body for rule in store.rules_for_skill(skill.id, tenant_id=tenant)
                                       if in_force(rule, store.superseders_of(rule.id)))
            for tenant in TENANTS for skill in store.skills_for_tenant(tenant)}


def test_the_rules_in_force_in_every_skill_are_the_same_after_the_split(schema_driver):
    # A rule that another skill's rule superseded was out of force in every skill it was in, and every
    # copy of it must stay out: each keeps the superseder.
    _world(schema_driver)
    before = _in_force(schema_driver)
    assert "Sign off with the team name." in before[(ACME, "email")]
    assert not any("Use the old footer." in bodies for bodies in before.values())
    _split(schema_driver)
    assert _in_force(schema_driver) == before
