"""A flat skill (no content blocks) keeps the author's rule order.

The store lists rules by id, so a headingless import needs its placements
read back; otherwise the rendered SKILL.md and the editor scramble the order.
"""
from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.models import Edge, Rule, Section, Skill
from oms.domain.types import EdgeType, Mutability, SectionKind
from oms.publish.publisher import Publisher

TENANT = "acme"
BODIES = {"a": "Zulu first.", "b": "Yankee second.", "c": "Xray third.", "d": "Whisky fourth."}


def _store(*, placements: dict[str, int | None] | None, counts: dict[str, int] | None = None):
    store = InMemoryGraphStore()
    store.upsert_skill(Skill(id="s", name="S", description="Guidance.", domain=None, tenant_id=TENANT))
    for rid, body in BODIES.items():
        store.upsert_rule(Rule(id=rid, body=body, corroboration_count=(counts or {}).get(rid, 1),
                               tenant_id=TENANT))
        store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id=rid, to_id="s"), tenant_id=TENANT)
    if placements is not None:
        store.upsert_section(Section(id="sec", skill_id="s", kind=SectionKind.RULES, heading="(intro)",
                                     order=0, mutability=Mutability.SYSTEM_AGGREGATED, tenant_id=TENANT))
        for rid, order in placements.items():
            store.attach_rule(store.rules[rid], "sec", order=order, tenant_id=TENANT)
    return store


def _bodies(text: str) -> list[str]:
    return [body for body in BODIES.values() if body in text]


def _order(text: str) -> list[str]:
    return sorted(_bodies(text), key=text.index)


def _outline_text(store) -> str:
    parts, path = Publisher(store).outline_skill("s", TENANT)
    assert path == "flat"
    return "\n".join(line for part in parts for line in part.lines)


def test_placement_order_survives_render_and_outline():
    # c, a, then b (placed, no order), then d (unplaced); ids alone would give a, b, c, d.
    store = _store(placements={"b": None, "a": 1, "c": 0})
    expected = [BODIES[k] for k in "cabd"]
    assert _order(Publisher(store).render_skill("s", TENANT)) == expected
    assert _order(_outline_text(store)) == expected


def test_unplaced_flat_skill_renders_by_corroboration_then_store_order():
    store = _store(placements=None, counts={"c": 5, "d": 5, "a": 2})
    expected = [BODIES[k] for k in "cdab"]
    assert _order(Publisher(store).render_skill("s", TENANT)) == expected
    assert _order(_outline_text(store)) == expected
