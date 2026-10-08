import pytest

from oms.domain.models import Edge, Skill
from oms.domain.types import EdgeType
from oms.sources.models import DiscoveryResult, DiscoveredPackage, InstallRequest, InstallSelection, ApplyRequest, DraftChoice, SaveDraftRequest
from oms.sources.review import part_fingerprint
from tests.sources.test_review_lifecycle import request


def example_card(world, before, after, *, shared):
    from datetime import timedelta
    first = world.package("a", "First description", example=before)
    world.discoveries.save(DiscoveryResult(discovery_id="examples", tenant_id="acme", actor_id="reviewer",
        resolved_ref=first.resolved_ref, expires_at=world.now + timedelta(hours=1),
        packages=(DiscoveredPackage(path="", valid=True),), acquired_packages=(first,)))
    world.service.install(world.context, InstallRequest(discovery_id="examples",
        selections=(InstallSelection(package_path="", local_name="expenses", domain="finance"),)), idempotency_key="install")
    parent = world.store.rules_for_skill("expenses", tenant_id="acme")[0]
    if shared:
        def share(graph, queue):
            graph.upsert_skill(Skill(id="other", name="Other", description="", domain="finance", tenant_id="acme"))
            graph.attach_edge(Edge(EdgeType.BELONGS_TO, parent.id, "other"), tenant_id="acme")
        world.repository.atomic("share", "acme", share)
    world.incoming = world.package("b", "First description", example=after)
    result = world.check()
    update = world.sources.get_update(result.outcomes[0].update_id, tenant_id="acme")
    choices = tuple(DraftChoice(part_id=conflict.part_id, choice="use_upstream",
        part_fingerprint=part_fingerprint(update, conflict.part_id)) for conflict in update.plan.conflicts)
    if choices:
        world.service.save_draft(world.context, SaveDraftRequest(**request(update).model_dump(), choices=choices), idempotency_key="draft")
        update = world.sources.get_update(update.update_id, tenant_id="acme")
    consents = tuple(change.part_id for change in update.plan.changes if change.incoming.kind == "absent")
    return parent, update, ApplyRequest(**request(update).model_dump(), removal_consents=consents)


def test_accepted_example_deletion_is_applied_as_one_update(source_world):
    parent, update, apply_request = example_card(source_world, "Old example", None, shared=False)
    result = source_world.service.apply(source_world.context, apply_request, idempotency_key="apply")
    assert result.committed
    assert source_world.store.examples_for_rule(parent.id, tenant_id="acme") == []
    assert source_world.baseline_revision() == "b" * 40


@pytest.mark.parametrize("before,after", [(None, "Added example"), ("Old example", "Edited example"), ("Old example", None)])
def test_example_only_reconciliation_forks_shared_parent_without_changing_other_owner(source_world, before, after):
    parent, update, apply_request = example_card(source_world, before, after, shared=True)
    other_before = source_world.store.examples_for_rule(parent.id, tenant_id="acme")
    result = source_world.service.apply(source_world.context, apply_request, idempotency_key="apply")
    other_parent = source_world.store.rules_for_skill("other", tenant_id="acme")[0]
    own_parent = source_world.store.rules_for_skill("expenses", tenant_id="acme")[0]
    assert other_parent.id == parent.id
    assert own_parent.id != parent.id
    assert source_world.store.examples_for_rule(parent.id, tenant_id="acme") == other_before
    assert [row.body for row in source_world.store.examples_for_rule(own_parent.id, tenant_id="acme")] == ([after] if after else [])
    undo = source_world.sources.get_undo(result.operation_id + ":undo", tenant_id="acme")
    assert any(row.before.kind == "known" and row.after.kind == "known" and row.before.value.get("kind") == "rule"
               and row.before.value["entity_id"] != row.after.value["entity_id"] for row in undo.custody)
    assert not any(claim.entity_ids == (own_parent.id,) for claim in source_world.sources.ownership_for_skill(source_world.skill))
