"""The update card's lifecycle: staging a plan, moving an origin's baseline, closing the review.

Each origin bound to a skill has its own baseline. A second origin's candidate for a
skill with an open card is refused rather than merged, and a reviewer's choices are
bound to the fingerprint of the part they were made about.
"""
from oms.domain.models import ReviewItem
from oms.domain.types import Verdict
from oms.ports.mutation import MutationContext
from oms.sources.errors import SourceConflict, SourceNotFound, StaleMutation
from oms.sources.models import (
    Binding, BlockedAttempt, DraftChoice, Evidence, LocalState, LocalStream, MergePlan,
    OriginRef, PartKind, ProjectionPart, SkillOutcome, SnapshotRef, SourceSnapshot, SourceStatus, Update,
)
from oms.sources.primitives import exact_digest
from oms.sources.safety import SourceSafety


def origin_state(bound: MutationContext, origin: OriginRef) -> Binding | LocalStream:
    state = (bound.sources.get_binding(origin.skill) if origin.kind == "github"
             else bound.sources.get_local_stream(origin.skill))
    if state is None or state.origin != origin or isinstance(state, Binding) and not state.active:
        raise StaleMutation("binding_changed", "origin changed")
    return state


def summarize_status(bindings: tuple[Binding, ...]) -> SourceStatus:
    bindings = tuple(bindings)
    statuses = tuple(binding.status for binding in bindings if binding.status is not None)
    checked = max((status.checked_at for status in statuses if status.checked_at is not None), default=None)
    failures = {status.state for status in statuses if status.state in {"failed", "missing", "redirect"}}
    if failures:
        state = next(iter(failures)) if len(failures) == 1 else "failed"
    elif any(status.state == "updates_available" for status in statuses):
        state = "updates_available"
    elif (bindings and len(statuses) == len(bindings)
          and all(status.state == "up_to_date" for status in statuses)
          and not any(binding.first_reconciliation for binding in bindings)):
        state = "up_to_date"
    else:
        state = "unchecked"
    return SourceStatus(state=state, checked_at=checked)


def refresh_source_status(bound: MutationContext, source_id: str, tenant_id: str) -> None:
    source = bound.sources.get_source(source_id, tenant_id=tenant_id)
    if source is None:
        return
    from oms.domain.identity import SkillRef
    bindings = tuple(binding for skill in bound.graph.skills_for_tenant(tenant_id)
                     if (binding := bound.sources.get_binding(SkillRef(tenant_id, skill.id))) is not None
                     and binding.active and binding.source_id == source_id)
    bound.sources.put_source(source.model_copy(update={"status": summarize_status(bindings)}))


def advance_baseline(bound: MutationContext, origin: OriginRef, snapshot_ref: SnapshotRef | None, *,
                     reconciled: bool = True) -> None:
    state = origin_state(bound, origin)
    if snapshot_ref is not None and not snapshot_ref.is_base_for(origin):
        raise SourceConflict("record_unavailable", "baseline origin mismatch")
    values = {"baseline": snapshot_ref}
    if isinstance(state, Binding):
        values["first_reconciliation"] = not reconciled
        if reconciled:
            values["status"] = (state.status if state.status is not None and state.status.state == "unchecked"
                                and state.status.checked_at is None else
                                SourceStatus(state="up_to_date", checked_at=state.status.checked_at
                                             if state.status else None))
        bound.sources.put_binding(state.model_copy(update=values))
        refresh_source_status(bound, state.source_id, origin.skill.tenant_id)
    else:
        bound.sources.put_local_stream(state.model_copy(update=values))


def close_review(bound: MutationContext, update: Update, resolution: str, actor_id: str) -> None:
    item = bound.reviews.get(update.review_item_id)
    if (item is None or item.tenant_id != update.plan.skill.tenant_id or item.kind != "skill_update"
            or item.subject_id != update.update_id or item.resolved):
        raise SourceConflict("request_identity_changed", "source review identity changed")
    bound.reviews.resolve(item.id, resolution, decided_by=actor_id)


def clear_retry(bound: MutationContext, origin: OriginRef, action: str) -> None:
    for attempt in bound.sources.blocked_attempts(origin.skill):
        if (attempt.origin.kind, attempt.origin.origin_id, attempt.action) == (origin.kind, origin.origin_id, action):
            bound.sources.clear_blocked_attempt(attempt.attempt_id, tenant_id=origin.skill.tenant_id)


def refuse_collision(bound: MutationContext, origin: OriginRef, existing: Update, *, operation_id: str,
                     actor_id: str, action: str) -> SkillOutcome:
    bound.sources.put_blocked_attempt(BlockedAttempt(attempt_id="blocked-" + exact_digest([operation_id,
        origin.skill.storage_key, action]),
        origin=origin, actor_id=actor_id, reason="another_update_open", existing_update_id=existing.update_id,
        action=action))
    return SkillOutcome(skill=origin.skill, state="blocked", update_id=existing.update_id, code="another_update_open")


def part_fingerprint(update: Update, part_id: str) -> str:
    change = next((change for change in update.plan.changes if change.part_id == part_id), None)
    if change is None:
        raise SourceNotFound("update_not_found", "review part not found")
    conflict = next((item for item in update.plan.conflicts if item.part_id == part_id), None)
    return exact_digest({"change": change.model_dump(mode="json"), "allowed": conflict.allowed_choices
                         if conflict else None,
        "policy": update.plan.fingerprint.policy_version, "policy_digest": update.plan.fingerprint.policy_digest})


def stage_update(bound: MutationContext, plan: MergePlan, incoming: SourceSnapshot, *, operation_id: str,
                 actor_id: str, action: str, replace_same_origin: bool = False) -> SkillOutcome:
    if incoming.ref != plan.incoming:
        raise SourceConflict("record_unavailable", "candidate snapshot mismatch")
    existing = bound.sources.open_update(plan.skill)
    same = (existing is not None
            and (existing.plan.origin.kind, existing.plan.origin.origin_id) == (plan.origin.kind,
                plan.origin.origin_id))
    if existing is not None and not (replace_same_origin and same and plan.origin.kind == "github"):
        return refuse_collision(bound, plan.origin, existing, operation_id=operation_id, actor_id=actor_id,
                                action=action)
    if existing is not None:
        prior = bound.sources.get_snapshot(existing.plan.incoming)
        if (prior is not None and existing.plan.origin == plan.origin and existing.plan.base == plan.base
                and existing.plan.changes == plan.changes and existing.plan.conflicts == plan.conflicts
                and existing.plan.flags == plan.flags
                and existing.plan.fingerprint.local_digest == plan.fingerprint.local_digest
                and existing.plan.fingerprint.policy_digest == plan.fingerprint.policy_digest
                and prior.model_dump(exclude={"ref"}) == incoming.model_dump(exclude={"ref"})):
            clear_retry(bound, plan.origin, action)
            return SkillOutcome(skill=plan.skill, state="awaiting_review", update_id=existing.update_id)
    update_id = "update-" + exact_digest([operation_id, plan.skill.storage_key])
    update = Update(update_id=update_id, plan=plan, generation=plan.fingerprint.update_generation,
        status="open", review_item_id="source-review-" + exact_digest([plan.skill.tenant_id, update_id]),
        operation_id=operation_id)
    if existing is not None:
        prior = bound.sources.get_snapshot(existing.plan.incoming)
        current_maps = {row.part_id: row for row in incoming.graph_mappings}
        prior_maps = {row.part_id: row for row in prior.graph_mappings}
        carried = []
        for choice in existing.drafts:
            if (existing.plan.base == plan.base
                and existing.plan.fingerprint.local_digest == plan.fingerprint.local_digest
                    and choice.part_id in {change.part_id for change in plan.changes}
                    and prior_maps.get(choice.part_id) == current_maps.get(choice.part_id)
                    and choice.part_fingerprint == part_fingerprint(update, choice.part_id)):
                carried.append(choice)
        update = update.model_copy(update={"drafts": tuple(carried)})
        close_review(bound, existing, "superseded", actor_id)
        bound.sources.put_update(existing.model_copy(update={"status": "superseded"}))
    bound.sources.put_snapshot(incoming)
    bound.sources.put_update(update)
    bound.reviews.enqueue(ReviewItem(id=update.review_item_id, kind="skill_update", subject_id=update_id,
        other_id=None, verdict=Verdict.AMBIGUOUS, reason="Source changes require review",
        tenant_id=plan.skill.tenant_id))
    clear_retry(bound, plan.origin, action)
    return SkillOutcome(skill=plan.skill, state="awaiting_review", update_id=update_id)


def validate_choices(update: Update, choices: tuple[DraftChoice, ...]) -> None:
    if len({choice.part_id for choice in choices}) != len(choices):
        raise SourceConflict("review_choice_not_allowed", "duplicate review choice")
    changes = {change.part_id: change for change in update.plan.changes}
    conflicts = {conflict.part_id: conflict for conflict in update.plan.conflicts}
    for choice in choices:
        change = changes.get(choice.part_id)
        if change is None or choice.part_fingerprint != part_fingerprint(update, choice.part_id):
            raise StaleMutation("update_changed", "review part changed")
        allowed = conflicts[choice.part_id].allowed_choices if choice.part_id in conflicts else (
            ("keep_oms", "use_upstream", "merged_text")
            if change.kind in (PartKind.RULE, PartKind.SECTION, PartKind.EXAMPLE)
            else ("keep_oms", "use_upstream"))
        if choice.choice not in allowed or choice.choice == "use_upstream" and change.incoming.kind == "unknown":
            raise SourceConflict("review_choice_not_allowed")
        if choice.choice == "merged_text" and SourceSafety().check_text(choice.merged_text).state != "passed":
            raise SourceConflict("required_check_held", "merged text not safe")


def aligned_snapshot(incoming: SourceSnapshot, update: Update, local: LocalState) -> SourceSnapshot:
    """Carry proven graph identities across source part renames without using local text."""
    parts = {part.part_id: part for part in incoming.effective_projection}
    mappings = {row.part_id: row for row in incoming.graph_mappings}
    live_maps = {row.part_id: row for row in local.graph_mappings}
    aliases = {}
    for change in update.plan.changes:
        if change.part_id in parts or change.incoming.kind != "known":
            continue
        known = live_maps.get(change.part_id)
        matches = [key for key, mapping in mappings.items() if known and mapping.entity_ids == known.entity_ids
                   and parts[key].kind == change.kind and mapping.section_id == known.section_id]
        if len(matches) != 1:
            raise SourceConflict("source_unit_identity_unknown", "source part identity unknown")
        aliases[matches[0]] = change.part_id
    return incoming.model_copy(update={
        "effective_projection": tuple(part.model_copy(update={"part_id": aliases.get(part.part_id,
            part.part_id)}) for part in incoming.effective_projection),
        "graph_mappings": tuple(row.model_copy(update={"part_id": aliases.get(row.part_id, row.part_id)})
                                for row in incoming.graph_mappings)})


def selected_writes(update: Update, incoming: SourceSnapshot, local: LocalState,
                    removal_consents: tuple[str, ...], *, preview: bool = False) -> tuple[SourceSnapshot,
                        tuple[ProjectionPart, ...], frozenset[str]]:
    validate_choices(update, update.drafts)
    choices = {choice.part_id: choice for choice in update.drafts}
    if any(conflict.part_id not in choices for conflict in update.plan.conflicts):
        raise SourceConflict("review_choice_not_allowed", "candidate requires resolution")
    aligned = aligned_snapshot(incoming, update, local)
    parts = {part.part_id: part for part in aligned.effective_projection}
    selected, manual = [], set()
    for change in update.plan.changes:
        choice = choices.get(change.part_id)
        if choice is not None and choice.choice == "keep_oms" or choice is None and change.action == "keep":
            continue
        if choice is not None and choice.choice == "merged_text":
            template = change.incoming if change.incoming.kind == "known" else change.local
            if template.kind != "known" or not isinstance(template.value, dict) or "body" not in template.value:
                raise SourceConflict("review_choice_not_allowed", "merged unit structure unknown")
            value = dict(template.value) | {"body": choice.merged_text}
            part = ProjectionPart(part_id=change.part_id, kind=change.kind,
                evidence=Evidence(kind="known", value=value, policy_version=incoming.policy_version))
            manual.add(change.part_id)
        elif change.incoming.kind == "absent":
            if not preview and change.part_id not in removal_consents:
                raise SourceConflict("consent_required", "removal consent required")
            part = ProjectionPart(part_id=change.part_id, kind=change.kind, evidence=change.incoming)
        else:
            part = parts[change.part_id]
        # An explicit keep (keep_oms, or merged_text on the local unit) is the
        # reviewer's own decision, so it neither blocks the replacement nor
        # needs consent. The consent guard stays for every other linked
        # removal so an add plus a delete cannot split to bypass it (spec 7.5).
        unkept = {linked for linked in change.linked_removals
                  if linked == change.part_id or linked not in choices or choices[linked].choice == "use_upstream"}
        if not preview and not unkept.issubset(removal_consents):
            raise SourceConflict("consent_required", "linked removal consent required")
        selected.append(part)
    return aligned, tuple(selected), frozenset(manual)
