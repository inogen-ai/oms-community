"""Operations: the durable record of each request, its identity, its lease and its result.

A request is identified by actor, key and digest so a retry finds its earlier
operation; a batch operation gathers its children's results when refreshed and never
starts or repeats a child's work.
"""
from datetime import timedelta
from math import ceil
from uuid import uuid4

from oms.domain.identity import SkillRef
from oms.domain.ids import slug

from oms.sources.errors import CheckThrottled, SourceConflict
from oms.sources.models import (CheckThrottle, InstallRequest, Lease, LocalBatchRequest, LocalImportRequest,
                                Operation, OperationResult, OperationScope, SkillOutcome)
from oms.sources.primitives import exact_digest


TERMINAL = frozenset({"complete", "blocked", "failed", "awaiting_review"})


def capture_scopes(bound, context, request, skills) -> tuple[OperationScope, ...]:
    proposed = {}
    if isinstance(request, InstallRequest):
        proposed = {SkillRef(context.tenant_id, slug(row.local_name)): row.domain for row in request.selections
                    if slug(row.local_name)}
    local = (request.requests if isinstance(request, LocalBatchRequest) else (request,)
             if isinstance(request, LocalImportRequest) else ())
    proposed.update({row.skill: row.domain for row in local if row.create})
    result = []
    for ref in sorted(set(skills) | proposed.keys()):
        skill = bound.graph.get_skill(ref.skill_id, tenant_id=context.tenant_id)
        domain = skill.domain if skill is not None else proposed.get(ref)
        if domain is not None:
            result.append(OperationScope(skill=ref, domain=domain,
                content_generation=bound.sources.get_generations(ref).content))
    return tuple(result)


def refresh_batch(sources, operation: Operation, now) -> Operation:
    """Recover durable per-skill results without running any missing child work."""
    if not operation.batch_members or operation.result.state in TERMINAL:
        return operation
    expired = operation.lease is None or operation.lease.expires_at <= now
    outcomes, complete, committed = [], True, False
    for member in operation.batch_members:
        child = sources.get_operation(member.child_operation_id, tenant_id=operation.context.tenant_id)
        if child is None or child.result.state not in TERMINAL:
            complete = False
            if expired:
                outcomes.append(SkillOutcome(skill=member.skill, state="failed", code="batch_interrupted"))
            continue
        if child.context.actor_id != operation.context.actor_id:
            raise SourceConflict("batch_changed", "batch member identity changed")
        committed |= child.result.committed
        found = next((row for row in child.result.outcomes if row.skill == member.skill), None)
        outcomes.append(found or SkillOutcome(skill=member.skill, state="failed", code="batch_item_failed"))
    if complete or expired:
        states = {row.state for row in outcomes}
        state = next((candidate for candidate in ("failed", "blocked", "awaiting_review")
                      if candidate in states), "complete")
        result = OperationResult(operation_id=operation.operation_id, state=state, committed=committed,
                                 outcomes=tuple(outcomes))
    else:
        result = OperationResult(operation_id=operation.operation_id, state="planning", committed=False,
                                 outcomes=tuple(outcomes))
    updated = operation.model_copy(update={"result": result})
    if updated != operation:
        sources.put_operation(updated)
    return updated


def request_identity(context, action, request, key):
    if not key or len(key) > 512:
        raise ValueError("An idempotency key of at most 512 characters is required")
    digest = exact_digest({"action": action, "request": request.model_dump(mode="json")})
    identity = exact_digest([context.tenant_id, context.actor_id, key])
    return "source-operation-" + identity, digest


def begin(sources, context, action, request, key, now, *, throttle_source=None, scope_proofs=(), upload_replay=None):
    identifier, digest = request_identity(context, action, request, key)
    old = sources.operation_for_key(key, tenant_id=context.tenant_id, actor_id=context.actor_id)
    if old is not None:
        if old.request_digest != digest:
            raise SourceConflict("idempotency_key_reused")
        old = refresh_batch(sources, old, now)
        if old.result.state not in TERMINAL and old.lease is not None and old.lease.expires_at <= now:
            old = old.model_copy(update={"result": OperationResult(operation_id=old.operation_id,
                state="failed", committed=False)})
            sources.put_operation(old)
        return old, False
    if throttle_source is not None:
        throttle = CheckThrottle(tenant_id=context.tenant_id, source_id=throttle_source,
            actor_id=context.actor_id, next_allowed_at=now + timedelta(seconds=60))
        if not sources.claim_manual_check(throttle, now=now):
            prior = sources.get_manual_check(throttle_source, tenant_id=context.tenant_id)
            raise CheckThrottled(max(1, ceil((prior.next_allowed_at - now).total_seconds())))
    operation = Operation(operation_id=identifier, context=context, idempotency_key=key,
        request_digest=digest, scope_proofs=scope_proofs, upload_replay=upload_replay,
        result=OperationResult(operation_id=identifier, state="fetching", committed=False),
        lease=Lease(tenant_id=context.tenant_id, resource_id=identifier, owner_id=context.actor_id,
                    run_id=str(uuid4()), fencing_token=1, expires_at=now + timedelta(minutes=10)))
    return sources.reserve_operation(operation), True


def finish(sources, operation, result, *, history_ids=(), event_ids=(), discovery_id=None):
    current = sources.get_operation(operation.operation_id, tenant_id=operation.context.tenant_id)
    if current is None or current.lease != operation.lease or current.result.state in TERMINAL:
        raise SourceConflict("operation_no_longer_active")
    sources.put_operation(operation.model_copy(update={"result": result,
        "history_ids": tuple(history_ids), "event_ids": tuple(event_ids), "discovery_id": discovery_id}))
    return result


def refresh(service, context, operation, *, action, skills):
    def record(graph, reviews):
        bound = service.factory_for(context, action, skills)(graph, reviews)
        snapshots = [(participant, participant.snapshot_state()) for participant in bound.rollback_participants]
        try:
            current = bound.sources.get_operation(operation.operation_id, tenant_id=context.tenant_id)
            updated = refresh_batch(bound.sources, current, service.clock())
            # Bookkeeping may record failures after grants were revoked; it cannot start new work.
            if service.completion_hook is not None:
                service.completion_hook(bound, updated.result)
            return updated
        except BaseException:
            for participant, snapshot in reversed(snapshots):
                participant.restore_state(snapshot)
            raise
    return service.repository.metadata('source:batch-status', context.tenant_id, record)


def admitted_result(service, context, operation):
    service._read(context, 'operation', (), lambda bound:
        service.policy.admit_operation(context, operation, bound.graph))
    return operation.result
