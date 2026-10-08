from datetime import timedelta

import pytest

from oms.sources.errors import SourceConflict, StaleMutation
from oms.sources.models import RetargetRequest


def retarget_request(world, ref):
    guard = world.sources.get_generations(world.skill)
    return RetargetRequest(skill=world.skill, ref=ref, expected_content_generation=guard.content,
                           expected_binding_generation=guard.binding)


def test_retarget_preserves_live_content_and_base_until_first_reconciliation(source_world):
    source_world.install()
    ref = source_world.incoming.resolved_ref.model_copy(update={"kind": "tag", "name": "v2"})
    source_world.incoming = source_world.incoming.model_copy(update={"resolved_ref": ref})
    result = source_world.service.retarget(source_world.context, retarget_request(source_world, ref), idempotency_key="retarget")
    binding = source_world.sources.get_binding(source_world.skill)
    assert result.outcomes[0].state == "awaiting_review"
    assert source_world.description() == "First description"
    assert source_world.baseline_revision() == "a" * 40
    assert binding.ref == ref
    assert binding.origin.generation == 2
    assert binding.first_reconciliation


def test_forged_tag_commit_is_rejected_before_package_read(source_world):
    source_world.install()
    real = source_world.incoming.resolved_ref.model_copy(update={"kind": "tag", "name": "v2"})
    source_world.incoming = source_world.incoming.model_copy(update={"resolved_ref": real})
    forged = real.model_copy(update={"commit": "f" * 40})
    with pytest.raises(StaleMutation):
        source_world.service.retarget(source_world.context, retarget_request(source_world, forged), idempotency_key="forged")
    assert source_world.reader.reads == 0
    assert source_world.sources.get_binding(source_world.skill).ref.kind == "branch"


def test_failed_retarget_keeps_existing_card_and_binding(source_world, monkeypatch):
    candidate = source_world.open_update()
    before = source_world.snapshot()
    ref = source_world.incoming.resolved_ref.model_copy(update={"kind": "tag", "name": "missing"})

    def missing(request):
        raise ValueError("ref missing")
    monkeypatch.setattr(source_world.reader, "resolve", missing)
    with pytest.raises(ValueError):
        source_world.service.retarget(source_world.context, retarget_request(source_world, ref), idempotency_key="missing")
    assert source_world.snapshot() == before


def test_old_generation_card_cannot_apply_after_retarget(source_world):
    candidate = source_world.open_update()
    ref = source_world.incoming.resolved_ref.model_copy(update={"kind": "tag", "name": "v2"})
    source_world.incoming = source_world.incoming.model_copy(update={"resolved_ref": ref})
    source_world.service.retarget(source_world.context, retarget_request(source_world, ref), idempotency_key="retarget")
    with pytest.raises(StaleMutation):
        source_world.apply(candidate)


def test_profile_permission_is_rechecked_after_acquisition(source_world, monkeypatch):
    source_world.install()
    before = source_world.snapshot()
    ref = source_world.incoming.resolved_ref.model_copy(update={"kind": "tag", "name": "v2"})

    def revoke():
        def denied(*args):
            raise PermissionError("personal profile revoked")
        monkeypatch.setattr(source_world.policy, "admit_profile", denied)
    source_world.reader.before_read = revoke
    with pytest.raises(PermissionError):
        source_world.service.retarget(source_world.context, retarget_request(source_world, ref), idempotency_key="retarget")
    assert source_world.snapshot() == before
