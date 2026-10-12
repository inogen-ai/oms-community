import pytest

from oms.domain.models import ContentBlock, Rule, Section
from oms.domain.types import Mutability, SectionKind


def test_detach_preserves_other_rule_and_block_owners(mutation_case):
    graph, ref = mutation_case.store, mutation_case.skill
    for identifier, mutability in (("rules-a", Mutability.SYSTEM_AGGREGATED), ("rules-b", Mutability.SYSTEM_AGGREGATED),
                                   ("prose-a", Mutability.AUTHORIAL_PASSTHROUGH), ("prose-b", Mutability.AUTHORIAL_PASSTHROUGH)):
        graph.upsert_section(Section(id=identifier, skill_id=ref.skill_id, tenant_id=ref.tenant_id,
            heading=identifier, order=0, kind=SectionKind.RULES if identifier.startswith("rules") else SectionKind.PROSE, mutability=mutability))
    rule = Rule(id="shared", tenant_id=ref.tenant_id, body="Retain")
    graph.upsert_rule(rule)
    for section in ("rules-a", "rules-b"):
        graph.attach_rule(rule, section, tenant_id=ref.tenant_id)
    block = ContentBlock(id="shared", tenant_id=ref.tenant_id, body="Retain", content_ref="digest", source_ref="source", kind=SectionKind.PROSE)
    graph.upsert_content_block(block)
    for section in ("prose-a", "prose-b"):
        graph.attach_block(block, section, tenant_id=ref.tenant_id)
    with pytest.raises(ValueError):
        graph.remove_section("rules-a", tenant_id=ref.tenant_id)
    graph.detach_rule(rule.id, "rules-a", tenant_id=ref.tenant_id)
    graph.detach_block(block.id, "prose-a", tenant_id=ref.tenant_id)
    graph.remove_section("rules-a", tenant_id=ref.tenant_id)
    graph.remove_section("prose-a", tenant_id=ref.tenant_id)
    assert graph.get_rule(rule.id) is not None
    assert [row.id for row in graph.rules_for_section("rules-b", tenant_id=ref.tenant_id)] == [rule.id]
    assert [row.id for row in graph.blocks_for_section("prose-b", tenant_id=ref.tenant_id)] == [block.id]


def test_historical_block_detach_is_explicit_and_preserves_other_section(mutation_case):
    from dataclasses import replace
    from oms.domain.types import BlockStatus
    graph = mutation_case.store
    for identifier in ("one", "two"):
        graph.upsert_section(Section(id=identifier, skill_id="expenses", tenant_id="acme",
            heading=identifier, order=0, kind=SectionKind.PROSE, mutability=Mutability.AUTHORIAL_PASSTHROUGH))
    block = ContentBlock(id="history", body="Historical", content_ref="historical", tenant_id="acme",
        source_ref="old", kind=SectionKind.PROSE, status=BlockStatus.SUPERSEDED)
    graph.upsert_content_block(block)
    for identifier in ("one", "two"):
        graph.attach_block(block, identifier, tenant_id="acme")
    assert graph.blocks_for_section("one", tenant_id="acme") == []
    with pytest.raises(ValueError):
        graph.remove_section("one", tenant_id="acme")
    assert [row.id for row in graph.blocks_for_section("one", tenant_id="acme", include_inactive=True)] == ["history"]
    graph.detach_block("history", "one", tenant_id="acme")
    graph.remove_section("one", tenant_id="acme")
    assert graph.get_content_block("history", tenant_id="acme") is not None
    assert graph.blocks_for_section("two", tenant_id="acme", include_inactive=True)


def test_example_removal_refuses_ambiguous_actual_parents(mutation_case):
    from oms.domain.models import Edge, Example, Skill
    from oms.domain.types import EdgeType, ExampleKind
    graph = mutation_case.store
    graph.upsert_skill(Skill(id="other", name="Other", description="", domain="finance", tenant_id="acme"))
    graph.upsert_example(Example(id="example", body="Retain", kind=ExampleKind.POSITIVE,
        tenant_id="acme", parent_skill_id="expenses"), tenant_id="acme")
    graph.attach_edge(Edge(EdgeType.HAS_EXAMPLE, "other", "example"), tenant_id="acme")
    with pytest.raises(ValueError):
        graph.remove_example("example", tenant_id="acme")
    assert graph.examples_for_skill("expenses", tenant_id="acme")
