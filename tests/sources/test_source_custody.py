"""Source withdrawal and placement reviews preserve real graph custody."""
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import pytest

from oms.domain.models import Edge, Skill, Transaction
from oms.domain.types import EdgeType, SignalType, SourceRuntime
from oms.skills.service import SkillAdminService
from oms.sources.errors import SourceConflict
from oms.sources.models import ApplyRequest, DraftChoice, PlanFlag, SaveDraftRequest
from oms.sources.review import part_fingerprint
from oms.sources.safety import eligibility


def _update(world, key):
    result = world.check(key)
    return world.sources.get_update(result.outcomes[0].update_id, tenant_id="acme")


def _resolve(world, update, choice, *, text=None, key="draft"):
    choices = tuple(DraftChoice(part_id=conflict.part_id, choice=choice,
        merged_text=text if choice == "merged_text" else None,
        part_fingerprint=part_fingerprint(update, conflict.part_id)) for conflict in update.plan.conflicts)
    world.service.save_draft(world.context, SaveDraftRequest(update_id=update.update_id,
        skill=world.skill, fingerprint=update.plan.fingerprint, choices=choices), idempotency_key=key)
    return world.sources.get_update(update.update_id, tenant_id="acme")


def _apply(world, update, key):
    return world.service.apply(world.context, ApplyRequest(update_id=update.update_id,
        skill=world.skill, fingerprint=update.plan.fingerprint), idempotency_key=key)


def _withdraw_keep_edit(world):
    world.install()
    rule = world.store.rules_for_skill("expenses", tenant_id="acme")[0]
    world.incoming = world.package("b", "First description")
    entry = world.incoming.manifest[0]
    body = world.blobs.get(entry.blob_ref).replace(b"- Keep receipts.\n", b"")
    world.incoming = world.incoming.model_copy(update={"manifest": (entry.model_copy(update={
        "blob_ref": world.blobs.put(body), "digest": sha256(body).hexdigest(), "size": len(body)}),)})
    withdrawal = _resolve(world, _update(world, "withdraw"), "keep_oms")
    _apply(world, withdrawal, "keep-withdrawn")
    world.repository.atomic("local-correction", "acme", lambda graph, queue:
        SkillAdminService(store=graph).edit_rule("expenses", rule.id, "acme", "Keep receipts for seven years."))
    world.now += timedelta(minutes=2)
    world.incoming = world.package("c", "First description")
    return rule.id


@pytest.mark.parametrize("choice", ["keep_oms", "use_upstream", "merged_text"])
@pytest.mark.parametrize("retained_claim", [True, False])
def test_reintroduced_kept_rule_requires_decision_and_preserves_correction(source_world, choice, retained_claim):
    world = source_world
    old_id = _withdraw_keep_edit(world)
    if not retained_claim:
        for claim in world.sources.ownership_for_skill(world.skill):
            if claim.entity_ids == (old_id,):
                world.sources.clear_ownership(world.skill, claim.part_id)
    corrected = world.store.get_rule(old_id)
    world.store.upsert_rule(replace(corrected, corroboration_count=3))
    update = _update(world, "reintroduce")
    changes = [change for change in update.plan.changes if change.kind == "rule"]
    assert len(changes) == 1
    assert changes[0].action == "conflict"
    assert changes[0].local.value["body"] == "Keep receipts for seven years."
    assert PlanFlag.CONFLICT in update.plan.flags
    for mode in ("automatic", "bulk"):
        assert eligibility(update.plan, mode=mode, automatic_enabled=True, edition="paid").state != "passed"
    before = world.snapshot()
    with pytest.raises(SourceConflict, match="candidate requires resolution"):
        _apply(world, update, "unresolved")
    assert world.snapshot() == before
    update = _resolve(world, update, choice, text="Keep original receipts for seven years.", key="resolve-reintroduced")
    _apply(world, update, "apply-reintroduced")
    expected = {"keep_oms": "Keep receipts for seven years.", "use_upstream": "Keep receipts.",
                "merged_text": "Keep original receipts for seven years."}[choice]
    rules = world.store.rules_for_skill("expenses", tenant_id="acme")
    assert len(rules) == 1
    assert rules[0].body == expected
    assert world.store.get_rule(old_id).body == "Keep receipts for seven years."
    assert world.store.get_rule(old_id).corroboration_count == 3
    if choice == "keep_oms":
        assert rules[0].id == old_id
    else:
        assert rules[0].id != old_id
        assert rules[0].corroboration_count == 1
        assert rules[0].id in world.store.superseders_of(old_id)
    assert world.baseline_revision() == "c" * 40


def test_reintroduced_rule_preserves_shared_owner_on_upstream_choice(source_world):
    world = source_world
    old_id = _withdraw_keep_edit(world)
    world.store.upsert_skill(Skill(id="other", name="Other", description="", domain="finance", tenant_id="acme"))
    world.store.attach_edge(Edge(EdgeType.BELONGS_TO, old_id, "other"), tenant_id="acme")
    update = _update(world, "reintroduce-shared")
    assert any(change.action == "conflict" for change in update.plan.changes if change.kind == "rule")
    update = _resolve(world, update, "use_upstream", key="choose-shared")
    _apply(world, update, "apply-shared")
    assert [(rule.id, rule.body) for rule in world.store.rules_for_skill("other", tenant_id="acme")] == [
        (old_id, "Keep receipts for seven years.")]
    assert world.store.rules_for_skill("expenses", tenant_id="acme")[0].id != old_id


@pytest.mark.parametrize("shared", [False, True])
def test_identical_wording_reorder_preserves_confirmations(source_world, shared):
    world = source_world
    discovery = world.discoveries.get("discovery", tenant_id="acme", actor_id="reviewer")
    first = world.package("a", "First description", rule="Keep receipts.\n- File expenses monthly.")
    world.discoveries.save(discovery.model_copy(update={"discovery_id": "ordered", "acquired_packages": (first,)}))
    from oms.sources.models import InstallRequest, InstallSelection
    world.service.install(world.context, InstallRequest(discovery_id="ordered", selections=(
        InstallSelection(package_path="", local_name="expenses", domain="finance"),)), idempotency_key="install")
    rule = next(rule for rule in world.store.rules_for_skill("expenses", tenant_id="acme") if rule.body == "Keep receipts.")
    world.store.upsert_rule(replace(rule, corroboration_count=3))
    world.store.upsert_transaction(Transaction(id="human-confirmation", tenant_id="acme",
        signal_type=SignalType.EXPLICIT_CORRECTION, source_runtime=SourceRuntime.MANUAL,
        sanitised_payload_ref="synthetic", timestamp=world.now, source_ref="manual-confirmation"))
    world.store.attach_edge(Edge(EdgeType.DERIVED_FROM, rule.id, "human-confirmation"), tenant_id="acme")
    if shared:
        world.store.upsert_skill(Skill(id="other", name="Other", description="", domain="finance", tenant_id="acme"))
        world.store.attach_edge(Edge(EdgeType.BELONGS_TO, rule.id, "other"), tenant_id="acme")
    lineage = {row.id for row in world.store.lineage(rule.id)}
    world.incoming = world.package("b", "First description", rule="File expenses monthly.\n- Keep receipts.")
    update = _resolve(world, _update(world, "reorder"), "use_upstream")
    _apply(world, update, "apply-reorder")
    actual = next(row for row in world.store.rules_for_skill("expenses", tenant_id="acme") if row.body == rule.body)
    assert actual.corroboration_count == 3
    assert lineage <= {row.id for row in world.store.lineage(actual.id)}
    if shared:
        assert world.store.get_rule(rule.id).corroboration_count == 3
        assert world.store.rules_for_skill("other", tenant_id="acme")[0].id == rule.id
    binding = world.sources.get_binding(world.skill)
    snapshot = world.sources.get_snapshot(binding.baseline)
    part = next(part for part in snapshot.effective_projection if part.kind == "rule" and part.evidence.value["body"] == rule.body)
    mapping = next(row for row in snapshot.graph_mappings if row.part_id == part.part_id)
    placements = world.store.rule_placements_for_section(mapping.section_id, tenant_id="acme")
    assert next(row.order for row in placements if row.rule_id == actual.id) == 1


def test_reintroduced_rule_undo_restores_local_anchor_and_correction(source_world):
    from oms.sources.models import UndoRequest
    world = source_world
    old_id = _withdraw_keep_edit(world)
    update = _resolve(world, _update(world, "reintroduce-undo"), "use_upstream", key="choose-undo")
    result = _apply(world, update, "apply-for-undo")
    assert world.store.get_rule(old_id).body == "Keep receipts for seven years."
    status = world.service.undo_status(world.context, result.operation_id + ":undo")
    assert status.available
    world.service.undo(world.context, UndoRequest(undo_id=status.undo_id, skill=world.skill,
        expected_generations=status.expected_generations, policy_version=status.policy_version), idempotency_key="undo-reintroduced")
    assert [(row.id, row.body) for row in world.store.rules_for_skill("expenses", tenant_id="acme")] == [
        (old_id, "Keep receipts for seven years.")]
    assert world.baseline_revision() == "b" * 40


def test_withdrawn_anchor_survives_without_reusing_stale_ownership_proof(source_world):
    from oms.sources.local_projection import project_local
    world = source_world
    old_id = _withdraw_keep_edit(world)
    claims = world.sources.ownership_for_skill(world.skill)
    claim = next(row for row in claims if row.entity_ids == (old_id,))
    baseline = world.sources.get_snapshot(world.sources.get_binding(world.skill).baseline)
    local = project_local(world.store, world.skill, baseline=baseline, ownership=claims)
    part = next(row for row in local.parts if row.kind == "rule")
    assert part.part_id == claim.part_id
    assert part.owner_origins == ()
    mapping = next(row for row in local.graph_mappings if row.part_id == part.part_id)
    assert mapping.entity_ids == (old_id,)
    assert mapping.custody_digest != claim.proof_digest


def test_selected_rule_alias_cannot_overwrite_unselected_local_entity(source_world):
    from oms.sources.apply import apply_parts
    from oms.sources.local_projection import project_local
    world = source_world
    world.install()
    baseline = world.sources.get_snapshot(world.sources.get_binding(world.skill).baseline)
    part = next(row for row in baseline.effective_projection if row.kind == "rule")
    mapping = next(row for row in baseline.graph_mappings if row.part_id == part.part_id)
    alias = part.model_copy(update={"part_id": "rule:other-source-alias", "evidence": part.evidence.model_copy(
        update={"value": dict(part.evidence.value) | {"body": "Changed through another alias."}})})
    incoming = baseline.model_copy(update={"effective_projection": (*baseline.effective_projection, alias),
        "graph_mappings": (*baseline.graph_mappings, mapping.model_copy(update={"part_id": alias.part_id}))})
    before = world.snapshot()

    def write(graph, queue):
        local = project_local(graph, world.skill, baseline=baseline)
        return apply_parts(world.case.factory(graph, queue), incoming, (alias,), now=world.now,
                           operation_id="aliased-write", local=local)
    with pytest.raises(SourceConflict, match="source unit identity unknown"):
        world.repository.atomic("aliased-write", "acme", write)
    assert world.snapshot() == before


def test_selected_rule_placements_refuse_conflicting_content(source_world):
    from oms.sources.apply import apply_parts
    from oms.sources.local_projection import project_local
    world = source_world
    world.install()
    baseline = world.sources.get_snapshot(world.sources.get_binding(world.skill).baseline)
    part = next(row for row in baseline.effective_projection if row.kind == "rule")
    mapping = next(row for row in baseline.graph_mappings if row.part_id == part.part_id)
    from oms.domain.models import Section
    from oms.domain.types import Mutability, SectionKind
    other_section = Section(id="other-rule-placement", skill_id="expenses", tenant_id="acme",
        heading="Other rules", kind=SectionKind.RULES, order=2, mutability=Mutability.SYSTEM_AGGREGATED)
    world.store.upsert_section(other_section)
    rule = world.store.get_rule(mapping.entity_ids[0])
    world.store.attach_rule(rule, other_section.id, order=0, tenant_id="acme")
    alias = part.model_copy(update={"part_id": "rule:second-placement", "evidence": part.evidence.model_copy(
        update={"value": dict(part.evidence.value) | {"body": "Different guidance.", "section_id": other_section.id}})})
    incoming = baseline.model_copy(update={"effective_projection": (*baseline.effective_projection, alias),
        "graph_mappings": (*baseline.graph_mappings, mapping.model_copy(update={
            "part_id": alias.part_id, "section_id": other_section.id}))})
    before = world.snapshot()

    def write(graph, queue):
        local = project_local(graph, world.skill, baseline=baseline)
        return apply_parts(world.case.factory(graph, queue), incoming, (part, alias), now=world.now,
                           operation_id="inconsistent-placements", local=local)
    with pytest.raises(SourceConflict, match="source rule alias conflict"):
        world.repository.atomic("inconsistent-placements", "acme", write)
    assert world.snapshot() == before


def test_renamed_source_part_keeps_shared_owner_in_mutation_scope(source_world):
    from oms.domain.identity import SkillRef
    from oms.sources.forks import affected_owners
    from oms.sources.local_projection import project_local
    world = source_world
    world.install()
    baseline = world.sources.get_snapshot(world.sources.get_binding(world.skill).baseline)
    part = next(row for row in baseline.effective_projection if row.kind == "rule")
    mapping = next(row for row in baseline.graph_mappings if row.part_id == part.part_id)
    world.store.upsert_skill(Skill(id="other", name="Other", description="", domain="finance", tenant_id="acme"))
    world.store.attach_edge(Edge(EdgeType.BELONGS_TO, mapping.entity_ids[0], "other"), tenant_id="acme")
    local = project_local(world.store, world.skill, baseline=baseline)
    incoming = baseline.model_copy(update={
        "effective_projection": tuple(row.model_copy(update={"part_id": "rule:renamed"})
            if row.part_id == part.part_id else row for row in baseline.effective_projection),
        "graph_mappings": tuple(row.model_copy(update={"part_id": "rule:renamed"})
            if row.part_id == part.part_id else row for row in baseline.graph_mappings)})
    assert affected_owners(local, incoming, {part.part_id}) == (world.skill, SkillRef("acme", "other"))
