"""Check a binding's source for updates.

A check captures the live state first, fetches the upstream package under one finite
budget, builds the plan and then either stages it for review or, where the paid edition
has automatic application switched on, applies it in the same transaction. A check
that fails records the failure on the binding so the source card can show it; the
reconciled content is never touched by a failure.
"""
from contextlib import contextmanager
from copy import copy
from dataclasses import dataclass, replace
import logging
import time

from oms.domain.identity import SkillRef
from oms.sources import operations, review
from oms.sources.apply import PreparedApply, commit_apply
from oms.sources.errors import SourceConflict, SourceError, SourceForbidden, SourceNotFound, StaleMutation
from oms.sources.forks import affected_owners
from oms.sources.git_reader import AcquisitionError
from oms.sources.limits import AcquisitionLimits, BudgetExceeded
from oms.sources.links import LinkError, parse_link
from oms.sources.local_projection import project_local
from oms.sources.merge import build_plan
from oms.sources.models import (
    BatchMember, Binding, CheckRequest, Generations, LocalState, Operation, OperationResult, PackageRequest,
    ProjectionPart, RefRequest, SkillOutcome, Source, SourceSnapshot, SourceStatus, Update,
)
from oms.sources.mutation import source_repository
from oms.sources.operations import admitted_result, refresh
from oms.sources.primitives import exact_digest
from oms.sources.retirement import retired_binding
from oms.sources.review import summarize_status

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedAutomatic:
    aligned: SourceSnapshot
    selected: tuple[ProjectionPart, ...]
    manual_parts: frozenset[str]
    guards: tuple[Generations, ...]


class _AttributedEvents:
    """Stamps automatic-apply events with the actor its history names."""
    def __init__(self, events, actor_id):
        self.events, self.actor_id = events, actor_id

    def append_event(self, event):
        return self.events.append_event(event.model_copy(update={'actor_id': self.actor_id}))


def _automatic(service, context, before, candidate):
    # Automatic application is decided twice: here on the captured state, to
    # find out whether it is worth preparing, and again inside the commit
    # against the current binding. A card with drafts means a reviewer has
    # started on it, and their work is not overwritten by an unattended apply.
    if candidate is None or not service.automatic or before.update is not None and before.update.drafts:
        return None
    incoming, plan = candidate
    if service.policy.automatic_eligibility(plan, before.binding).state != 'passed':
        return None
    provisional = Update(update_id='pending', plan=plan, generation=plan.fingerprint.update_generation,
                         status='open', review_item_id='pending')
    try:
        aligned, selected, manual = review.selected_writes(provisional, incoming, before.local, ())
        owners = affected_owners(before.local, incoming, {part.part_id for part in selected})
        service.policy.admit('automatic_apply', context, owners)
    except (SourceConflict, SourceForbidden):
        return None
    if service.policy.screen_resolved(incoming, before.local, selected).state != 'passed':
        return None
    # Every skill the apply would reach needs a guard captured before the
    # fetch; an owner that turned up only now was not frozen, so the apply
    # waits for a reviewer rather than writing to a skill nobody guarded.
    frozen = {guard.skill: guard for guard in before.owner_guards}
    if any(owner not in frozen for owner in owners):
        return None
    return PreparedAutomatic(aligned, selected, manual, tuple(frozen[owner] for owner in owners))


def single(service, context, request, key, budget, *, before=None, throttle=True, scheduled=False, open_session=False):
    skill = request.skills[0]
    if before is None:
        before = service._read(context, 'check', (skill,), lambda bound:
            capture(service, context, request, skill, bound, scheduled=scheduled))
    operation, fresh = service._begin(context, 'check', request, key, (skill,),
                                     throttle_source=request.source_id if throttle else None)
    if not fresh:
        return operation.result
    try:
        if open_session:
            with reader_session(service, before.source, budget) as prepared:
                candidate = acquire(prepared, context, before, operation, budget)
        else:
            candidate = acquire(service, context, before, operation, budget)
        automatic = _automatic(service, context, before, candidate)
        guards = automatic.guards if automatic is not None else (before.guard,)

        def commit(bound):
            budget.check()
            # The fetch ran outside the transaction, so everything captured
            # before it is compared whole with the state under the lock. The
            # source's status is left out: a sibling's failed check in the same
            # batch writes it, and that is not a reason to refuse this one.
            current = capture(service, context, request, skill, bound, scheduled=scheduled)
            if (current.source.model_copy(update={'status': None}) != before.source.model_copy(update={'status': None})
                    or replace(current, source=before.source) != before):
                raise StaleMutation('source_changed', 'source baseline or local changed')
            if candidate is None:
                outcome = review.refuse_collision(bound, before.binding.origin, before.update,
                    operation_id=operation.operation_id, actor_id=context.actor_id, action='check')
            else:
                incoming, plan = candidate
                # A revision an undo reverted is not offered again for this
                # binding (spec §8.5). A comparison with nothing to change and
                # no flags needs nobody's decision either: the snapshot is kept
                # as the record of what was checked and the binding reads up
                # to date.
                if bound.sources.candidate_suppressed(plan.origin, incoming.revision):
                    outcome = SkillOutcome(skill=skill, state='unchanged')
                elif (not any(change.action != 'keep' for change in plan.changes) and not plan.flags
                      and before.update is None):
                    bound.sources.put_snapshot(incoming)
                    outcome = SkillOutcome(skill=skill, state='unchanged')
                    review.clear_retry(bound, plan.origin, 'check')
                else:
                    outcome = review.stage_update(bound, plan, incoming, operation_id=operation.operation_id,
                        actor_id=context.actor_id, action='check', replace_same_origin=True)
                    # Authority and eligibility are re-read at the write boundary
                    # on purpose: switching automation off between the fetch and
                    # this point must prevent the unattended apply (spec §8.4).
                    if automatic is not None and outcome.state == 'awaiting_review':
                        service.policy.admit('automatic_apply', context, tuple(guard.skill for guard in guards))
                        if service.policy.automatic_eligibility(plan, current.binding).state != 'passed':
                            raise StaleMutation('policy_changed', 'source automation changed')
                        update = bound.sources.get_update(outcome.update_id, tenant_id=context.tenant_id)
                        retained = bound.sources.get_snapshot(update.plan.incoming)
                        if retained is None:
                            raise SourceConflict('record_unavailable', 'candidate snapshot missing')
                        aligned = automatic.aligned.model_copy(update={'ref': retained.ref})
                        # Checking is not approval: the version, review decision and apply
                        # event name the binding's automation actor, not whoever ran the check.
                        attributed = context.model_copy(update={'actor_id': current.binding.automation_actor_id
                                                                or context.actor_id})
                        result = commit_apply(replace(bound,
                                                      events=_AttributedEvents(bound.events, attributed.actor_id)),
                                              attributed, operation,
                                              PreparedApply(update, current.binding, retained, aligned, before.local,
                                                            guards, automatic.selected, automatic.manual_parts),
                                              now=service.clock(), policy_version=service.builder.policy_version,
                                              policy=service.policy)
                        binding = bound.sources.get_binding(skill)
                        bound.sources.put_binding(binding.model_copy(update={'status': SourceStatus(state='up_to_date',
                            checked_at=service.clock())}))
                        review.refresh_source_status(bound, request.source_id, context.tenant_id)
                        return result
            state = ('up_to_date' if outcome.state == 'unchanged' else 'updates_available'
                     if outcome.state == 'awaiting_review' else 'failed')
            bound.sources.put_binding(current.binding.model_copy(update={'status': SourceStatus(state=state,
                checked_at=service.clock(), code=outcome.code)}))
            review.refresh_source_status(bound, request.source_id, context.tenant_id)
            return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                state='awaiting_review' if outcome.state == 'awaiting_review' else 'blocked'
                if outcome.state == 'blocked' else 'complete',
                committed=False, outcomes=(outcome,)))
        return service._commit(context, 'check', operation, guards, commit)
    except Exception as error:
        # The failure is written to the binding in a transaction of its own,
        # after the check's has rolled back: the operator sees why the check
        # failed on the source card, and the reconciled content stays as it was.
        logger.warning('Source check failed',
                       extra={'operation_id': operation.operation_id, 'exception_type': type(error).__name__})
        row = (before.binding, before.base, before.guard, before.local, before.update, before.policy_revision)
        record_check_failure(service, context, before.source, (row,), error)
        service._failed(context, 'check', operation, (skill,), error=error)
        raise


def check(service, context, request, *, idempotency_key, scheduled=False):
    skills = request.skills
    if not skills or len(skills) > 10_000 or len(set(skills)) != len(skills):
        raise SourceConflict('invalid_request', 'invalid check scope')
    if scheduled and not context.service:
        raise SourceForbidden('source_action_forbidden', 'scheduled service context required')
    replay = service._replay(context, 'check', request, idempotency_key, skills)
    if replay is not None:
        return replay.result
    # One budget for the whole scope, however many skills it names: the
    # deadline, the bytes fetched and the local projections all count against
    # it, so a wide check cannot run for as long as its skills add up to.
    budget = CheckBudget(getattr(service.reader, 'limits', None),
                         cancelled=getattr(service, 'cancelled', lambda: False))

    def capture_all(bound):
        rows = []
        for skill in skills:
            budget.check()
            row = capture(service, context, request, skill, bound, scheduled=scheduled)
            budget.capture(row.local)
            rows.append(row)
        return tuple(rows)
    captured = service._read(context, 'check', skills, capture_all)
    if len(skills) == 1:
        return single(service, context, request, idempotency_key, budget, before=captured[0],
                      throttle=not scheduled, scheduled=scheduled, open_session=True)
    return multiple(service, context, request, idempotency_key, captured, budget, scheduled=scheduled)


def multiple(service, context, request, key, captured, budget, *, scheduled):
    skills = request.skills
    operation, fresh = service._begin(context, 'check', request, key, skills,
                                    throttle_source=request.source_id if not scheduled else None)
    if not fresh:
        return admitted_result(service, context, refresh(service, context, operation, action='check', skills=skills))
    requests = tuple(request.model_copy(update={'skills': (skill,)}) for skill in skills)
    keys = tuple('check-item-' + exact_digest([key, index, skill.storage_key]) for index, skill in enumerate(skills))
    members = tuple(BatchMember(child_operation_id=operations.request_identity(context, 'check', row,
        child_key)[0], skill=skill)
                    for row, child_key, skill in zip(requests, keys, skills))

    def initialize(bound):
        if bound.sources.get_operation(operation.operation_id, tenant_id=context.tenant_id) != operation:
            raise SourceConflict('batch_changed', 'batch operation changed')
        bound.sources.put_operation(operation.model_copy(update={'batch_members': members}))
    service._read(context, 'check', skills, initialize)
    child_service = copy(service)

    def factory_for(ctx, action, affected):
        factory = service.factory_for(ctx, action, affected)

        def bind(graph, reviews):
            bound = factory(graph, reviews)

            # Each child commits on its own, so a failed skill never rolls its
            # peers back. What a child may not do is commit under a parent
            # that has finished or lost its lease: the parent is re-read at
            # every child's admission for exactly that.
            def admit():
                bound.admit()
                parent = bound.sources.get_operation(operation.operation_id, tenant_id=context.tenant_id)
                if (parent is None or parent.result.state in operations.TERMINAL or parent.lease != operation.lease
                        or parent.lease.expires_at <= service.clock()):
                    raise SourceConflict('operation_no_longer_active', 'batch no longer active')
            return replace(bound, admit=admit)
        return bind
    child_service.factory_for = factory_for

    # A child that never got to reserve its own operation still needs a
    # failed one on record, or the batch result would be silent about it and
    # a retry of the same key would not find it.
    def fail(row, child_key, error):
        def record(graph, reviews):
            sources = source_repository(graph)
            identifier, digest = operations.request_identity(context, 'check', row, child_key)
            if sources.get_operation(identifier, tenant_id=context.tenant_id) is None:
                sources.reserve_operation(Operation(operation_id=identifier, context=context, idempotency_key=child_key,
                    request_digest=digest, scope_proofs=tuple(proof for proof in operation.scope_proofs
                                                              if proof.skill in row.skills),
                    result=OperationResult(operation_id=identifier, state='failed', committed=False,
                        outcomes=(SkillOutcome(skill=row.skills[0], state='failed',
                                               code=getattr(error, 'code', 'source_check_failed')),))))
        service.repository.metadata('source:check-batch-failure', context.tenant_id, record)
    try:
        with reader_session(child_service, captured[0].source, budget) as prepared:
            for index, (row, child_key, before) in enumerate(zip(requests, keys, captured)):
                try:
                    budget.check()
                    single(prepared, context, row, child_key, budget, before=before, throttle=False,
                           scheduled=scheduled)
                except BudgetExceeded as error:
                    for remainder, remainder_key in zip(requests[index:], keys[index:]):
                        fail(remainder, remainder_key, error)
                    break
                except Exception as error:
                    fail(row, child_key, error)
    except Exception as error:
        for row, child_key in zip(requests, keys):
            fail(row, child_key, error)
    return admitted_result(service, context, refresh(service, context, operation, action='check', skills=skills))


class CheckBudget:
    def __init__(self, limits=None, *, cancelled=lambda: False):
        self.limits = limits or AcquisitionLimits()
        self.cancelled = cancelled
        self.deadline = time.monotonic() + self.limits.operation_seconds
        self.bytes = 0
        self.files = 0
        self.local_bytes = 0
        self.retained = set()

    def check(self):
        if self.cancelled():
            raise BudgetExceeded('acquisition cancelled')
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded('operation deadline exceeded')

    def capture(self, local):
        self.check()
        self.local_bytes += len(local.model_dump_json().encode('utf-8'))
        if self.local_bytes > self.limits.expanded_bytes:
            raise BudgetExceeded('source check projection budget exceeded')

    def retain(self, package):
        self.check()
        # Several bindings often share one package at one commit; its bytes
        # count once, so a source with many skills in one package is not
        # charged for the same files per skill.
        key = (package.resolved_ref.commit, package.package_path,
               tuple((row.path, row.digest, row.size, row.mode) for row in package.manifest))
        if key not in self.retained:
            self.retained.add(key)
            self.bytes += sum(entry.size for entry in package.manifest)
            self.files += len(package.manifest)
        if self.bytes > self.limits.package_bytes or self.files > self.limits.files:
            raise BudgetExceeded('source check content budget exceeded')


@contextmanager
def reader_session(service, source, budget):
    # One reader session for every skill of a check, so the repository is
    # fetched once rather than once per binding; the session's own deadline
    # is pulled in to the check's so neither can outlive the other.
    reader = service.reader
    if not callable(getattr(reader, 'batch_session', None)):
        yield service
        return
    reader = copy(reader)
    if hasattr(reader, 'cancelled'):
        prior = reader.cancelled
        reader.cancelled = lambda: budget.cancelled() or prior()
    with reader.batch_session(tenant_id=source.tenant_id, credential_profile_id=source.credential_profile_id,
                              canonical_url=source.canonical_url) as session:
        if hasattr(session, 'budget'):
            session.budget.deadline = min(session.budget.deadline, budget.deadline)
        bound = copy(service)
        bound.reader = session
        yield bound


@dataclass(frozen=True)
class CapturedCheck:
    source: Source
    binding: Binding
    base: SourceSnapshot | None
    guard: Generations
    local: LocalState
    update: Update | None
    policy_revision: str
    owner_guards: tuple[Generations, ...]


def capture(service, context, request, skill, bound, *, scheduled=False):
    source = bound.sources.get_source(request.source_id, tenant_id=context.tenant_id)
    if source is None:
        raise SourceNotFound('source_not_found')
    if source.generation != request.expected_source_generation:
        raise StaleMutation('source_changed', 'source generation changed')
    if scheduled and not source.scheduled:
        raise SourceConflict('source_changed', 'source schedule disabled')
    binding = bound.sources.get_binding(skill)
    if (binding is None or not binding.active or binding.source_id != source.source_id
        or retired_binding(bound.sources, binding)):
        raise SourceNotFound('binding_not_found')
    base = bound.sources.get_snapshot(binding.baseline) if binding.baseline else None
    guard = bound.sources.get_generations(skill)
    local = project_local(bound.graph, skill, baseline=base, content_generation=guard.content,
        ownership=bound.sources.ownership_for_skill(skill), policy_version=service.builder.policy_version)
    # Guards for every skill sharing content with this one are captured now,
    # before the fetch: an apply that forks a shared rule reaches them too.
    owners = {skill, *(owner for row in local.graph_mappings for owner in row.owner_skills)}
    return CapturedCheck(source, binding, base, guard, local,
        bound.sources.open_update(skill), service._policy_revision(context, bound, (skill,)),
        tuple(bound.sources.get_generations(owner) for owner in sorted(owners)))


def acquire(service, context, before, operation, budget):
    source, binding, base = before.source, before.binding, before.base
    card = before.update
    if card is not None and (card.plan.origin.kind, card.plan.origin.origin_id) != (binding.origin.kind,
            binding.origin.origin_id):
        return None
    budget.check()
    # The binding names the URL it was installed from; it must still be one
    # of the source's confirmed addresses, or a source that has moved on would
    # be fetched from wherever the old name now points.
    canonical = parse_link(binding.ref.canonical_url).repository_url.casefold()
    if canonical not in {parse_link(url).repository_url.casefold()
                         for url in (source.canonical_url, *source.confirmed_aliases)}:
        raise SourceForbidden('repository_endpoint_changed', 'binding repository changed')
    resolved = binding.ref
    if resolved.kind == 'branch':
        resolved = service.reader.resolve(RefRequest(tenant_id=context.tenant_id, url=source.canonical_url,
            credential_profile_id=source.credential_profile_id, ref_kind='branch', ref_name=resolved.name,
            package_path=binding.package_path))
    if resolved.kind != binding.ref.kind or resolved.name != binding.ref.name:
        raise SourceForbidden('source_changed', 'resolved ref changed')
    if parse_link(resolved.canonical_url).repository_url.casefold() != canonical:
        raise SourceForbidden('resolved_repository_changed')
    # The baseline commit lets the reader fetch only what changed since the
    # last retained revision and tells it whether that revision is still an
    # ancestor, which is where the plan's history evidence comes from.
    acquired = service.reader.read(PackageRequest(tenant_id=context.tenant_id, resolved_ref=resolved,
        package_path=binding.package_path, credential_profile_id=source.credential_profile_id,
        baseline_commit=base.revision if base else None))
    if acquired.resolved_ref != resolved or acquired.package_path != binding.package_path:
        raise SourceConflict('acquired_package_changed')
    budget.retain(acquired)
    incoming = service.builder.build(acquired, binding.origin,
        snapshot_id='snapshot-' + exact_digest([operation.operation_id, binding.origin.skill.storage_key]),
        mappings=base.graph_mappings if base else (), base=base)
    checked = service.policy.screen(incoming, before.local)
    plan = build_plan(base, incoming, before.local, service._merge_policy(incoming, checked,
        update_generation=card.generation + 1 if card else 1, policy_digest=before.policy_revision,
        first_reconciliation=binding.first_reconciliation, history_evidence=acquired.history_evidence))
    budget.check()
    return incoming, plan


def record_check_failure(service, context, source, rows, error):
    if isinstance(error, AcquisitionError):
        state = "missing" if str(error) == "skill package not found" else "failed"
        code = "package_missing" if state == "missing" else "acquisition_failed"
    elif isinstance(error, BudgetExceeded):
        state, code = 'failed', error.code
    elif isinstance(error, LinkError) and str(error) == "ref not found":
        state, code = "missing", "ref_missing"
    elif isinstance(error, SourceForbidden) and error.code == "resolved_repository_changed":
        state, code = "redirect", "repository_endpoint_changed"
    elif isinstance(error, (SourceForbidden, SourceNotFound, StaleMutation)):
        # A caller's authority, a vanished binding or a concurrent write says
        # nothing durable about this binding's source.
        return
    else:
        state, code = "failed", error.code if isinstance(error, SourceError) else "source_operation_failed"

    def record(graph, queue):
        sources = source_repository(graph)
        current = sources.get_source(source.source_id, tenant_id=context.tenant_id)
        if current is None or current.model_copy(update={"status": None}) != source.model_copy(update={"status": None}):
            return
        status = SourceStatus(state=state, code=code, checked_at=service.clock())
        for row in rows:
            binding = row[0]
            if sources.get_binding(binding.origin.skill) == binding:
                sources.put_binding(binding.model_copy(update={"status": status}))
        bindings = tuple(binding for skill in graph.skills_for_tenant(context.tenant_id)
            if (binding := sources.get_binding(SkillRef(context.tenant_id, skill.id))) is not None
            and binding.active and binding.source_id == source.source_id)
        sources.put_source(current.model_copy(update={"status": summarize_status(bindings)}))
    service.repository.metadata("source:check-status", context.tenant_id, record)
