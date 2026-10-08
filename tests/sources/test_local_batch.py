from datetime import timedelta

from oms.domain.identity import SkillRef
from oms.sources import operations
from oms.sources.models import BatchMember, LocalBatchRequest
from tests.sources.test_local_imports import local_request


def test_mixed_batch_returns_durable_applied_and_review_outcomes(source_world):
    world = source_world
    world.install()
    first = local_request(world)
    second = first.model_copy(update={"skill": SkillRef("acme", "another"), "create": True,
        "local_name": "another", "domain": "finance", "expected_content_generation": 0,
        "expected_binding_generation": 0})
    request = LocalBatchRequest(requests=(first, second))
    result = world.service.submit_local_batch(world.context, request, idempotency_key="mixed-upload")
    assert result.state == "awaiting_review" and result.committed
    assert [row.state for row in result.outcomes] == ["awaiting_review", "applied"]
    assert world.store.get_skill("another", tenant_id="acme") is not None
    assert world.description() == "First description"
    assert world.service.get_operation(world.context, result.operation_id) == result
    assert world.service.submit_local_batch(world.context, request, idempotency_key="mixed-upload") == result


def test_batch_admission_failure_does_not_hide_prior_committed_skill(source_world):
    world = source_world
    first = local_request(world, create=True)
    second = first.model_copy(update={"skill": SkillRef("acme", "another"), "local_name": "another"})
    original = world.policy.admit_local
    count = 0

    def revoke_second(context, request):
        nonlocal count
        original(context, request)
        if request.skill == second.skill:
            count += 1
            if count > 1:
                raise PermissionError("grant revoked")
    world.policy.admit_local = revoke_second
    result = world.service.submit_local_batch(world.context, LocalBatchRequest(requests=(first, second)),
                                              idempotency_key="mixed-failure")
    assert result.state == "failed" and result.committed
    assert [row.state for row in result.outcomes] == ["applied", "failed"]
    assert world.store.get_skill("expenses", tenant_id="acme") is not None
    assert world.store.get_skill("another", tenant_id="acme") is None


def test_expired_batch_replay_recovers_committed_child_without_running_missing_work(source_world):
    world = source_world
    first = local_request(world, create=True)
    second = first.model_copy(update={"skill": SkillRef("acme", "another"), "local_name": "another"})
    request = LocalBatchRequest(requests=(first, second))
    parent, _ = world.service._begin(world.context, "local_import", request, "interrupted", (first.skill, second.skill))
    child_id, _ = operations.request_identity(world.context, "local_import", first, "finished-child")
    missing_id, _ = operations.request_identity(world.context, "local_import", second, "missing-child")
    world.sources.put_operation(parent.model_copy(update={"batch_members": (
        BatchMember(child_operation_id=child_id, skill=first.skill),
        BatchMember(child_operation_id=missing_id, skill=second.skill))}))
    world.service.submit_local(world.context, first, idempotency_key="finished-child")
    world.now += timedelta(minutes=11)
    result = world.service.submit_local_batch(world.context, request, idempotency_key="interrupted")
    assert result.state == "failed" and result.committed
    assert [row.state for row in result.outcomes] == ["applied", "failed"]
    assert world.store.get_skill("another", tenant_id="acme") is None
    assert world.sources.get_operation(missing_id, tenant_id="acme") is None
    assert world.service.get_operation(world.context, parent.operation_id) == result
