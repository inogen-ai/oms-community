"""Re-import a skill from a local package: a folder, an archive or the command line.

Each skill has one local stream, reconciled through the same plan, review and apply
path as a GitHub source. A batch submits several packages and records one receipt
per skill, which the upload approval and the command line build on.
"""
from copy import copy
from dataclasses import dataclass, replace
import logging
from pathlib import Path
from uuid import uuid4

from oms.domain.ids import slug
from oms.domain.models import Skill
from oms.domain.repo import REPO_ID_PREFIX, ReservedSkillId, reserved_id_refusal
from oms.domain.types import SkillVersionCause
from oms.publish.manifest import is_manifest_echo
from oms.sources import operations, review
from oms.sources.apply import PreparedApply, apply_parts, commit_apply
from oms.sources.errors import SourceConflict, SourceNotFound, StaleMutation
from oms.sources.forks import affected_owners
from oms.sources.local_package import retain_local_packages
from oms.sources.local_projection import project_local
from oms.sources.local_upload import LocalSelection, batch_request, describe_packages
from oms.sources.merge import build_plan
from oms.sources.models import (
    ActionContext, BatchMember, DurableEvent, Generations, LocalBatchRequest, LocalImportRequest, LocalState,
    LocalStream, Operation, OperationResult, OriginRef, PlanFlag, SkillOutcome, SourceSnapshot, Update,
)
from oms.sources.mutation import source_repository
from oms.sources.primitives import exact_digest
from oms.sources.service import SourceService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalCapture:
    skill: Skill | None
    stream: LocalStream | None
    guard: Generations
    base: SourceSnapshot | None
    local: LocalState
    card: Update | None
    policy: str
    echo: bool


def submit_local(service: SourceService, context: ActionContext, request: LocalImportRequest, *,
                 idempotency_key: str) -> OperationResult:
    skill = request.skill
    service.policy.admit_local(context, request)
    operation, fresh = service._begin(context, "local_import", request, idempotency_key, (skill,))
    if not fresh:
        return operation.result

    def capture(bound):
        current = bound.graph.get_skill(skill.skill_id, tenant_id=skill.tenant_id)
        if request.create and current is not None:
            raise SourceConflict("explicit_local_target_required")
        if not request.create and current is None:
            raise SourceNotFound("skill_not_found", "local target not found")
        guard = bound.sources.get_generations(skill)
        if (guard.content, guard.binding) != (request.expected_content_generation, request.expected_binding_generation):
            raise StaleMutation("content_changed", "local target changed")
        stream = bound.sources.get_local_stream(skill)
        base = bound.sources.get_snapshot(stream.baseline) if stream and stream.baseline else None
        local = (project_local(bound.graph, skill, baseline=base, content_generation=guard.content,
                    ownership=bound.sources.ownership_for_skill(skill), policy_version=service.builder.policy_version)
                 if current else LocalState(skill=skill, content_generation=guard.content, digest="absent"))
        primary = next((row for row in request.package.manifest if row.path == "SKILL.md"), None)
        echo = bool(primary and current and any(is_manifest_echo(row, skill_id=skill.skill_id,
            manifest=request.package.manifest, complete=True, source_digest=primary.digest)
            for row in bound.graph.publications_for_skill(skill.tenant_id, skill.skill_id)))
        return LocalCapture(current, stream, guard, base, local, bound.sources.open_update(skill),
                            service._policy_revision(context, bound, (skill,)), echo)

    try:
        if request.create and skill.skill_id.startswith(REPO_ID_PREFIX):
            raise ReservedSkillId(reserved_id_refusal(skill.skill_id))
        if request.create and slug(request.local_name) != skill.skill_id:
            raise SourceConflict("invalid_request", "local identity mismatch")
        before = service._read(context, "local_import", (skill,), capture)
        origin = (before.stream.origin if before.stream else OriginRef(skill=skill, kind="local", generation=1,
                  origin_id="local-" + exact_digest([operation.operation_id, skill.storage_key])))
        holder = (before.stream or LocalStream(origin=origin)).model_copy(update={
            "transport": request.transport, "filename": request.filename, "uploader_id": context.actor_id})
        if before.card is not None:
            def refuse(bound):
                service.policy.admit_local(context, request)
                if capture(bound) != before:
                    raise StaleMutation("content_changed", "local target changed")
                if before.stream is None:
                    bound.sources.put_local_stream(holder)
                outcome = review.refuse_collision(bound, origin, before.card, operation_id=operation.operation_id,
                    actor_id=context.actor_id, action="local_import")
                return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                    state="blocked", committed=False, outcomes=(outcome,)))
            return service._commit(context, "local_import", operation, (before.guard,), refuse)

        incoming = service.builder.build(request.package, origin,
            snapshot_id="local-snapshot-" + exact_digest([operation.operation_id, skill.storage_key]),
            mappings=before.base.graph_mappings if before.base else (), base=before.base)
        checked = service.policy.screen(incoming, before.local)
        plan = build_plan(before.base, incoming, before.local, service._merge_policy(incoming, checked,
            update_generation=1, policy_digest=before.policy, binding_generation=before.guard.binding,
            first_reconciliation=before.base is None, history_evidence="proven_ancestor"))
        permitted = checked.state == "passed" and not any(flag in plan.flags for flag in
            (PlanFlag.SAFETY_HOLD, PlanFlag.REQUIRED_CHECK_UNAVAILABLE))
        unchanged = permitted and not plan.flags and all(change.action == "keep" for change in plan.changes)
        new_allowed = (request.approve and permitted and all(part.evidence.kind != "unknown"
                                                            for part in incoming.effective_projection))
        prepared = None
        if not request.create and request.approve and permitted and not plan.conflicts and before.base is not None:
            provisional = Update(update_id="pending", plan=plan, generation=1, status="open", review_item_id="pending")
            try:
                aligned, selected, manual = review.selected_writes(provisional, incoming, before.local,
                                                                   request.removal_consents)
            except SourceConflict:
                pass
            else:
                if service.policy.screen_resolved(incoming, before.local, selected).state == "passed":
                    prepared = aligned, selected, manual
        changed_ids = {part.part_id for part in prepared[1]} if prepared else set()
        owners = {skill, *affected_owners(before.local, incoming, changed_ids)}
        guards = service._read(context, "local_import", tuple(sorted(owners)), lambda bound:
            tuple(bound.sources.get_generations(owner) for owner in sorted(owners)))
        if next(guard for guard in guards if guard.skill == skill) != before.guard:
            raise StaleMutation("content_changed", "local target changed")

        def commit(bound):
            service.policy.admit_local(context, request)
            if capture(bound) != before:
                raise StaleMutation("content_changed", "local target or policy changed")
            if before.echo and permitted:
                return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                    state="complete", committed=False,
                    outcomes=(SkillOutcome(skill=skill, state="unchanged", code="published_echo"),)))
            if unchanged:
                if request.approve:
                    bound.sources.put_snapshot(incoming)
                    bound.sources.put_local_stream(holder.model_copy(update={"baseline": incoming.ref}))
                    review.clear_retry(bound, origin, "local_import")
                return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                    state="complete", committed=False, outcomes=(SkillOutcome(skill=skill, state="unchanged"),)))
            if request.create:
                if not new_allowed:
                    return operations.finish(bound.sources, operation,
                                             OperationResult(operation_id=operation.operation_id,
                        state="blocked", committed=False,
                        outcomes=(SkillOutcome(skill=skill, state="blocked", code="approval_or_checks_required"),)))
                bound.graph.upsert_skill(Skill(id=skill.skill_id, name=request.local_name, domain=request.domain,
                    description="", tenant_id=skill.tenant_id, import_source_ref="source:" + origin.origin_id))
                resolved = apply_parts(bound, incoming, incoming.effective_projection, now=service.clock(),
                                       operation_id=operation.operation_id)
                bound.sources.put_snapshot(resolved)
                bound.sources.put_local_stream(holder.model_copy(update={"baseline": resolved.ref}))
                history = bound.history.capture_required(skill.skill_id, skill.tenant_id,
                                                         cause=SkillVersionCause.SOURCE_INSTALL,
                    actor=context.actor_id, source_operation_id=operation.operation_id,
                    source_origin_id=origin.origin_id,
                    source_revision=incoming.revision)
                event_id = operation.operation_id + ":local-installed"
                bound.events.append_event(DurableEvent(event_id=event_id, tenant_id=skill.tenant_id,
                    operation_id=operation.operation_id, action="local_import", skills=(skill,)))
                return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                    state="complete", committed=True, outcomes=(SkillOutcome(skill=skill, state="applied"),)),
                    history_ids=(history.id,) if history else (), event_ids=(event_id,))
            bound.sources.put_local_stream(holder)
            outcome = review.stage_update(bound, plan, incoming, operation_id=operation.operation_id,
                actor_id=context.actor_id, action="local_import")
            if prepared is not None:
                update = bound.sources.get_update(outcome.update_id, tenant_id=skill.tenant_id)
                aligned, selected, manual = prepared
                return commit_apply(bound, context, operation, PreparedApply(update, holder, incoming, aligned,
                    before.local, guards, selected, manual), now=service.clock(),
                    policy_version=service.builder.policy_version,
                    policy=service.policy)
            return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                state="awaiting_review", committed=False, outcomes=(outcome,)))
        return service._commit(context, "local_import", operation, guards, commit)
    except Exception as exc:
        logger.warning("Local source import failed", extra={"operation_id": operation.operation_id,
                                                           "exception_type": type(exc).__name__})
        service._failed(context, "local_import", operation, (skill,), error=exc)
        raise


def submit_local_batch(service: SourceService, context: ActionContext, request: LocalBatchRequest, *,
                       idempotency_key: str, upload_replay=None) -> OperationResult:
    """`upload_replay` records what a retry restates once the staged package is gone."""
    skills = tuple(row.skill for row in request.requests)
    for row in request.requests:
        service.policy.admit_local(context, row)
    operation, fresh = service._begin(context, "local_import", request, idempotency_key, skills,
                                      upload_replay=upload_replay)

    from oms.sources.operations import refresh, admitted_result

    def status():
        return refresh(service, context, operation, action='local_import', skills=skills)

    def result_for(current):
        return admitted_result(service, context, current)
    if not fresh:
        return result_for(status())
    keys = tuple("local-item-" + exact_digest([idempotency_key, index, row.skill.storage_key])
                 for index, row in enumerate(request.requests))
    members = tuple(BatchMember(child_operation_id=operations.request_identity(context, "local_import", row,
        key)[0], skill=row.skill)
                    for row, key in zip(request.requests, keys))

    def initialize(bound):
        current = bound.sources.get_operation(operation.operation_id, tenant_id=context.tenant_id)
        if current != operation:
            raise SourceConflict("batch_changed", "batch operation changed")
        bound.sources.put_operation(operation.model_copy(update={"batch_members": members}))
    service._read(context, "local_import", skills, initialize)
    child_service = copy(service)

    def factory_for(admitted, action, affected):
        original = service.factory_for(admitted, action, affected)

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
    for row, key in zip(request.requests, keys):
        try:
            child_service.submit_local(context, row, idempotency_key=key)
        except Exception as exc:
            logger.warning("Local batch item failed", extra={"operation_id": operation.operation_id,
                "skill_id": row.skill.skill_id, "exception_type": type(exc).__name__})

            def fail(graph, reviews):
                sources = source_repository(graph)
                identifier, digest = operations.request_identity(context, "local_import", row, key)
                current = sources.get_operation(identifier, tenant_id=context.tenant_id)
                if current is None:
                    sources.reserve_operation(Operation(operation_id=identifier, context=context, idempotency_key=key,
                        request_digest=digest, result=OperationResult(operation_id=identifier, state="failed",
                                                                      committed=False,
                            outcomes=(SkillOutcome(skill=row.skill, state="failed", code="local_import_failed"),))))
            service.repository.atomic("source:local-batch-failure", context.tenant_id, fail)
        status()
    return result_for(status())


def import_local_path(service, importer, context, path: Path, *, targets: tuple[str, ...] = (),
                      limit: int | None = None):
    state = importer.capture_state(context.tenant_id)
    packages = retain_local_packages(path, service.builder.blobs)
    if limit is not None:
        if limit < 1:
            raise SourceConflict("invalid_request", "positive import limit required")
        packages = packages[:limit]
    selected = {}
    for target in targets:
        if "=" in target:
            package_path, skill_id = target.split("=", 1)
        elif len(packages) == 1:
            package_path, skill_id = packages[0].package_path, target
        else:
            raise SourceConflict("invalid_package_selection", "package target mapping required")
        if not skill_id or package_path in selected:
            raise SourceConflict("invalid_package_selection", "invalid local target mapping")
        selected[package_path] = skill_id
    if not set(selected).issubset({package.package_path for package in packages}):
        raise SourceConflict("invalid_package_selection", "local package not found")
    descriptions = describe_packages(service, packages, context.tenant_id)
    choices = tuple(LocalSelection(package_path=package.package_path, target_skill_id=selected[package.package_path])
        if package.package_path in selected else LocalSelection(package_path=package.package_path,
            local_name=description.name, domain=description.domain)
        for package, description in zip(packages, descriptions))
    request = batch_request(packages, choices, state, filename=path.name, transport="cli")
    return service.submit_local_batch(context, request, idempotency_key="cli-" + uuid4().hex)
