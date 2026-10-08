import pytest

from oms.sources.errors import SourceConflict
from oms.sources.models import UndoRequest


def undo_request(world, result):
    record = world.sources.get_undo(result.operation_id + ":undo", tenant_id="acme")
    return UndoRequest(undo_id=record.undo_id, skill=world.skill, expected_generations=record.expected_generations,
                       policy_version=record.policy_version)


def test_undo_restores_only_its_baseline_and_suppresses_reapplication(source_world):
    candidate = source_world.open_update()
    result = source_world.apply(candidate)
    request = undo_request(source_world, result)
    assert source_world.service.undo_status(source_world.context, request.undo_id).available
    undone = source_world.service.undo(source_world.context, request, idempotency_key="undo")
    assert undone.committed
    assert source_world.description() == "First description"
    assert source_world.baseline_revision() == "a" * 40
    assert source_world.sources.candidate_suppressed(candidate.update.plan.origin, "b" * 40)
    assert not source_world.service.undo_status(source_world.context, request.undo_id).available
    assert source_world.service.undo(source_world.context, request, idempotency_key="undo") == undone


def test_later_edit_disables_undo_even_when_text_is_restored(source_world):
    result = source_world.apply(source_world.open_update())
    request = undo_request(source_world, result)
    source_world.edit_description("Later edit")
    source_world.edit_description("Second description")
    assert not source_world.service.undo_status(source_world.context, request.undo_id).available
    with pytest.raises(SourceConflict):
        source_world.service.undo(source_world.context, request, idempotency_key="undo")
    assert source_world.baseline_revision() == "b" * 40


def test_file_undo_restores_exact_bytes_mode_and_provenance(source_world):
    candidate = source_world.open_file_update("bin/check", old_mode="100755", new_mode="100644")
    before = source_world.store.artefacts_for_skill("expenses", tenant_id="acme")[0][0]
    result = source_world.apply(candidate)
    source_world.service.undo(source_world.context, undo_request(source_world, result), idempotency_key="undo")
    restored = source_world.store.artefacts_for_skill("expenses", tenant_id="acme")[0][0]
    assert (restored.id, restored.content_ref, restored.mode, restored.source_ref) == (before.id, before.content_ref, before.mode, before.source_ref)
    assert source_world.blobs.get(restored.content_ref) == b"Original supporting document\n"


def test_undo_shared_example_fork_restores_memberships_and_retained_example(source_world):
    from tests.sources.test_example_reconciliation import example_card
    parent, update, apply_request = example_card(source_world, "Old example", "New example", shared=True)
    before = source_world.store.examples_for_rule(parent.id, tenant_id="acme")[0]
    result = source_world.service.apply(source_world.context, apply_request, idempotency_key="apply")
    source_world.service.undo(source_world.context, undo_request(source_world, result), idempotency_key="undo")
    assert source_world.store.rules_for_skill("expenses", tenant_id="acme")[0].id == parent.id
    assert source_world.store.rules_for_skill("other", tenant_id="acme")[0].id == parent.id
    assert source_world.store.get_example(before.id, tenant_id="acme").body == before.body
    assert source_world.store.examples_for_rule(parent.id, tenant_id="acme")[0].source_ref == before.source_ref
    assert source_world.store.superseders_of(parent.id) == []


def test_failed_required_history_rolls_back_undo_and_keeps_it_available(source_world):
    result = source_world.apply(source_world.open_update())
    request = undo_request(source_world, result)
    before = source_world.snapshot()
    source_world.fail_required_history()
    with pytest.raises(OSError):
        source_world.service.undo(source_world.context, request, idempotency_key="undo")
    assert source_world.snapshot() == before
    assert source_world.sources.get_undo(request.undo_id, tenant_id="acme").undone_by_operation_id is None


def test_undo_restores_retarget_first_reconciliation_without_switching_ref(source_world):
    from tests.sources.test_retargeting import retarget_request
    from oms.sources.models import ApplyRequest
    source_world.install()
    ref = source_world.incoming.resolved_ref.model_copy(update={"kind": "tag", "name": "v2"})
    source_world.incoming = source_world.incoming.model_copy(update={"resolved_ref": ref})
    result = source_world.service.retarget(source_world.context, retarget_request(source_world, ref), idempotency_key="retarget")
    update = source_world.sources.get_update(result.outcomes[0].update_id, tenant_id="acme")
    applied = source_world.service.apply(source_world.context, ApplyRequest(update_id=update.update_id, skill=source_world.skill,
        fingerprint=update.plan.fingerprint), idempotency_key="apply")
    source_world.service.undo(source_world.context, undo_request(source_world, applied), idempotency_key="undo")
    binding = source_world.sources.get_binding(source_world.skill)
    assert binding.ref == ref
    assert binding.first_reconciliation
    assert source_world.baseline_revision() == "a" * 40


def test_undo_uses_recorded_local_file_bytes_instead_of_source_baseline(source_world):
    from dataclasses import replace
    from oms.sources.models import ApplyRequest, DraftChoice, SaveDraftRequest, UpdateRequest
    from oms.sources.review import part_fingerprint
    candidate = source_world.open_file_update("README.md")
    local_bytes = b"Locally revised supporting document\n"

    def edit(graph, queue):
        artefact, path = graph.artefacts_for_skill("expenses", tenant_id="acme")[0]
        graph.upsert_artefact(replace(artefact, content_ref=source_world.blobs.put(local_bytes), size=len(local_bytes),
            source_ref="skill-editor/local-file"), "expenses", path, tenant_id="acme")
    source_world.repository.atomic("local-file", "acme", edit)
    update = candidate.update
    source_world.service.recheck(source_world.context, UpdateRequest(update_id=update.update_id, skill=source_world.skill,
        fingerprint=update.plan.fingerprint), idempotency_key="recheck")
    update = source_world.sources.get_update(update.update_id, tenant_id="acme")
    choice = DraftChoice(part_id="file:README.md", choice="use_upstream", part_fingerprint=part_fingerprint(update, "file:README.md"))
    source_world.service.save_draft(source_world.context, SaveDraftRequest(update_id=update.update_id, skill=source_world.skill,
        fingerprint=update.plan.fingerprint, choices=(choice,)), idempotency_key="draft")
    update = source_world.sources.get_update(update.update_id, tenant_id="acme")
    applied = source_world.service.apply(source_world.context, ApplyRequest(update_id=update.update_id, skill=source_world.skill,
        fingerprint=update.plan.fingerprint), idempotency_key="apply")
    undo = undo_request(source_world, applied)
    assert source_world.service.undo_status(source_world.context, undo.undo_id).available
    source_world.service.undo(source_world.context, undo, idempotency_key="undo")
    restored = source_world.store.artefacts_for_skill("expenses", tenant_id="acme")[0][0]
    assert source_world.blobs.get(restored.content_ref) == local_bytes
    assert restored.source_ref == "skill-editor/local-file"


def test_revoked_undo_permission_returns_no_affected_scope_metadata(source_world, monkeypatch):
    from oms.sources.errors import SourceForbidden
    result = source_world.apply(source_world.open_update())
    undo = undo_request(source_world, result)
    before = source_world.snapshot()

    def admit(action, context, refs):
        if action == "undo":
            raise SourceForbidden("review_permission_revoked")
    monkeypatch.setattr(source_world.policy, "admit", admit)
    status = source_world.service.undo_status(source_world.context, undo.undo_id)
    assert not status.available
    assert status.expected_generations == ()
    assert source_world.snapshot() == before
    with pytest.raises(SourceForbidden):
        source_world.service.undo(source_world.context, undo, idempotency_key="undo")


def test_undo_does_not_reenable_automation(source_world):
    from oms.sources.models import AutomationRequest
    candidate = source_world.open_update()
    binding = source_world.sources.get_binding(source_world.skill)
    source_world.sources.put_binding(binding.model_copy(update={"automatic_apply": True}))
    applied = source_world.apply(candidate)
    guard = source_world.sources.get_generations(source_world.skill)
    source_world.service.configure_automation(source_world.context, AutomationRequest(skill=source_world.skill,
        expected_content_generation=guard.content, expected_binding_generation=guard.binding, enabled=False), idempotency_key="disable-auto")
    source_world.service.undo(source_world.context, undo_request(source_world, applied), idempotency_key="undo")
    assert not source_world.sources.get_binding(source_world.skill).automatic_apply


def test_keep_only_apply_and_undo_are_baseline_decisions_without_content_versions(source_world):
    from oms.sources.models import ApplyRequest, DraftChoice, SaveDraftRequest
    from oms.sources.review import part_fingerprint
    candidate = source_world.open_update()
    update = candidate.update
    versions = source_world.store.skill_versions("acme", "expenses")
    choice = DraftChoice(part_id="field:description", choice="keep_oms", part_fingerprint=part_fingerprint(update, "field:description"))
    source_world.service.save_draft(source_world.context, SaveDraftRequest(update_id=update.update_id, skill=source_world.skill,
        fingerprint=update.plan.fingerprint, choices=(choice,)), idempotency_key="draft")
    update = source_world.sources.get_update(update.update_id, tenant_id="acme")
    applied = source_world.service.apply(source_world.context, ApplyRequest(update_id=update.update_id, skill=source_world.skill,
        fingerprint=update.plan.fingerprint), idempotency_key="apply")
    assert not applied.committed
    assert source_world.baseline_revision() == "b" * 40
    undone = source_world.service.undo(source_world.context, undo_request(source_world, applied), idempotency_key="undo")
    assert not undone.committed
    assert source_world.baseline_revision() == "a" * 40
    assert source_world.store.skill_versions("acme", "expenses") == versions


def rule_fork_update(world, monkeypatch):
    from oms.domain.models import Edge, Rule
    from oms.domain.types import EdgeType
    from oms.sources.models import ApplyRequest
    world.install()
    parent = world.store.rules_for_skill("expenses", tenant_id="acme")[0]
    world.store.upsert_rule(Rule(id="prior-successor", tenant_id="acme", body="Prior correction"))
    world.store.attach_edge(Edge(EdgeType.SUPERSEDES, "prior-successor", parent.id), tenant_id="acme")
    world.incoming = world.package("b", "First description", rule="Keep itemized receipts.")
    original = world.service.builder.build

    def mapped(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        part = next(part for part in snapshot.effective_projection if part.kind == "rule")
        return snapshot.model_copy(update={"graph_mappings": tuple(row.model_copy(update={"entity_ids": (parent.id,)})
            if row.part_id == part.part_id else row for row in snapshot.graph_mappings)})
    monkeypatch.setattr(world.service.builder, "build", mapped)
    checked = world.check()
    update = world.sources.get_update(checked.outcomes[0].update_id, tenant_id="acme")
    applied = world.service.apply(world.context, ApplyRequest(update_id=update.update_id, skill=world.skill,
        fingerprint=update.plan.fingerprint), idempotency_key="apply")
    return parent, applied


def test_undo_removes_only_operation_supersession_edges(source_world, monkeypatch):
    parent, applied = rule_fork_update(source_world, monkeypatch)
    assert len(source_world.store.superseders_of(parent.id)) == 2
    source_world.service.undo(source_world.context, undo_request(source_world, applied), idempotency_key="undo")
    assert source_world.store.superseders_of(parent.id) == ["prior-successor"]


def test_later_historical_supersession_change_disables_undo(source_world, monkeypatch):
    from oms.domain.models import Edge, Rule
    from oms.domain.types import EdgeType
    parent, applied = rule_fork_update(source_world, monkeypatch)
    source_world.store.upsert_rule(Rule(id="later-successor", tenant_id="acme", body="Later correction"))
    source_world.store.attach_edge(Edge(EdgeType.SUPERSEDES, "later-successor", parent.id), tenant_id="acme")
    request = undo_request(source_world, applied)
    assert not source_world.service.undo_status(source_world.context, request.undo_id).available
