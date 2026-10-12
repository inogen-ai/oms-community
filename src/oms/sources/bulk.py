"""Apply every clean update in one request, each skill in its own guarded transaction.

A skill that fails leaves its peers applied; the result says per skill what happened
and why an excluded one was left out (spec §8.6).
"""
from copy import copy
from dataclasses import replace
import logging

from oms.sources import operations
from oms.sources.apply import capture_apply
from oms.sources.errors import SourceConflict, SourceError, SourceForbidden, SourceNotFound
from oms.sources.models import BatchMember, Operation, OperationResult, PlanFlag, SkillOutcome
from oms.sources.mutation import source_repository
from oms.sources.primitives import exact_digest
from oms.sources.safety import eligibility

logger = logging.getLogger(__name__)


def bulk_apply(service, context, request, *, idempotency_key):
    refs = tuple(row.skill for row in request.updates)
    if not refs or len(set(refs)) != len(refs):
        raise SourceConflict("invalid_request", "bulk requires unique skills")
    if any(ref.tenant_id != context.tenant_id for ref in refs):
        raise SourceForbidden("source_scope_denied", "bulk tenant mismatch")
    admitted = []
    for row in request.updates:
        try:
            service.policy.admit("read", context, (row.skill,))
            service.policy.admit("apply", context, (row.skill,))
            existing = service._read(context, "read", (row.skill,), lambda bound:
                bound.graph.get_skill(row.skill.skill_id, tenant_id=context.tenant_id))
            if existing is None:
                continue
        except (PermissionError, SourceForbidden, SourceNotFound):
            continue
        admitted.append(row)
    rows = tuple(admitted)
    operation, fresh = service._begin(context, "bulk_apply", request, idempotency_key, tuple(row.skill for row in rows))

    def status():
        return service.repository.atomic("source:bulk-status", context.tenant_id, lambda graph, reviews:
            operations.refresh_batch(source_repository(graph),
                                     source_repository(graph).get_operation(operation.operation_id,
                tenant_id=context.tenant_id), service.clock()))
    if not fresh:
        return status().result
    if not rows:
        return service._commit(context, "bulk_apply", operation, (), lambda bound:
            operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                state="complete", committed=False)))
    keys = tuple("bulk-item-" + exact_digest([idempotency_key, row.skill.storage_key, row.update_id]) for row in rows)
    members = tuple(BatchMember(child_operation_id=operations.request_identity(context, "apply", row, key)[0],
                                skill=row.skill)
                    for row, key in zip(rows, keys))

    def initialize(bound):
        if bound.sources.get_operation(operation.operation_id, tenant_id=context.tenant_id) != operation:
            raise SourceConflict("batch_changed", "batch operation changed")
        bound.sources.put_operation(operation.model_copy(update={"batch_members": members}))
    service._read(context, "bulk_apply", (), initialize)
    child_service = copy(service)

    def factory_for(admitted, action, skills):
        original = service.factory_for(admitted, action, skills)

        def factory(graph, reviews):
            bound = original(graph, reviews)

            def admit():
                bound.admit()
                parent = bound.sources.get_operation(operation.operation_id, tenant_id=context.tenant_id)
                if (parent is None or parent.result.state in operations.TERMINAL or parent.lease != operation.lease
                        or parent.lease.expires_at <= service.clock()):
                    raise SourceConflict("operation_no_longer_active", "batch no longer active")
            return replace(bound, admit=admit)
        return factory
    child_service.factory_for = factory_for
    for row, key in zip(rows, keys):
        if status().result.state in operations.TERMINAL:
            break
        code = None
        try:
            update, holder, incoming, local, guards = child_service._read(context, "apply", (row.skill,),
                lambda bound: capture_apply(child_service, context, row, bound))
            service.policy.admit("apply", context, tuple(guard.skill for guard in guards))
            check = eligibility(update.plan, mode="bulk", automatic_enabled=False, edition="community")
            if check.state != "passed":
                code = ("script_changes" if PlanFlag.SCRIPT_CHANGES in update.plan.flags else check.reasons[0]
                        if check.reasons else check.code)
            else:
                child_service.apply(context, row, idempotency_key=key)
        except (PermissionError, SourceForbidden):
            code = "not_authorized"
        except SourceError as error:
            code = error.code
        except Exception as error:
            logger.warning("Bulk source apply failed", extra={"operation_id": operation.operation_id,
                "skill_id": row.skill.skill_id, "error_type": type(error).__name__})
            code = "source_operation_failed"
        if code:
            def fail(graph, reviews):
                sources = source_repository(graph)
                identifier, digest = operations.request_identity(context, "apply", row, key)
                if sources.get_operation(identifier, tenant_id=context.tenant_id) is None:
                    sources.reserve_operation(Operation(operation_id=identifier, context=context, idempotency_key=key,
                        request_digest=digest, result=OperationResult(operation_id=identifier, state="blocked",
                                                                      committed=False,
                            outcomes=(SkillOutcome(skill=row.skill, state="blocked", update_id=row.update_id,
                                                   code=code),))))
            service.repository.atomic("source:bulk-excluded", context.tenant_id, fail)
        status()
    return status().result
