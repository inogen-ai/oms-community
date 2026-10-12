"""Live projections preserve ownership and never turn live text into historical proof."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from oms.domain.identity import SkillRef
from oms.domain.models import Edge, Rule, Section, Skill, Transaction
from oms.domain.types import EdgeType, Mutability, SectionKind, SignalType, SourceRuntime
from oms.sources.local_projection import project_local
from oms.sources.models import Evidence, SourceSnapshot
from tests.community.test_repository_views import repositories, critic_neo4j_driver  # noqa: F401
from tests.sources.test_package_projection import acquired


@pytest.fixture
def projection_case(repositories, tmp_path):
    store, _ = repositories
    builder, package, origin = acquired(tmp_path, "---\nname: expenses\ndescription: File receipts\n---\n## Rules\n- Keep receipts.\n")
    snapshot = builder.build(package, origin, snapshot_id="snapshot")
    parts = {part.part_id: part for part in snapshot.effective_projection}
    maps = {mapping.part_id: mapping for mapping in snapshot.graph_mappings}
    section = next(part for part in parts.values() if part.kind == "section")
    rule = next(part for part in parts.values() if part.kind == "rule")
    sid, rid = maps[section.part_id].entity_ids[0], maps[rule.part_id].entity_ids[0]
    store.upsert_skill(Skill(id="expenses", name="expenses", description="File receipts", domain="general", tenant_id="acme"))
    store.upsert_section(Section(id=sid, skill_id="expenses", heading="Rules", kind=SectionKind.RULES,
                                order=0, mutability=Mutability.SYSTEM_AGGREGATED, tenant_id="acme"))
    store.upsert_rule(Rule(id=rid, body="Keep receipts.", tenant_id="acme"))
    store.attach_edge(Edge(EdgeType.BELONGS_TO, rid, "expenses"), tenant_id="acme")
    store.attach_rule(store.get_rule(rid), sid, order=0, tenant_id="acme")
    transaction = Transaction(id="source-proof", tenant_id="acme", source_ref="source:origin:" + "a" * 40,
        signal_type=SignalType.SKILL_IMPORT, source_runtime=SourceRuntime.MANUAL, sanitised_payload_ref="retained",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))
    store.upsert_transaction(transaction)
    store.attach_edge(Edge(EdgeType.DERIVED_FROM, rid, transaction.id), tenant_id="acme")
    return store, snapshot, rule.part_id, rid, sid


def test_local_values_match_effective_source_without_reading_rendered_markdown(projection_case):
    store, snapshot, rule_part, _, sid = projection_case
    local = project_local(store, snapshot.ref.origin.skill, baseline=snapshot)
    parts = {part.part_id: part for part in local.parts}
    expected = next(part for part in snapshot.effective_projection if part.part_id == rule_part)
    assert parts[rule_part].evidence.value == expected.evidence.value
    assert parts[rule_part].evidence.value["section_id"] == sid
    assert parts[rule_part].owner_origins == (snapshot.ref.origin.origin_id,)
    assert local.manifest == ()


def test_complete_owner_inventory_includes_section_only_rule_owners(projection_case):
    store, snapshot, rule_part, rid, _ = projection_case
    store.upsert_skill(Skill(id="other", name="Other", description="Other", domain="general", tenant_id="acme"))
    store.upsert_section(Section(id="other-section", skill_id="other", heading="Rules", kind=SectionKind.RULES,
                                order=0, mutability=Mutability.SYSTEM_AGGREGATED, tenant_id="acme"))
    store.attach_rule(store.get_rule(rid), "other-section", order=0, tenant_id="acme")
    local = project_local(store, snapshot.ref.origin.skill, baseline=snapshot)
    mapping = next(mapping for mapping in local.graph_mappings if mapping.part_id == rule_part)
    assert mapping.owner_skills == (SkillRef("acme", "expenses"), SkillRef("acme", "other"))


def test_matching_manual_text_and_unchanged_curated_value_do_not_prove_source_ownership(projection_case):
    store, snapshot, _, _, _ = projection_case
    store.upsert_rule(Rule(id="manual", body="Keep receipts.", tenant_id="acme"))
    store.attach_edge(Edge(EdgeType.BELONGS_TO, "manual", "expenses"), tenant_id="acme")
    skill = store.get_skill("expenses", tenant_id="acme")
    store.upsert_skill(replace(skill, curated=frozenset({"description"})))
    local = project_local(store, snapshot.ref.origin.skill, baseline=snapshot)
    manual = next(mapping for mapping in local.graph_mappings if mapping.entity_ids == ("manual",))
    part = next(part for part in local.parts if part.part_id == manual.part_id)
    assert part.owner_origins == ()
    description = next(part for part in local.parts if part.part_id == "field:description")
    assert description.curated and description.owner_origins == ()


def test_unknown_baseline_stays_unknown_even_when_live_rule_is_known(projection_case):
    store, snapshot, rule_part, _, _ = projection_case
    unknown = SourceSnapshot.model_validate(snapshot.model_dump() | {
        "effective_projection": [part.model_dump() | {"evidence": Evidence(kind="unknown", policy_version="1").model_dump()}
                                 for part in snapshot.effective_projection]})
    local = project_local(store, snapshot.ref.origin.skill, baseline=unknown)
    part = next(part for part in local.parts if part.part_id == rule_part)
    assert part.evidence.kind == "known" and part.evidence.value["body"] == "Keep receipts."
    assert part.owner_origins == ()
    assert all(part.evidence.kind == "unknown" for part in unknown.effective_projection)


def test_known_neighbouring_field_cannot_prove_an_unknown_field_owner(projection_case):
    store, snapshot, _, _, _ = projection_case
    changed = [part.model_copy(update={"evidence": Evidence(kind="unknown", policy_version="1")})
               if part.part_id == "field:description" else part for part in snapshot.effective_projection]
    baseline = snapshot.model_copy(update={"effective_projection": tuple(changed)})
    local = project_local(store, snapshot.ref.origin.skill, baseline=baseline)
    description = next(part for part in local.parts if part.part_id == "field:description")
    assert description.evidence.value == "File receipts"
    assert description.owner_origins == ()


def test_all_proven_rule_origins_are_retained_and_manual_lineage_removes_claim(projection_case):
    store, snapshot, rule_part, rid, _ = projection_case
    other_origin = snapshot.ref.origin.model_copy(update={"origin_id": "second-origin"})
    second = snapshot.model_copy(update={
        "ref": snapshot.ref.model_copy(update={"snapshot_id": "second", "origin": other_origin}),
        "effective_projection": tuple(part.model_copy(update={"owner_origins": ("second-origin",)})
                                      for part in snapshot.effective_projection)})
    transaction = store.get_transaction("source-proof")
    store.upsert_transaction(replace(transaction, id="second-proof", source_ref="source:second-origin:" + "a" * 40))
    store.attach_edge(Edge(EdgeType.DERIVED_FROM, rid, "second-proof"), tenant_id="acme")
    local = project_local(store, snapshot.ref.origin.skill, baseline=snapshot, origins=(second,))
    assert next(part for part in local.parts if part.part_id == rule_part).owner_origins == ("origin", "second-origin")
    store.upsert_transaction(replace(transaction, id="manual-proof", signal_type=SignalType.EXPLICIT_CORRECTION,
                                     source_ref="source:origin:" + "a" * 40))
    store.attach_edge(Edge(EdgeType.DERIVED_FROM, rid, "manual-proof"), tenant_id="acme")
    local = project_local(store, snapshot.ref.origin.skill, baseline=snapshot, origins=(second,))
    assert next(part for part in local.parts if part.part_id == rule_part).owner_origins == ()


def test_legacy_example_context_does_not_create_additional_owners(projection_case):
    from oms.domain.models import Example
    from oms.domain.types import ExampleKind
    store, snapshot, _, rid, sid = projection_case
    store.upsert_skill(Skill(id="other", name="Other", description="Other", domain="general", tenant_id="acme"))
    example = Example(id="legacy-context", body="Original", kind=ExampleKind.NEUTRAL, tenant_id="acme",
                      parent_section_id=sid, parent_rule_id=rid, parent_skill_id="other")
    store.upsert_example(example, tenant_id="acme")
    store.upsert_example(replace(example, body="Updated"), tenant_id="acme")
    assert store.examples_for_rule(rid, tenant_id="acme") == []
    assert store.examples_for_skill("other", tenant_id="acme") == []
    actual = store.examples_for_section(sid, tenant_id="acme")[0]
    assert actual.body == "Updated" and actual.parent_rule_id == rid and actual.parent_skill_id == "other"
    local = project_local(store, snapshot.ref.origin.skill, baseline=snapshot)
    mapping = next(mapping for mapping in local.graph_mappings if mapping.entity_ids == (example.id,))
    assert mapping.owner_skills == (SkillRef("acme", "expenses"),)
    assert mapping.section_id == sid
    store.delete_skill("other", tenant_id="acme")
    assert store.examples_for_section(sid, tenant_id="acme")[0].body == "Updated"


def test_origin_marker_prefix_cannot_claim_another_origin(projection_case):
    store, snapshot, rule_part, _, _ = projection_case
    origin_id = snapshot.ref.origin.origin_id + ":child"
    other_origin = snapshot.ref.origin.model_copy(update={"origin_id": origin_id})
    child = snapshot.model_copy(update={
        "ref": snapshot.ref.model_copy(update={"snapshot_id": "child", "origin": other_origin}),
        "effective_projection": tuple(part.model_copy(update={"owner_origins": (origin_id,)})
                                      for part in snapshot.effective_projection)})
    transaction = store.get_transaction("source-proof")
    store.upsert_transaction(replace(transaction, source_ref=f"source:{origin_id}:" + "a" * 40))
    local = project_local(store, snapshot.ref.origin.skill, baseline=snapshot, origins=(child,))
    assert next(part for part in local.parts if part.part_id == rule_part).owner_origins == (origin_id,)


def test_conflicting_retained_snapshot_identity_is_refused(projection_case):
    store, snapshot, _, _, _ = projection_case
    conflicting = snapshot.model_copy(update={"revision": "b" * 40})
    with pytest.raises(ValueError, match="snapshot"):
        project_local(store, snapshot.ref.origin.skill, baseline=snapshot, origins=(conflicting,))


def test_field_ownership_requires_live_custody_proof_not_comparison_snapshots(projection_case):
    from oms.sources.models import PartOwnership
    store, snapshot, _, _, _ = projection_case
    ref = snapshot.ref.origin.skill
    initial = project_local(store, ref, baseline=snapshot)
    description = next(part for part in initial.parts if part.part_id == "field:description")
    assert description.owner_origins == ()
    mapping = next(mapping for mapping in initial.graph_mappings if mapping.part_id == description.part_id)
    assert mapping.custody_digest
    claim = PartOwnership(skill=ref, part_id=description.part_id, origin_ids=("origin",),
                          entity_ids=mapping.entity_ids, proof_digest=mapping.custody_digest)
    other_origin = snapshot.ref.origin.model_copy(update={"origin_id": "adopted-comparison"})
    adopted = snapshot.model_copy(update={
        "ref": snapshot.ref.model_copy(update={"snapshot_id": "adopted", "origin": other_origin}),
        "effective_projection": tuple(part.model_copy(update={"owner_origins": (other_origin.origin_id,)})
                                      for part in snapshot.effective_projection)})
    local = project_local(store, ref, baseline=snapshot, origins=(adopted,), ownership=(claim,))
    assert next(part for part in local.parts if part.part_id == description.part_id).owner_origins == ("origin",)
    skill = store.get_skill(ref.skill_id, tenant_id=ref.tenant_id)
    store.upsert_skill(replace(skill, curated=frozenset({"description"})))
    local = project_local(store, ref, baseline=snapshot, ownership=(claim,))
    assert next(part for part in local.parts if part.part_id == description.part_id).owner_origins == ()


def test_legacy_import_lineage_cannot_grant_two_origin_claims(projection_case):
    store, snapshot, rule_part, _, _ = projection_case
    ref = snapshot.ref.origin.skill
    skill = store.get_skill(ref.skill_id, tenant_id=ref.tenant_id)
    store.upsert_skill(replace(skill, import_source_ref="legacy/SKILL.md"))
    transaction = store.get_transaction("source-proof")
    transaction.source_ref = "legacy/SKILL.md"
    store.upsert_transaction(transaction)
    other_origin = snapshot.ref.origin.model_copy(update={"origin_id": "other"})
    other = snapshot.model_copy(update={"ref": snapshot.ref.model_copy(update={"origin": other_origin, "snapshot_id": "other"}),
        "effective_projection": tuple(part.model_copy(update={"owner_origins": ("other",)}) for part in snapshot.effective_projection)})
    local = project_local(store, ref, baseline=snapshot, origins=(other,))
    assert next(part for part in local.parts if part.part_id == rule_part).owner_origins == ()
