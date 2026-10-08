"""Move a source to a new repository address. An operator's explicit decision, applied to the source and every
binding at once, without fetching anything."""
from dataclasses import dataclass

from oms.domain.identity import SkillRef
from oms.sources import operations
from oms.sources.scoped import policy_for, with_admission
from oms.sources.errors import SourceConflict, SourceNotFound, StaleMutation
from oms.sources.links import LinkError, parse_link
from oms.sources.models import Binding, DurableEvent, Generations, OperationResult, SkillOutcome, Source, SourceStatus


@dataclass(frozen=True)
class RelocationState:
    source: Source
    bindings: tuple[Binding, ...]
    guards: tuple[Generations, ...]


def destination(raw):
    link = parse_link(raw)
    if link.ref_path or link.explicit_package or '?' in raw or '#' in raw:
        raise LinkError('relocation requires a GitHub repository root')
    return link.repository_url


def _capture(service, context, request, bound, *, authorise=True):
    source = bound.sources.get_source(request.source_id, tenant_id=context.tenant_id)
    if source is None:
        raise SourceNotFound('source_not_found')
    bindings = bound.sources.active_bindings(source.source_id, tenant_id=context.tenant_id)
    refs = tuple(row.origin.skill for row in bindings)
    if authorise:
        policy = policy_for(service, bound)
        policy.admit('relocate_source', context, refs)
        policy.admit_profile(context, source.credential_profile_id)
    return RelocationState(source, bindings, tuple(bound.sources.get_generations(ref) for ref in refs))


def relocate(service, context, request, *, idempotency_key):
    endpoint = destination(request.destination_url)

    def prepare(bound):
        state = _capture(service, context, request, bound)
        previous = bound.sources.operation_for_key(idempotency_key, tenant_id=context.tenant_id,
                                                   actor_id=context.actor_id)
        if previous is None:
            if state.source.generation != request.expected_source_generation:
                raise StaleMutation('source_changed', 'source generation changed')
            if tuple(sorted(request.expected_generations, key=lambda row: row.skill)) != state.guards:
                raise StaleMutation('source_changed', 'source scope changed')
        else:
            service.policy.admit_operation(context, previous, bound.graph)
        refs = tuple(row.skill for row in state.guards)
        operation, fresh = operations.begin(bound.sources, context, 'relocate_source', request,
                                            idempotency_key, service.clock(),
            scope_proofs=operations.capture_scopes(bound, context, request, refs))
        return state, operation, fresh
    state, operation, fresh = service._read(context, 'relocate_source', (), prepare)
    if not fresh:
        return operation.result
    refs = tuple(row.skill for row in state.guards)
    try:
        def commit(bound):
            if _capture(service, context, request, bound, authorise=False) != state:
                raise StaleMutation('source_changed')
            if parse_link(state.source.canonical_url).repository_url == endpoint:
                return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                    state='complete', committed=False, source_id=state.source.source_id,
                    outcomes=tuple(SkillOutcome(skill=ref, state='unchanged', code='repository_unchanged')
                                   for ref in refs)))
            aliases = tuple(sorted({*state.source.confirmed_aliases, state.source.canonical_url, endpoint}))
            status = SourceStatus(state='unchecked')
            bound.sources.put_source(state.source.model_copy(update={'canonical_url': endpoint,
                                                                     'confirmed_aliases': aliases,
                'generation': state.source.generation + 1, 'status': status}))
            for binding in state.bindings:
                bound.sources.put_binding(binding.model_copy(update={
                    'ref': binding.ref.model_copy(update={'canonical_url': endpoint}), 'status': status}))
            event_id = operation.operation_id + ':relocated'
            bound.events.append_event(DurableEvent(event_id=event_id, tenant_id=context.tenant_id,
                operation_id=operation.operation_id, action='source_relocated', skills=refs))
            return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                state='complete', committed=False, source_id=state.source.source_id,
                outcomes=tuple(SkillOutcome(skill=ref, state='unchanged', code='repository_relocated')
                               for ref in refs)), event_ids=(event_id,))
        protected = with_admission(service, lambda bound: _capture(service, context, request, bound))
        return protected._commit(context, 'relocate_source', operation, state.guards, commit)
    except Exception as error:
        service._failed(context, 'relocate_source', operation, refs, error=error)
        raise
