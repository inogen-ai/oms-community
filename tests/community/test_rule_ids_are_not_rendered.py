"""A rule's id never reaches the bytes a skill publishes (one rule, one skill, spec 8.5).

Mutes and the Tier 2 catalogue key on `render.version_of` over the fetch view.
The rule split gives some rules new ids that keep the old id as their prefix.
These tests pin that the rendered text, and so the version, does not change
when only ids change, while the edit anchors, which do carry ids, do.

The renderer prints no id, but it keeps the order in which the stores return
rules and examples, and both stores break ties (equal corroboration count,
equal placement order, no example order) on id. An id that sorted elsewhere
would reorder a published file. So the six rules have equal counts and come in
tied pairs, and the split renames the second rule of each pair. A renamed rule
that led its pair, or stood alone, could not show a reorder. The layouts put
the pairs on every path that breaks a tie on id:

* flat: no sections. All six rules tie in one list.
* sectioned: a prose section and a rules section. Rules 1 and 2 are placed at
  the same order, rules 3 and 4 are in the section with no order, and rules 5
  and 6 are in no section, so they render under "Additional rules".
* flat-placed: the same placements in a rules section with no prose, which the
  publisher renders as a flat document ordered by `_placed_order`.

The skill has no description of its own, so the first rule of the tie also
names the description line. The second rule carries two examples with no order."""

from collections.abc import Sequence

import pytest

from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.models import ContentBlock, Edge, Example, Rule, Section, Skill
from oms.domain.types import EdgeType, ExampleKind, Mutability, SectionKind
from oms.publish.publisher import Publisher
from oms.publish.versions import skill_version

TENANT = "acme"
# The form the split gives a renamed rule: the old id, "-split-" and 12 hex digits.
SUFFIX = "-split-0123456789ab"
BEFORE = ("rule-t1", "rule-t2", "rule-t3", "rule-t4", "rule-t5", "rule-t6")
AFTER = ("rule-t1", "rule-t2" + SUFFIX, "rule-t3", "rule-t4" + SUFFIX, "rule-t5", "rule-t6" + SUFFIX)
EXAMPLES_BEFORE = ("example-aaaaaaaaaaaaaaaa", "example-bbbbbbbbbbbbbbbb")
EXAMPLES_AFTER = tuple(identifier + "-0123456789ab" for identifier in EXAMPLES_BEFORE)
BODIES = (
    "Keep every receipt.",
    "Claim within thirty days.",
    "Use the company card for travel.",
    "Book flights through the travel desk.",
    "Submit mileage once a month.",
    "Attach the approval email.",
)
EXAMPLE_BODIES = ("A taxi receipt.", "A hotel folio.")
# The placement order of rules 1 to 4 in the rules section: 1 and 2 tie, 3 and 4 have none.
SECTION_ORDERS = (0, 0, None, None)
# Each layout, and the renderer `Publisher.outline_skill` reports for it.
RENDERER_OF = {"flat": "flat", "sectioned": "sectioned", "flat-placed": "flat"}
LAYOUTS = tuple(RENDERER_OF)


def _world(rule_ids: Sequence[str], example_ids: Sequence[str], layout: str) -> tuple[InMemoryGraphStore, Skill]:
    store = InMemoryGraphStore()
    skill = Skill(id="expenses", name="Expenses", description="", domain="finance", tenant_id=TENANT)
    store.upsert_skill(skill)
    if layout == "sectioned":
        store.upsert_section(Section(id="expenses-intro", skill_id="expenses", kind=SectionKind.PROSE,
                                     heading="Overview", order=0,
                                     mutability=Mutability.AUTHORIAL_PASSTHROUGH, tenant_id=TENANT))
        block = ContentBlock(id="block-intro", content_ref="sha256-" + "0" * 64, kind=SectionKind.PROSE,
                             tenant_id=TENANT, source_ref="skills/expenses/SKILL.md#overview",
                             body="Claims follow the travel policy.")
        store.upsert_content_block(block)
        store.attach_block(block, "expenses-intro", tenant_id=TENANT)
    if layout != "flat":
        store.upsert_section(Section(id="expenses-rules", skill_id="expenses", kind=SectionKind.RULES,
                                     heading="Rules", order=1,
                                     mutability=Mutability.SYSTEM_AGGREGATED, tenant_id=TENANT))
    # Equal counts, so the stores' tie-break on id is what orders them.
    rules = [Rule(id=rule_id, body=body, tenant_id=TENANT, corroboration_count=2)
             for rule_id, body in zip(rule_ids, BODIES, strict=True)]
    for rule in rules:
        store.upsert_rule(rule)
        store.attach_edge(Edge(EdgeType.BELONGS_TO, rule.id, "expenses"), tenant_id=TENANT)
    if layout != "flat":
        for rule, order in zip(rules[:len(SECTION_ORDERS)], SECTION_ORDERS, strict=True):
            store.attach_rule(rule, "expenses-rules", order=order, tenant_id=TENANT)
    for example_id, body in zip(example_ids, EXAMPLE_BODIES, strict=True):
        # No order, so they render in id order.
        store.upsert_example(Example(id=example_id, body=body, kind=ExampleKind.POSITIVE,
                                     tenant_id=TENANT, parent_rule_id=rule_ids[1]), tenant_id=TENANT)
    return store, skill


def _versions(layout: str) -> tuple[Publisher, Publisher, Skill]:
    before_store, skill = _world(BEFORE, EXAMPLES_BEFORE, layout)
    after_store, _ = _world(AFTER, EXAMPLES_AFTER, layout)
    return Publisher(before_store), Publisher(after_store), skill


def _anchors(publisher: Publisher, skill: Skill) -> list[str]:
    parts, _renderer = publisher.outline_skill(skill.id, TENANT)
    return [part.anchor for part in parts]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_published_text_and_the_version_ignore_rule_ids(layout: str) -> None:
    before, after, skill = _versions(layout)
    assert before.render_package(skill) == after.render_package(skill), layout
    assert skill_version(before, skill) == skill_version(after, skill), layout
    body, _resources = before.fetch_view(skill, before.render_package(skill))
    # The skill renders real content, so the checks above cannot pass on two empty documents.
    assert all(f"* {text}" in body for text in BODIES), layout
    assert all(f"  Example: {text}" in body for text in EXAMPLE_BODIES), layout
    assert not any(identifier in body for identifier in (*BEFORE, *EXAMPLES_BEFORE)), layout


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_edit_anchors_do_carry_rule_ids(layout: str) -> None:
    before, after, skill = _versions(layout)
    assert {f"rule:{rule_id}" for rule_id in BEFORE} <= set(_anchors(before, skill)), layout
    assert {f"rule:{rule_id}" for rule_id in AFTER} <= set(_anchors(after, skill)), layout


@pytest.mark.parametrize("layout", LAYOUTS)
def test_each_layout_takes_the_renderer_it_is_meant_to_exercise(layout: str) -> None:
    publisher, _after, skill = _versions(layout)
    assert publisher.outline_skill(skill.id, TENANT)[1] == RENDERER_OF[layout], layout
