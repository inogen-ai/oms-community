"""The reviewer's actions on an update card: save a draft, skip, adopt, recheck. None of them writes live
content; apply is the only action that does."""
from dataclasses import dataclass

from oms.domain.identity import SkillRef
from oms.domain.types import SkillStatus
from oms.sources import operations, review
from oms.sources.checks import refresh_repository
from oms.sources.errors import SourceConflict, SourceNotFound, StaleMutation
from oms.sources.local_projection import project_local
from oms.sources.merge import build_plan
from oms.sources.models import (
    Binding, DurableEvent, Generations, LocalState, LocalStream, OperationResult,
    SaveDraftRequest, SkillOutcome, SourceSnapshot, Update, UpdateRequest, PlanFlag,
)


@dataclass(frozen=True)
class ReviewState:
    update: Update
    holder: Binding | LocalStream
    base: SourceSnapshot | None
    incoming: SourceSnapshot
    local: LocalState
    guard: Generations
    policy_revision: str


def capture(service, context, request: UpdateRequest, bound, *, fresh: bool = True) -> ReviewState:
    update = bound.sources.get_update(request.update_id, tenant_id=context.tenant_id)
    if update is None or update.plan.skill != request.skill:
        raise SourceNotFound("update_not_found")
    if update.status != "open" or update.plan.fingerprint != request.fingerprint:
        raise StaleMutation("update_changed")
    holder = review.origin_state(bound, update.plan.origin)
    if holder.baseline != update.plan.base:
        raise StaleMutation("update_changed", "baseline changed")
    base = bound.sources.get_snapshot(holder.baseline) if holder.baseline else None
    incoming = bound.sources.get_snapshot(update.plan.incoming)
    guard = bound.sources.get_generations(request.skill)
    policy = service._policy_revision(context, bound, (request.skill,))
    local = project_local(bound.graph, request.skill, baseline=base, content_generation=guard.content,
        ownership=bound.sources.ownership_for_skill(request.skill), policy_version=service.builder.policy_version)
    if fresh and (guard.content != request.fingerprint.content_generation
                  or guard.binding != request.fingerprint.binding_generation
                  or local.digest != request.fingerprint.local_digest or policy != request.fingerprint.policy_digest
                  or service.builder.policy_version != request.fingerprint.policy_version):
        raise StaleMutation("content_changed", "local or policy changed")
    return ReviewState(update, holder, base, incoming, local, guard, policy)


def refuse_held(bound, skill: SkillRef, action: str) -> None:
    """Skip and Adopt would mark a held install's empty skill as reconciled (spec D6).

    The only ways out of a held install are applying its card or deleting the skill.
    Checked when the state is captured; the skill's status is part of its content
    digest, so a status change before the commit fails the commit's generation guard.
    """
    if action not in {"skip", "adopt"}:
        return
    record = bound.graph.get_skill(skill.skill_id, tenant_id=skill.tenant_id)
    if record is not None and record.status is SkillStatus.HELD:
        raise SourceConflict("held_install_requires_apply", "apply this skill or delete it")


def decide(service, context, request: UpdateRequest, key: str, action: str) -> OperationResult:
    operation, fresh = service._begin(context, action, request, key, (request.skill,))
    if not fresh:
        return operation.result
    try:
        def read(bound):
            state = capture(service, context, request, bound, fresh=action != "recheck")
            refuse_held(bound, request.skill, action)
            # A recheck refreshes the facts of the source the card's own binding names, never another.
            source = (bound.sources.get_source(state.holder.source_id, tenant_id=context.tenant_id)
                      if action == "recheck" and isinstance(state.holder, Binding) else None)
            return state, source
        state, source = service._read(context, action, (request.skill,), read)
        if source is not None:
            refresh_repository(service, context, source)
        next_plan = state.update.plan
        next_drafts = state.update.drafts
        if action == "save_draft":
            review.validate_choices(state.update, request.choices)
            next_drafts = request.choices
        elif action == "adopt":
            pass
        elif action == "recheck":
            check = service.policy.screen(state.incoming, state.local)
            next_plan = build_plan(state.base, state.incoming, state.local, service._merge_policy(state.incoming, check,
                update_generation=state.update.generation + 1, binding_generation=state.guard.binding,
                policy_digest=state.policy_revision, first_reconciliation=isinstance(state.holder, Binding)
                and state.holder.first_reconciliation,
                history_evidence="rewritten" if PlanFlag.REWRITTEN_HISTORY in state.update.plan.flags
                else "unproven" if state.base is None or PlanFlag.UNPROVEN_HISTORY in state.update.plan.flags
                else "proven_ancestor"))
            temporary = state.update.model_copy(update={"plan": next_plan})
            next_drafts = tuple(choice for choice in state.update.drafts
                if choice.part_id in {change.part_id for change in next_plan.changes}
                and state.update.plan.fingerprint.local_digest == state.local.digest
                and choice.part_fingerprint == review.part_fingerprint(temporary, choice.part_id))
        elif action != "skip":
            raise ValueError("Unknown source review action")

        def commit(bound):
            if capture(service, context, request, bound, fresh=action != "recheck") != state:
                raise StaleMutation("update_changed", "review state changed")
            if action in {"save_draft", "recheck"}:
                generation = state.update.generation + 1
                plan = next_plan.model_copy(update={
                    "fingerprint": next_plan.fingerprint.model_copy(update={"update_generation": generation})})
                bound.sources.put_update(state.update.model_copy(update={"plan": plan,
                    "generation": generation, "drafts": next_drafts}))
            else:
                if action == "adopt":
                    review.advance_baseline(bound, state.holder.origin, state.incoming.ref)
                else:
                    bound.sources.suppress_candidate(state.holder.origin, state.incoming.revision)
                review.close_review(bound, state.update, "adopted" if action == "adopt" else "skipped",
                                    context.actor_id)
                bound.sources.put_update(state.update.model_copy(update={"status": "adopted"
                    if action == "adopt" else "skipped", "operation_id": operation.operation_id}))
            event_id = operation.operation_id + ":" + action
            bound.events.append_event(DurableEvent(event_id=event_id, tenant_id=context.tenant_id,
                operation_id=operation.operation_id, action="source_" + action, skills=(request.skill,)))
            pending = action in {"save_draft", "recheck"}
            return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                state="awaiting_review" if pending else "complete", committed=False,
                outcomes=(SkillOutcome(skill=request.skill, state="awaiting_review" if pending
                                       else "unchanged", update_id=state.update.update_id),)), event_ids=(event_id,))
        return service._commit(context, action, operation, (state.guard,), commit)
    except Exception as error:
        service._failed(context, action, operation, (request.skill,), error=error)
        raise
