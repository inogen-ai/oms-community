"""Link an existing skill to a source, relink it, or retarget its binding to another ref.

The server fetches and retains the package itself before anything is bound, and a
retarget is a change of ref guarded by the binding and content generations like any
other write.
"""
from dataclasses import dataclass

from oms.domain.types import SignalType
from oms.sources import operations, review
from oms.sources.errors import SourceConflict, SourceForbidden, SourceNotFound, StaleMutation
from oms.sources.links import parse_link
from oms.sources.local_projection import project_local
from oms.sources.merge import build_plan
from oms.sources.mutation import source_repository
from oms.sources.models import (
    Binding, DurableEvent, Generations, LinkRequest, LocalState, OperationResult, OriginRef,
    PackageRequest, RefRequest, RetargetRequest, Source, SourceSnapshot, SourceStatus, Update,
)
from oms.sources.primitives import exact_digest


@dataclass(frozen=True)
class LinkState:
    source: Source
    binding: Binding | None
    base: SourceSnapshot | None
    guard: Generations
    local: LocalState
    open_update: Update | None
    policy_revision: str


def eligible_first_link(graph, skill) -> None:
    if not skill.import_source_ref:
        raise SourceConflict("reconciliation_required", "single import origin required")
    rules = {rule.id: rule for rule in graph.rules_for_skill(skill.id, tenant_id=skill.tenant_id)}
    for section in graph.sections_for_skill(skill.id, tenant_id=skill.tenant_id):
        rules.update((rule.id, rule) for rule in graph.rules_for_section(section.id, tenant_id=skill.tenant_id))
    origins = {skill.import_source_ref}
    from oms.domain.identity import SkillRef
    stream = source_repository(graph).get_local_stream(SkillRef(skill.tenant_id, skill.id))
    logical = "source:" + stream.origin.origin_id if stream else None
    for rule in rules.values():
        for transaction in graph.lineage(rule.id):
            if transaction.signal_type is not SignalType.SKILL_IMPORT or not transaction.source_ref:
                continue
            value = transaction.source_ref.partition("#")[0]
            prefix, separator, revision = value.rpartition(":")
            if logical == skill.import_source_ref and separator and revision and prefix == logical:
                value = logical
            origins.add(value)
    if origins != {skill.import_source_ref}:
        raise SourceConflict("reconciliation_required", "single import origin required")


def switch(service, context, request: LinkRequest | RetargetRequest, key: str, action: str) -> OperationResult:
    ref = request.skill
    operation, fresh = service._begin(context, action, request, key, (ref,))
    if not fresh:
        return operation.result
    try:
        def capture(bound):
            skill = bound.graph.get_skill(ref.skill_id, tenant_id=ref.tenant_id)
            if skill is None:
                raise SourceNotFound("skill_not_found")
            guard = bound.sources.get_generations(ref)
            if (guard.content, guard.binding) != (request.expected_content_generation,
                                                  request.expected_binding_generation):
                raise StaleMutation("binding_changed", "binding or content changed")
            binding = bound.sources.get_binding(ref)
            from oms.sources.retirement import retired_binding
            if retired_binding(bound.sources, binding):
                binding = None
            if action == "link":
                if binding is not None:
                    raise SourceConflict("source_already_exists", "use explicit relink")
                eligible_first_link(bound.graph, skill)
            elif binding is None or action == "retarget" and not binding.active:
                raise SourceNotFound("binding_not_found")
            source_id = request.source_id if isinstance(request, LinkRequest) else binding.source_id
            if binding is not None and binding.source_id != source_id:
                raise SourceConflict("binding_changed", "repository source changed")
            source = bound.sources.get_source(source_id, tenant_id=context.tenant_id)
            if source is None:
                raise SourceNotFound("source_not_found")
            base = bound.sources.get_snapshot(binding.baseline) if binding and binding.baseline else None
            local = project_local(bound.graph, ref, baseline=base, content_generation=guard.content,
                ownership=bound.sources.ownership_for_skill(ref), policy_version=service.builder.policy_version)
            return LinkState(source, binding, base, guard, local, bound.sources.open_update(ref),
                             service._policy_revision(context, bound, (ref,)))
        state = service._read(context, action, (ref,), capture)
        origin = (state.binding.origin.model_copy(update={"generation": state.guard.binding + 1})
                  if state.binding else OriginRef(
                      skill=ref, kind="github",
                      origin_id="origin-" + exact_digest(["github-link", ref.storage_key, state.guard.binding]),
                      generation=state.guard.binding + 1))
        if (state.open_update is not None
            and (state.open_update.plan.origin.kind, state.open_update.plan.origin.origin_id) != (origin.kind,
                origin.origin_id)):
            def refuse(bound):
                if capture(bound) != state:
                    raise StaleMutation("binding_changed", "binding or card changed")
                outcome = review.refuse_collision(bound, state.binding.origin if state.binding else origin,
                                                  state.open_update,
                    operation_id=operation.operation_id, actor_id=context.actor_id, action=action)
                return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                    state="blocked", committed=False, outcomes=(outcome,)))
            return service._commit(context, action, operation, (state.guard,), refuse)
        source = state.source
        service.policy.admit_profile(context, source.credential_profile_id)
        allowed = {parse_link(url).repository_url.casefold().removesuffix(".git")
                   for url in (source.canonical_url, *source.confirmed_aliases)}
        if parse_link(request.ref.canonical_url).repository_url.casefold().removesuffix(".git") not in allowed:
            raise SourceForbidden("repository_endpoint_changed")
        path = request.package_path if isinstance(request, LinkRequest) else state.binding.package_path
        resolved = service.reader.resolve(RefRequest(tenant_id=context.tenant_id, url=source.canonical_url,
            credential_profile_id=source.credential_profile_id, ref_kind=request.ref.kind,
            ref_name=request.ref.name, package_path=path))
        if (resolved.kind, resolved.name, resolved.commit) != (request.ref.kind, request.ref.name, request.ref.commit):
            raise StaleMutation("source_changed", "ref changed since preview")
        if parse_link(resolved.canonical_url).repository_url.casefold().removesuffix(".git") not in allowed:
            raise SourceForbidden("resolved_repository_changed")
        acquired = service.reader.read(PackageRequest(tenant_id=context.tenant_id, resolved_ref=resolved,
                                                      package_path=path,
            credential_profile_id=source.credential_profile_id, baseline_commit=state.base.revision
            if state.base else None))
        if acquired.resolved_ref != resolved or acquired.package_path != path:
            raise SourceConflict("acquired_package_changed")
        incoming = service.builder.build(acquired, origin,
            snapshot_id="snapshot-" + exact_digest([operation.operation_id, ref.storage_key]),
            mappings=state.base.graph_mappings if state.base else (), base=state.base)
        check = service.policy.screen(incoming, state.local)
        plan = build_plan(state.base, incoming, state.local, service._merge_policy(incoming, check,
            update_generation=state.open_update.generation + 1 if state.open_update else 1,
            policy_digest=state.policy_revision, first_reconciliation=True, history_evidence=acquired.history_evidence))

        def commit(bound):
            service.policy.admit_profile(context, source.credential_profile_id)
            if capture(bound) != state:
                raise StaleMutation("binding_changed", "binding or policy changed")
            binding = Binding(origin=origin, source_id=source.source_id, package_path=path, ref=resolved,
                baseline=state.base.ref if state.base else None, first_reconciliation=True,
                automatic_apply=state.binding.automatic_apply if state.binding else service.automatic,
                automation_actor_id=state.binding.automation_actor_id if state.binding else context.actor_id,
                status=SourceStatus(state="updates_available", checked_at=service.clock()))
            bound.sources.put_binding(binding)
            outcome = review.stage_update(bound, plan, incoming, operation_id=operation.operation_id,
                actor_id=context.actor_id, action=action, replace_same_origin=True)
            event_id = operation.operation_id + ":" + action
            bound.events.append_event(DurableEvent(event_id=event_id, tenant_id=context.tenant_id,
                operation_id=operation.operation_id, action="source_" + action, skills=(ref,)))
            return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                state="awaiting_review", committed=False, outcomes=(outcome,)), event_ids=(event_id,))
        return service._commit(context, action, operation, (state.guard,), commit)
    except Exception as error:
        service._failed(context, action, operation, (ref,), error=error)
        raise


def retarget(service, context, request, *, idempotency_key):
    return switch(service, context, request, idempotency_key, "retarget")
