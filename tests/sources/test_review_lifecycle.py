from dataclasses import replace

import pytest

from oms.sources.models import ApplyRequest, DraftChoice, SaveDraftRequest, UpdateRequest


def request(update):
    return UpdateRequest(update_id=update.update_id, skill=update.plan.skill, fingerprint=update.plan.fingerprint)


def test_skip_suppresses_only_revision_without_moving_baseline(source_world):
    candidate = source_world.open_update()
    result = source_world.service.skip(source_world.context, request(candidate.update), idempotency_key="skip")
    assert not result.committed
    assert source_world.baseline_revision() == "a" * 40
    assert source_world.sources.candidate_suppressed(candidate.update.plan.origin, "b" * 40)
    assert source_world.sources.open_update(source_world.skill) is None


def test_adopt_moves_only_baseline_without_granting_live_ownership(source_world):
    candidate = source_world.open_update()
    ownership = source_world.sources.ownership_for_skill(source_world.skill)
    versions = source_world.store.skill_versions("acme", "expenses")
    source_world.service.adopt(source_world.context, request(candidate.update), idempotency_key="adopt")
    assert source_world.description() == "First description"
    assert source_world.baseline_revision() == "b" * 40
    assert source_world.sources.ownership_for_skill(source_world.skill) == ownership
    assert source_world.store.skill_versions("acme", "expenses") == versions


def test_draft_writes_no_content_and_explicit_upstream_preserves_curation(source_world):
    from oms.sources.review import part_fingerprint
    source_world.install()
    graph = source_world.store

    def curate(store, queue):
        skill = store.get_skill("expenses", tenant_id="acme")
        store.upsert_skill(replace(skill, curated=frozenset({"description"})))
    source_world.repository.atomic("curate", "acme", curate)
    result = source_world.check()
    update = source_world.sources.get_update(result.outcomes[0].update_id, tenant_id="acme")
    before = source_world.description(), source_world.baseline_revision(), source_world.sources.get_generations(source_world.skill)
    choice = DraftChoice(part_id="field:description", choice="use_upstream", part_fingerprint=part_fingerprint(update, "field:description"))
    source_world.service.save_draft(source_world.context, SaveDraftRequest(**request(update).model_dump(), choices=(choice,)), idempotency_key="draft")
    assert (source_world.description(), source_world.baseline_revision(), source_world.sources.get_generations(source_world.skill)) == before
    updated = source_world.sources.get_update(update.update_id, tenant_id="acme")
    source_world.service.apply(source_world.context, ApplyRequest(**request(updated).model_dump()), idempotency_key="apply")
    assert source_world.description() == "Second description"
    assert "description" in graph.get_skill("expenses", tenant_id="acme").curated


def test_adopt_held_candidate_preserves_local_holds_and_ownership(source_world):
    source_world.install()
    source_world.policy.held = True
    result = source_world.check()
    update = source_world.sources.get_update(result.outcomes[0].update_id, tenant_id="acme")
    claims = source_world.sources.ownership_for_skill(source_world.skill)
    source_world.service.adopt(source_world.context, request(update), idempotency_key="adopt-held")
    assert source_world.description() == "First description"
    assert source_world.policy.held
    assert source_world.sources.ownership_for_skill(source_world.skill) == claims
    assert source_world.baseline_revision() == "b" * 40


def test_new_candidate_supersedes_only_after_successful_preparation(source_world):
    from datetime import timedelta
    candidate = source_world.open_update()
    source_world.now += timedelta(seconds=61)
    source_world.incoming = source_world.package("c", "Third description")
    result = source_world.check(key="newer")
    current = source_world.sources.get_update(result.outcomes[0].update_id, tenant_id="acme")
    assert current.plan.incoming != candidate.update.plan.incoming
    assert source_world.sources.get_update(candidate.update.update_id, tenant_id="acme").status == "superseded"
    assert source_world.baseline_revision() == "a" * 40
    assert source_world.description() == "First description"
    assert len(source_world.queue.pending("acme")) == 1
    assert source_world.sources.get_binding(source_world.skill).status.state == "updates_available"


def test_missing_package_preserves_card_and_baseline_with_explicit_status(source_world, monkeypatch):
    from datetime import timedelta
    from oms.sources.git_reader import AcquisitionError
    candidate = source_world.open_update()
    source_world.now += timedelta(seconds=61)

    def missing(request):
        raise AcquisitionError("skill package not found")
    monkeypatch.setattr(source_world.reader, "read", missing)
    with pytest.raises(AcquisitionError):
        source_world.check(key="missing")
    assert source_world.sources.open_update(source_world.skill) == candidate.update
    assert source_world.baseline_revision() == "a" * 40
    assert source_world.sources.get_binding(source_world.skill).status.state == "missing"


def test_failed_skill_check_is_recorded_on_its_binding_and_source(source_world, monkeypatch):
    from oms.sources.errors import SourceConflict
    source_world.install()
    read = source_world.reader.read
    monkeypatch.setattr(source_world.reader, "read", lambda request: read(request).model_copy(update={"package_path": "elsewhere"}))
    with pytest.raises(SourceConflict):
        source_world.check()
    binding = source_world.sources.get_binding(source_world.skill)
    assert (binding.status.state, binding.status.code) == ("failed", "acquired_package_changed")
    assert binding.status.checked_at == source_world.now
    assert source_world.sources.get_source(binding.source_id, tenant_id="acme").status.state == "failed"
    assert source_world.baseline_revision() == "a" * 40


def test_human_merged_prose_keeps_source_baseline_and_manual_custody(source_world):
    from oms.skills.service import SkillAdminService
    from oms.sources.review import part_fingerprint
    source_world.install()
    binding = source_world.sources.get_binding(source_world.skill)
    baseline = source_world.sources.get_snapshot(binding.baseline)
    section_part = next(part for part in baseline.effective_projection if part.kind == "section" and part.evidence.value["heading"] == "Procedure")
    section_id = next(row.entity_ids[0] for row in baseline.graph_mappings if row.part_id == section_part.part_id)
    SkillAdminService(store=source_world.store, repository=source_world.repository).revise_section("expenses", section_id, "acme", "Local prose")
    source_world.incoming = source_world.package("b", "Second description", prose="Upstream prose")
    result = source_world.check()
    update = source_world.sources.get_update(result.outcomes[0].update_id, tenant_id="acme")
    choice = DraftChoice(part_id=section_part.part_id, choice="merged_text", merged_text="Human reviewed prose",
                         part_fingerprint=part_fingerprint(update, section_part.part_id))
    source_world.service.save_draft(source_world.context, SaveDraftRequest(**request(update).model_dump(), choices=(choice,)), idempotency_key="draft")
    updated = source_world.sources.get_update(update.update_id, tenant_id="acme")
    source_world.service.apply(source_world.context, ApplyRequest(**request(updated).model_dump()), idempotency_key="apply")
    assert source_world.store.blocks_for_section(section_id, tenant_id="acme")[0].body == "Human reviewed prose"
    new_base = source_world.sources.get_snapshot(source_world.sources.get_binding(source_world.skill).baseline)
    assert next(part for part in new_base.effective_projection if part.part_id == section_part.part_id).evidence.value["body"] == "Upstream prose"
    assert all(claim.part_id != section_part.part_id for claim in source_world.sources.ownership_for_skill(source_world.skill))
    assert source_world.store.pending_transactions(tenant_id="acme") == []


def test_unchanged_part_draft_carries_to_new_prepared_github_card(source_world):
    from datetime import timedelta
    from oms.sources.review import part_fingerprint
    candidate = source_world.open_update()
    choice = DraftChoice(part_id="field:description", choice="keep_oms", part_fingerprint=part_fingerprint(candidate.update, "field:description"))
    source_world.service.save_draft(source_world.context, SaveDraftRequest(**request(candidate.update).model_dump(), choices=(choice,)), idempotency_key="draft")
    source_world.now += timedelta(seconds=61)
    source_world.incoming = source_world.package("c", "Second description", prose="Another upstream paragraph")
    result = source_world.check(key="newer")
    assert source_world.sources.get_update(result.outcomes[0].update_id, tenant_id="acme").drafts == (choice,)


def test_tag_replacement_detaches_actual_legacy_tag_identity(source_world):
    from oms.domain.models import Edge
    from oms.domain.types import EdgeType
    from oms.sources.review import part_fingerprint
    source_world.install()

    def legacy_identity(graph, queue):
        graph.upsert_tag("opaque-tag-id", "receipts")
        graph.detach_edge(Edge(EdgeType.TAGGED_WITH, "expenses", "receipts"), tenant_id="acme")
        graph.attach_edge(Edge(EdgeType.TAGGED_WITH, "expenses", "opaque-tag-id"), tenant_id="acme")
    source_world.repository.atomic("legacy-tag", "acme", legacy_identity)
    source_world.incoming = source_world.package("b", "First description", tags=("updated",))
    result = source_world.check()
    update = source_world.sources.get_update(result.outcomes[0].update_id, tenant_id="acme")
    choice = DraftChoice(part_id="field:tags", choice="use_upstream", part_fingerprint=part_fingerprint(update, "field:tags"))
    source_world.service.save_draft(source_world.context, SaveDraftRequest(**request(update).model_dump(), choices=(choice,)), idempotency_key="draft")
    update = source_world.sources.get_update(update.update_id, tenant_id="acme")
    source_world.service.apply(source_world.context, ApplyRequest(**request(update).model_dump()), idempotency_key="apply")
    assert [(tag.id, tag.name) for tag in source_world.store.tag_records_for_skill("expenses", tenant_id="acme")] == [("updated", "updated")]
    assert source_world.store.tag_name("opaque-tag-id") == "receipts"


def test_same_candidate_check_preserves_existing_card_and_draft_generation(source_world):
    from datetime import timedelta
    candidate = source_world.open_update()
    source_world.now += timedelta(seconds=61)
    result = source_world.check(key="same-candidate")
    assert result.outcomes[0].update_id == candidate.update.update_id
    assert source_world.sources.open_update(source_world.skill) == candidate.update


def test_resolved_preview_does_not_supply_removal_consent(source_world):
    from oms.sources.errors import SourceConflict
    from oms.sources.local_projection import project_local
    from oms.sources.review import selected_writes
    candidate = source_world.open_file_update("README.md", new_bytes=None)
    update = candidate.update
    from oms.sources.review import part_fingerprint
    choices = tuple(DraftChoice(part_id=conflict.part_id, choice="use_upstream",
        part_fingerprint=part_fingerprint(update, conflict.part_id)) for conflict in update.plan.conflicts)
    source_world.service.save_draft(source_world.context, SaveDraftRequest(**request(update).model_dump(), choices=choices), idempotency_key="draft-removal")
    update = source_world.sources.get_update(update.update_id, tenant_id="acme")
    base = source_world.sources.get_snapshot(update.plan.base)
    incoming = source_world.sources.get_snapshot(update.plan.incoming)
    local = project_local(source_world.store, source_world.skill, baseline=base,
        content_generation=update.plan.fingerprint.content_generation,
        ownership=source_world.sources.ownership_for_skill(source_world.skill))
    before = source_world.snapshot()
    preview = selected_writes(update, incoming, local, (), preview=True)
    assert any(part.part_id == "file:README.md" and part.evidence.kind == "absent" for part in preview[1])
    with pytest.raises(SourceConflict, match="consent"):
        selected_writes(update, incoming, local, ())
    assert source_world.snapshot() == before


def test_proven_mapped_rename_applies_using_aligned_part_identity(source_world, monkeypatch):
    source_world.install()
    base = source_world.sources.get_snapshot(source_world.sources.get_binding(source_world.skill).baseline)
    old_part = next(part for part in base.effective_projection if part.kind == "section" and part.evidence.value["heading"] == "Procedure")
    old_id = next(row.entity_ids[0] for row in base.graph_mappings if row.part_id == old_part.part_id)
    source_world.incoming = source_world.package("b", "First description", procedure_heading="Steps")
    original = source_world.service.builder.build

    def mapped(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        renamed = next(part for part in snapshot.effective_projection if part.kind == "section" and part.evidence.value["heading"] == "Steps")
        return snapshot.model_copy(update={"graph_mappings": tuple(row.model_copy(update={"entity_ids": (old_id,)})
            if row.part_id == renamed.part_id else row for row in snapshot.graph_mappings)})
    monkeypatch.setattr(source_world.service.builder, "build", mapped)
    result = source_world.check()
    update = source_world.sources.get_update(result.outcomes[0].update_id, tenant_id="acme")
    source_world.service.apply(source_world.context, ApplyRequest(**request(update).model_dump()), idempotency_key="apply")
    assert source_world.store.get_section(old_id, tenant_id="acme").heading == "Steps"
    resolved = source_world.sources.get_snapshot(source_world.sources.get_binding(source_world.skill).baseline)
    assert next(part for part in resolved.effective_projection if part.part_id == old_part.part_id).evidence.value["heading"] == "Steps"
