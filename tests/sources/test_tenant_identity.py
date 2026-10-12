"""Tenant-qualified custody retains visible IDs without sharing mutable state."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from oms.domain.models import Artefact, ContentBlock, Edge, Example, Rule, Section, SkillVersion
from oms.domain.types import ArtefactKind, EdgeType, ExampleKind, Mutability, SectionKind, SkillVersionCause
from oms.publish.publisher import Publisher
from tests.community.test_critic_workflow import world, critic_neo4j_driver  # noqa: F401


def package(world, tenant):
    world.skill("expenses", tenant=tenant)
    section = Section(id="section", skill_id="expenses", kind=SectionKind.PROSE,
                      heading="Process", order=0, mutability=Mutability.AUTHORIAL_PASSTHROUGH,
                      tenant_id=tenant)
    world.store.upsert_section(section)
    block = ContentBlock(id="block", content_ref="digest", kind=SectionKind.PROSE,
                         tenant_id=tenant, source_ref="expenses/SKILL.md", body=tenant)
    world.store.upsert_content_block(block)
    world.store.attach_block(block, section.id, tenant_id=tenant)
    world.store.attach_example(Example(id="example", body=tenant, kind=ExampleKind.NEUTRAL,
        tenant_id=tenant, parent_section_id=section.id), tenant_id=tenant)
    artefact = Artefact(id="same-digest", content_ref="digest", kind=ArtefactKind.OTHER,
                       name=tenant, size=3, tenant_id=tenant, source_ref=tenant)
    world.store.upsert_artefact(artefact, "expenses", "file.txt", tenant_id=tenant)


def test_same_visible_skill_id_is_isolated(world):
    world.skill("expenses", tenant="one")
    world.skill("expenses", tenant="two")
    assert world.store.get_skill("expenses", tenant_id="one").tenant_id == "one"
    assert world.store.get_skill("expenses", tenant_id="two").tenant_id == "two"
    assert world.store.get_skill("expenses", tenant_id="absent") is None


def test_owned_content_history_and_deletion_do_not_cross_tenants(world):
    for tenant in ("one", "two"):
        package(world, tenant)
        world.store.append_skill_version(SkillVersion(id=f"version-{tenant}", skill_id="expenses",
            tenant_id=tenant, at=datetime(2026, 1, 1, tzinfo=timezone.utc), revision=tenant, cause=SkillVersionCause.CREATED))
    assert world.store.get_skill_version("version-two", tenant_id="one") is None
    assert world.store.blocks_for_section("section", tenant_id="one")[0].body == "one"
    assert world.store.examples_for_skill("expenses", tenant_id="two")[0].body == "two"
    assert world.store.artefacts_for_skill("expenses", tenant_id="one")[0][0].name == "one"
    world.store.delete_skill("expenses", tenant_id="one")
    assert world.store.get_skill("expenses", tenant_id="two") is not None
    assert world.store.sections_for_skill("expenses", tenant_id="one") == []
    assert world.store.blocks_for_section("section", tenant_id="two")[0].body == "two"
    assert world.store.examples_for_skill("expenses", tenant_id="two")[0].body == "two"
    assert len(world.store.skill_versions("two", "expenses")) == 1
    assert world.store.recent_skill_changes("two")[0].skill_id == "expenses"


def test_same_digest_file_occurrences_keep_independent_metadata(world):
    package(world, "one")
    original = world.store.artefacts_for_skill("expenses", tenant_id="one")[0][0]
    world.store.upsert_artefact(replace(original, name="other", source_ref="other"),
                               "expenses", "other.txt", tenant_id="one")
    assert [(path, item.name) for item, path in world.store.artefacts_for_skill(
        "expenses", tenant_id="one")] == [("file.txt", "one"), ("other.txt", "other")]


def test_foreign_membership_is_rejected_and_shared_rules_survive_deletion(world):
    for tenant in ("one", "two"):
        world.skill("expenses", tenant=tenant)
    world.skill("travel", tenant="one")
    rule = Rule(id="rule-one", body="Keep receipts", tenant_id="one")
    world.store.upsert_rule(rule)
    edge = Edge(type=EdgeType.BELONGS_TO, from_id=rule.id, to_id="expenses")
    with pytest.raises((ValueError, KeyError)):
        world.store.attach_edge(edge, tenant_id="two")
    world.store.attach_edge(edge, tenant_id="one")
    world.store.attach_edge(replace(edge, to_id="travel"), tenant_id="one")
    assert world.store.rules_for_skill("expenses", tenant_id="two") == []
    world.store.detach_edge(edge, tenant_id="two")
    assert len(world.store.rules_for_skill("expenses", tenant_id="one")) == 1
    world.store.delete_skill("expenses", tenant_id="one")
    assert world.store.get_rule(rule.id) is not None
    assert [s.id for s in world.store.skills_for_rule(rule.id, tenant_id="one")] == ["travel"]


def test_rendering_does_not_mutate_stored_skill_description(world):
    world.skill("expenses", tenant="one")
    before = world.store.get_skill("expenses", tenant_id="one")
    before.description = ""
    world.store.upsert_skill(before)
    Publisher(world.store).render_skill("expenses", "one")
    assert world.store.get_skill("expenses", tenant_id="one").description == ""


def test_generic_graph_edges_do_not_leak_between_same_id_nodes(world):
    for tenant in ("one", "two"):
        world.skill("expenses", tenant=tenant)
    world.skill("target", tenant="one")
    world.skill("target", tenant="two")
    world.store.attach_edge(Edge(type=EdgeType.ROUTES_TO, from_id="expenses", to_id="target"),
                            tenant_id="one")
    assert world.store.graph_neighbours("expenses", "two")["nodes"] == []
    assert world.store.graph_neighbours("expenses", "one")["nodes"]


def test_owned_relationships_reject_foreign_parents_before_writing(world):
    package(world, "one")
    world.skill("expenses", tenant="two")
    block = ContentBlock(id="foreign", content_ref="digest", kind=SectionKind.PROSE,
                         tenant_id="two", source_ref="foreign", body="foreign")
    world.store.upsert_content_block(block)
    with pytest.raises((ValueError, KeyError)):
        world.store.attach_block(block, "section", tenant_id="one")
    with pytest.raises((ValueError, KeyError)):
        world.store.upsert_example(Example(id="rejected", body="foreign", kind=ExampleKind.NEUTRAL,
            tenant_id="two", parent_section_id="section"), tenant_id="two")
    assert world.store.examples_for_skill("expenses", tenant_id="two") == []
    assert [b.body for b in world.store.blocks_for_section("section", tenant_id="one")] == ["one"]


def test_example_reparenting_and_equal_order_reads_are_deterministic(world):
    package(world, "one")
    world.skill("travel", tenant="one")
    example = world.store.examples_for_skill("expenses", tenant_id="one")[0]
    world.store.upsert_example(replace(example, parent_section_id=None, parent_skill_id="travel"),
                              tenant_id="one")
    assert world.store.examples_for_section("section", tenant_id="one") == []
    assert [e.id for e in world.store.examples_for_skill("travel", tenant_id="one")] == ["example"]
    section = world.store.get_section("section", tenant_id="one")
    for identifier in ("z", "a"):
        world.store.upsert_section(replace(section, id=identifier))
    assert [s.id for s in world.store.sections_for_skill("expenses", tenant_id="one")] == ["a", "section", "z"]


def test_typed_edges_preserve_other_entity_kinds_on_skill_deletion(world):
    world.skill("same", tenant="one")
    world.skill("other", tenant="one")
    world.store.upsert_rule(Rule(id="same", body="Keep receipts", tenant_id="one"))
    world.store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id="same", to_id="other"), tenant_id="one")
    world.store.delete_skill("same", tenant_id="one")
    assert [s.id for s in world.store.skills_for_rule("same", tenant_id="one")] == ["other"]


def test_global_rule_relationships_reject_foreign_provenance(world):
    from oms.domain.models import Transaction
    from oms.domain.types import SignalType, SourceRuntime

    world.store.upsert_rule(Rule(id="local-rule", body="Keep receipts", tenant_id="one"))
    world.store.upsert_rule(Rule(id="foreign-rule", body="Foreign policy", tenant_id="two"))
    world.store.upsert_transaction(Transaction(id="foreign-transaction", tenant_id="two",
        signal_type=SignalType.EXPLICIT_CORRECTION, source_runtime=SourceRuntime.MANUAL,
        sanitised_payload_ref="fixture", timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    with pytest.raises((ValueError, KeyError)):
        world.store.observe_rule_in("local-rule", "foreign-transaction", "source")
    with pytest.raises((ValueError, KeyError)):
        world.store.upsert_cross_skill_edge(EdgeType.RELATES_TO, "local-rule", "foreign-rule", 1.0)
    assert world.store.cross_skill_neighbours("local-rule") == []


def test_rule_reads_and_input_objects_cannot_write_outside_mutation(world):
    world.skill("expenses", tenant="one")
    rule = Rule(id="detached-rule", body="Original", tenant_id="one")
    world.store.upsert_rule(rule)
    rule.body = "Changed input"
    world.store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id=rule.id, to_id="expenses"), tenant_id="one")
    for read in (lambda: world.store.get_rule(rule.id),
                 lambda: world.store.rules_for_tenant("one")[0],
                 lambda: world.store.rules_page("one").items[0],
                 lambda: world.store.rules_for_skill("expenses", tenant_id="one")[0]):
        projection = read()
        assert projection.body == "Original"
        projection.body = "Uncommitted edit"
        assert world.store.get_rule(rule.id).body == "Original"
    world.store.upsert_rule(projection)
    assert world.store.get_rule(rule.id).body == "Uncommitted edit"
    with pytest.raises(ValueError):
        world.store.upsert_rule(replace(projection, tenant_id="two"))
    assert world.store.get_rule(rule.id).tenant_id == "one"


def test_constraint_projections_do_not_change_policy_until_written(world):
    from oms.domain.models import Constraint

    constraint = Constraint(id="policy", body="Original policy", tenant_id="one", immutable=True)
    world.store.upsert_constraint(constraint)
    constraint.body = "Changed input"
    for rows in (world.store.active_constraints("one"), world.store.all_constraints("one")):
        assert rows[0].body == "Original policy"
        rows[0].body = "Uncommitted policy"
    assert world.store.active_constraints("one")[0].body == "Original policy"


def test_existing_custody_and_transaction_owners_cannot_be_reassigned(world):
    from oms.domain.models import Transaction
    from oms.domain.types import SignalType, SourceRuntime

    package(world, "one")
    world.skill("travel", tenant="one")
    section = world.store.get_section("section", tenant_id="one")
    with pytest.raises(ValueError):
        world.store.upsert_section(replace(section, skill_id="travel"))
    assert world.store.sections_for_skill("travel", tenant_id="one") == []
    transaction = Transaction(id="global-transaction", tenant_id="one",
        signal_type=SignalType.EXPLICIT_CORRECTION, source_runtime=SourceRuntime.MANUAL,
        sanitised_payload_ref="fixture", timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))
    world.store.upsert_transaction(transaction)
    with pytest.raises(ValueError):
        world.store.upsert_transaction(replace(transaction, tenant_id="two"))
    assert world.store.get_transaction(transaction.id).tenant_id == "one"


def test_transaction_inputs_and_reads_cannot_rewrite_stored_provenance(world):
    from oms.domain.models import Transaction
    from oms.domain.types import SignalType, SourceRuntime

    transaction = Transaction(id="detached-transaction", tenant_id="one", person_id="author",
        signal_type=SignalType.EXPLICIT_CORRECTION, source_runtime=SourceRuntime.MANUAL,
        sanitised_payload_ref="fixture", source_ref="original",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))
    world.store.upsert_transaction(transaction)
    transaction.tenant_id = "two"
    assert world.store.get_transaction(transaction.id).tenant_id == "one"
    for read in (lambda: world.store.get_transaction(transaction.id),
                 lambda: world.store.transactions_for_tenant("one")[0],
                 lambda: world.store.transactions_for_person("author", "one")[0],
                 lambda: world.store.pending_transactions(tenant_id="one")[0]):
        projection = read()
        projection.source_ref = "uncommitted"
        projection.tenant_id = "two"
        assert world.store.get_transaction(transaction.id).source_ref == "original"
        assert world.store.get_transaction(transaction.id).tenant_id == "one"
    world.store.upsert_rule(Rule(id="provenance-rule", body="Keep receipts", tenant_id="one"))
    world.store.attach_edge(Edge(type=EdgeType.DERIVED_FROM, from_id="provenance-rule",
                                to_id=transaction.id), tenant_id="one")
    world.store.lineage("provenance-rule")[0].source_ref = "uncommitted"
    assert world.store.get_transaction(transaction.id).source_ref == "original"


def test_ambiguous_graph_roots_do_not_mix_entity_relationships(world):
    world.skill("same", tenant="one")
    world.skill("other", tenant="one")
    world.store.upsert_rule(Rule(id="same", body="Keep receipts", tenant_id="one"))
    world.store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id="same", to_id="other"), tenant_id="one")
    assert world.store.graph_node("same", "one") is None
    assert world.store.graph_neighbours("same", "one")["nodes"] == []


def test_a_skill_and_a_tag_sharing_an_id_each_open_their_own_neighbourhood(world):
    for skill_id in ("security", "audit"):
        world.skill(skill_id, tenant="one")
        world.store.upsert_tag("security", "security")
        world.store.attach_edge(Edge(type=EdgeType.TAGGED_WITH, from_id=skill_id, to_id="security"),
                                tenant_id="one")
    world.store.upsert_tag("billing", "billing")
    world.store.attach_edge(Edge(type=EdgeType.TAGGED_WITH, from_id="audit", to_id="billing"), tenant_id="one")
    assert world.store.graph_node("security", "one")["kind"] == "skill"
    page = world.store.graph_neighbours("security", "one")
    (tag,) = page["nodes"]
    assert tag["kind"] == "tag" and tag["properties"]["id"] == "security"
    assert page["edges"] == [{"source": "security", "target": tag["id"], "type": "TAGGED_WITH"}]
    assert world.store.graph_node(tag["id"], "one") == tag
    assert [node["id"] for node in world.store.graph_neighbours(tag["id"], "one")["nodes"]] == ["audit", "security"]
    assert world.store.graph_node(tag["id"], "two") is None
    assert world.store.graph_neighbours(tag["id"], "two")["nodes"] == []
    # A bare id that names one node still opens it.
    assert world.store.graph_node("billing", "one")["kind"] == "tag"


def test_identical_files_in_two_skills_open_as_separate_occurrences(world):
    licence = Artefact(id="artefact-licence", content_ref="digest", kind=ArtefactKind.OTHER,
                       name="LICENSE", size=3, tenant_id="one", source_ref="LICENSE")
    for skill_id in ("expenses", "travel"):
        world.skill(skill_id, tenant="one")
        world.store.upsert_artefact(licence, skill_id, "legal/LICENSE", tenant_id="one")
    keys = set()
    for skill_id in ("expenses", "travel"):
        page = world.store.graph_neighbours(skill_id, "one")
        (occurrence,) = page["nodes"]
        assert occurrence["kind"] == "artefact" and occurrence["properties"]["id"] == "artefact-licence"
        assert page["edges"] == [{"source": skill_id, "target": occurrence["id"], "type": "HAS_ARTEFACT"}]
        assert world.store.graph_node(occurrence["id"], "one") == occurrence
        assert [node["id"] for node in world.store.graph_neighbours(occurrence["id"], "one")["nodes"]] == [skill_id]
        assert world.store.graph_node(occurrence["id"], "two") is None
        keys.add(occurrence["id"])
    assert len(keys) == 2
    # The shared digest names two occurrences, so it still opens neither.
    assert world.store.graph_node("artefact-licence", "one") is None


@pytest.mark.parametrize("rule_ids", [("block",), ("block", "replacement")])
def test_edge_validation_uses_complete_permitted_pairs(world, rule_ids):
    package(world, "one")
    for rule_id in rule_ids:
        world.store.upsert_rule(Rule(id=rule_id, body="Different entity kind", tenant_id="one"))
    old = world.store.get_content_block("block", tenant_id="one")
    world.store.supersede_block(old.id, replace(old, id="replacement", body="New wording"),
                               "section", tenant_id="one")
    assert [b.body for b in world.store.blocks_for_section("section", tenant_id="one")] == ["New wording"]
    assert world.store.get_rule("block").body == "Different entity kind"


def test_ambiguous_detach_preserves_existing_typed_edges(world):
    package(world, "one")
    for identifier in ("older", "newer"):
        world.store.upsert_rule(Rule(id=identifier, body=identifier, tenant_id="one"))
    edge = Edge(type=EdgeType.SUPERSEDES, from_id="newer", to_id="older")
    world.store.attach_edge(edge, tenant_id="one")
    block = world.store.get_content_block("block", tenant_id="one")
    for identifier in ("older", "newer"):
        world.store.upsert_content_block(replace(block, id=identifier))
    with pytest.raises(ValueError):
        world.store.detach_edge(edge, tenant_id="one")
    assert world.store.superseders_of("older") == ["newer"]


def test_neighbour_pages_follow_the_explorer_key_on_every_adapter(world):
    world.skill("middle", tenant="one")
    for tag in ("bbb", "aaa"):
        world.store.upsert_tag(tag, tag)
        world.store.attach_edge(Edge(type=EdgeType.TAGGED_WITH, from_id="middle", to_id=tag), tenant_id="one")
    world.store.upsert_section(Section(id="s1", skill_id="middle", kind=SectionKind.PROSE, heading="Notes",
                                       order=0, mutability=Mutability.AUTHORIAL_PASSTHROUGH, tenant_id="one"))
    world.store.upsert_artefact(Artefact(id="000-digest", content_ref="digest", kind=ArtefactKind.OTHER,
        name="x.md", size=3, tenant_id="one", source_ref="x.md"), "middle", "x.md", tenant_id="one")
    pages = [world.store.graph_neighbours("middle", "one", offset=offset, limit=2) for offset in (0, 2)]
    assert [[node["id"] for node in page["nodes"]] for page in pages] == [
        ["artefact:middle:x.md", "s1"], ["tag:aaa", "tag:bbb"]]
    assert [(page["total"], page["next_offset"]) for page in pages] == [(4, 2), (4, None)]
    assert [edge["target"] for edge in pages[1]["edges"]] == ["tag:aaa", "tag:bbb"]
