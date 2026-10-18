"""A rule's id never reaches the bytes a skill publishes (one rule, one skill, spec 8.5).

Mutes and the Tier 2 catalogue key on `render.version_of` over the fetch view.
The rule split gives some rules new ids that keep the old id as their prefix.
These tests pin that the rendered text, and so the version, does not change
when only ids change, while the edit anchors, which do carry ids, do."""
from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.models import ContentBlock, Edge, Example, Rule, Section, Skill
from oms.domain.types import EdgeType, ExampleKind, Mutability, SectionKind
from oms.publish.publisher import Publisher
from oms.publish.versions import skill_version

TENANT = "acme"
SUFFIX = "-split-0123456789ab"
BEFORE = ("rule-t1", "rule-t2", "rule-t3")
AFTER = ("rule-t1", "rule-t2" + SUFFIX, "rule-t3")
EXAMPLES_BEFORE = ("example-aaaaaaaaaaaaaaaa", "example-bbbbbbbbbbbbbbbb")
EXAMPLES_AFTER = tuple(identifier + "-0123456789ab" for identifier in EXAMPLES_BEFORE)


def _world(rule_ids, example_ids, *, sectioned: bool):
    store = InMemoryGraphStore()
    skill = Skill(id="expenses", name="Expenses", description="Expense claims",
                  domain="finance", tenant_id=TENANT)
    store.upsert_skill(skill)
    if sectioned:
        store.upsert_section(Section(id="expenses-intro", skill_id="expenses", kind=SectionKind.PROSE,
                                     heading="Overview", order=0,
                                     mutability=Mutability.AUTHORIAL_PASSTHROUGH, tenant_id=TENANT))
        block = ContentBlock(id="block-intro", content_ref="sha256-" + "0" * 64, kind=SectionKind.PROSE,
                             tenant_id=TENANT, source_ref="skills/expenses/SKILL.md#overview",
                             body="Claims follow the travel policy.")
        store.upsert_content_block(block)
        store.attach_block(block, "expenses-intro", tenant_id=TENANT)
        store.upsert_section(Section(id="expenses-rules", skill_id="expenses", kind=SectionKind.RULES,
                                     heading="Rules", order=1,
                                     mutability=Mutability.SYSTEM_AGGREGATED, tenant_id=TENANT))
    bodies = ("Keep every receipt.", "Claim within thirty days.", "Use the company card for travel.")
    for rule_id, body in zip(rule_ids, bodies, strict=True):
        # Equal counts, so the renderer's tie-break on id is what orders them.
        store.upsert_rule(Rule(id=rule_id, body=body, tenant_id=TENANT, corroboration_count=2))
        store.attach_edge(Edge(EdgeType.BELONGS_TO, rule_id, "expenses"), tenant_id=TENANT)
    if sectioned:
        store.attach_rule(store.get_rule(rule_ids[0]), "expenses-rules", order=0, tenant_id=TENANT)
    for example_id, body in zip(example_ids, ("A taxi receipt.", "A hotel folio."), strict=True):
        # No order, so they render in id order.
        store.upsert_example(Example(id=example_id, body=body, kind=ExampleKind.POSITIVE,
                                     tenant_id=TENANT, parent_rule_id=rule_ids[1]), tenant_id=TENANT)
    return store, skill


def _versions(sectioned: bool):
    before_store, skill = _world(BEFORE, EXAMPLES_BEFORE, sectioned=sectioned)
    after_store, _ = _world(AFTER, EXAMPLES_AFTER, sectioned=sectioned)
    return Publisher(before_store), Publisher(after_store), skill


def test_the_published_text_and_the_version_ignore_rule_ids():
    for sectioned in (False, True):
        before, after, skill = _versions(sectioned)
        assert before.render_package(skill) == after.render_package(skill), sectioned
        assert skill_version(before, skill) == skill_version(after, skill), sectioned
        body, _resources = before.fetch_view(skill, before.render_package(skill))
        assert not any(identifier in body for identifier in (*BEFORE, *EXAMPLES_BEFORE)), sectioned


def test_the_edit_anchors_do_carry_rule_ids():
    before, after, skill = _versions(True)
    anchors = lambda publisher: [part.anchor for part in publisher.outline_skill(skill.id, TENANT)[0]]
    assert "rule:rule-t2" in anchors(before)
    assert "rule:rule-t2" + SUFFIX in anchors(after)
