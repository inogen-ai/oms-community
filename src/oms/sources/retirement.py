"""Unlink a skill from its source, remove or reconfigure a source, and fence what was retired.

Unlinking deactivates the binding and closes its open update; the installed content
and any other origin's baseline stay. The retirement generations recorded for a
deleted skill keep its archived snapshots and updates from being read as belonging to
a later skill created under the same id.
"""
from oms.domain.identity import SkillRef
from oms.ports.source_store import SourceRepository
from oms.sources import operations, review
from oms.sources.errors import SourceConflict, SourceNotFound, StaleMutation
from oms.sources.models import Binding, DurableEvent, OperationResult, SkillOutcome, SourceStatus, Update


def close_binding(bound, binding, actor_id):
    current = bound.sources.open_update(binding.origin.skill)
    if current is not None and (current.plan.origin.kind, current.plan.origin.origin_id) == ("github",
            binding.origin.origin_id):
        review.close_review(bound, current, "closed", actor_id)
        bound.sources.put_update(current.model_copy(update={"status": "closed"}))
    bound.sources.put_binding(binding.model_copy(update={
        "origin": binding.origin.model_copy(update={"generation": binding.origin.generation + 1}), "active": False}))


def change_binding(service, context, request, key, action):
    operation, fresh = service._begin(context, action, request, key, (request.skill,))
    if not fresh:
        return operation.result
    try:
        def capture(bound):
            binding = bound.sources.get_binding(request.skill)
            guard = bound.sources.get_generations(request.skill)
            if binding is None or not binding.active:
                raise SourceNotFound("binding_not_found")
            if (guard.content, guard.binding) != (request.expected_content_generation,
                                                  request.expected_binding_generation):
                raise StaleMutation("binding_changed", "binding or content changed")
            return binding, guard, bound.sources.open_update(request.skill)
        state = service._read(context, action, (request.skill,), capture)

        def commit(bound):
            if capture(bound) != state:
                raise StaleMutation("binding_changed")
            binding = state[0]
            if action == "unlink":
                close_binding(bound, binding, context.actor_id)
            else:
                bound.sources.put_binding(binding.model_copy(update={"automatic_apply": request.enabled,
                    "automation_actor_id": context.actor_id if request.enabled else binding.automation_actor_id}))
            event_id = operation.operation_id + ":" + action
            bound.events.append_event(DurableEvent(event_id=event_id, tenant_id=context.tenant_id,
                operation_id=operation.operation_id, action="source_" + action, skills=(request.skill,)))
            return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                state="complete", committed=False,
                outcomes=(SkillOutcome(skill=request.skill, state="unchanged"),)), event_ids=(event_id,))
        return service._commit(context, action, operation, (state[1],), commit)
    except Exception as error:
        service._failed(context, action, operation, (request.skill,), error=error)
        raise


def change_source(service, context, request, key, action):
    operation, fresh = service._begin(context, action, request, key, ())
    if not fresh:
        return operation.result
    skills = ()
    try:
        def capture(bound):
            source = bound.sources.get_source(request.source_id, tenant_id=context.tenant_id)
            if source is None:
                raise SourceNotFound("source_not_found")
            if source.generation != request.expected_source_generation:
                raise StaleMutation("source_changed", "source generation changed")
            scope = tuple(SkillRef(context.tenant_id, skill.id)
                          for skill in bound.graph.skills_for_tenant(context.tenant_id))
            bindings = tuple(sorted((binding for ref in scope if (binding := bound.sources.get_binding(ref)) is not None
                                    and binding.active and binding.source_id == source.source_id),
                                    key=lambda row: row.origin.skill))
            guards = tuple(bound.sources.get_generations(binding.origin.skill) for binding in bindings)
            if tuple(sorted(request.expected_generations, key=lambda row: row.skill)) != guards:
                raise StaleMutation("source_changed", "source scope changed")
            return source, bindings, guards
        state = service._read(context, action, (), capture)
        source, bindings, guards = state
        skills = tuple(binding.origin.skill for binding in bindings)
        service.policy.admit(action, context, skills)

        def commit(bound):
            if capture(bound) != state:
                raise StaleMutation("source_changed")
            if action == "remove_source":
                for binding in bindings:
                    close_binding(bound, binding, context.actor_id)
            values = {"generation": source.generation + 1, "scheduled": request.enabled
                      if action == "configure_schedule" else False}
            if action == "remove_source":
                values["status"] = SourceStatus(state="unchecked")
            bound.sources.put_source(source.model_copy(update=values))
            event_id = operation.operation_id + ":" + action
            bound.events.append_event(DurableEvent(event_id=event_id, tenant_id=context.tenant_id,
                operation_id=operation.operation_id, action="source_" + action, skills=skills))
            return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                state="complete", committed=False,
                outcomes=tuple(SkillOutcome(skill=ref, state="unchanged") for ref in skills)), event_ids=(event_id,))
        return service._commit(context, action, operation, guards, commit)
    except Exception as error:
        service._failed(context, action, operation, skills, error=error)
        raise


def retired_update(sources: SourceRepository, update: Update) -> bool:
    fence = sources.get_retirement(update.plan.skill)
    return fence is not None and update.plan.fingerprint.content_generation <= fence.content


def retired_binding(sources: SourceRepository, binding: Binding | None) -> bool:
    if binding is None:
        return False
    fence = sources.get_retirement(binding.origin.skill)
    return fence is not None and binding.origin.generation <= fence.binding
