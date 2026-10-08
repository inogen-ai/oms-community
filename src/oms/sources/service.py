"""The entry point for every source action.

Each public method reserves an operation under the workflow lock, does its fetching
and reading outside any write, and commits through one guarded transaction. The
bodies live in the action modules (checks, apply, linking, retirement and so on);
this module imports them inside the methods, so the package has a single front door
without the action modules and the service importing each other at load time.
"""
from dataclasses import replace
from collections.abc import Callable
from datetime import datetime, timezone
import logging

from oms.domain.identity import SkillRef
from oms.domain.models import ReviewItem, Skill
from oms.domain.types import SkillVersionCause, Verdict
from oms.ports.mutation import MutationRequest, MutationContextFactory, MutationContext
from oms.ports.workflow import WorkflowRepository
from oms.ports.source_reader import SourceReader
from oms.ports.source_store import SourcePolicy
from oms.sources.discovery import Discoveries, DiscoveryAccessError
from oms.sources.projection import ProjectionBuilder
from oms.sources import installation, operations
from oms.sources.apply import apply_parts
from oms.sources.errors import SourceConflict, SourceForbidden, SourceNotFound, StaleMutation, SourceError
from oms.sources.links import parse_link
from oms.sources.local_projection import project_local
from oms.sources.merge import MergePolicy, build_plan
from oms.sources.models import (
    ActionContext, ApplyRequest, BulkApplyRequest, CheckRequest, CreateSourceRequest, DiscoveryRequest,
    DiscoveryResult, InstallRequest, LocalImportRequest, LocalBatchRequest, LinkRequest, RetargetRequest,
    UnlinkRequest, RelocateSourceRequest, RemoveSourceRequest, AutomationRequest, ScheduleRequest, UndoRequest,
    UndoStatus, SaveDraftRequest, UpdateRequest, Binding, DurableEvent, Evidence, Generations, LocalState,
    OperationResult, PackageRequest,
    PartKind, PlanFlag, ProjectionPart, RefRequest, SkillOutcome, SourceStatus, Update, UndoRecord, UndoWrite,
)
from oms.sources.mutation import source_repository
from oms.sources.primitives import exact_digest
from oms.sources.safety import FileCheck
from oms.sources import review

logger = logging.getLogger(__name__)


class SourceService:
    def __init__(self, repository: WorkflowRepository,
                 factory_for: Callable[[ActionContext, str, tuple[SkillRef, ...]], MutationContextFactory],
                 reader: SourceReader, projection_builder: ProjectionBuilder, policy: SourcePolicy,
                 discoveries: Discoveries, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), automatic: bool = False,
                 completion_hook: Callable[[MutationContext, OperationResult], None] | None = None):
        self.repository, self.factory_for = repository, factory_for
        self.reader, self.builder, self.policy = reader, projection_builder, policy
        self.discoveries, self.clock, self.automatic = discoveries, clock, automatic
        self.completion_hook = completion_hook

    def _read(self, context, action, skills, callback, *, read_only=False):
        """Bookkeeping under the workflow lock, or with `read_only` a plain read
        transaction that neither waits for that lock nor copies state.

        Reserving an operation or capturing state for a later commit must see a
        settled graph, so it queues behind writers. A pure read, status polling
        above all, would otherwise wait behind a long apply and copy the memory
        store for nothing; it takes the read-only path where the repository has
        one.
        """
        self.policy.admit(action, context, skills)
        if any(skill.tenant_id != context.tenant_id for skill in skills):
            raise SourceForbidden("source_scope_denied", "foreign skill")

        def read(graph, queue):
            bound = self.factory_for(context, action, skills)(graph, queue)
            bound.admit()
            return callback(bound)
        if read_only and hasattr(self.repository, "query"):
            return self.repository.query("source:" + action, context.tenant_id, read)
        return self.repository.metadata("source:" + action, context.tenant_id, read)

    def _begin(self, context, action, request, key, skills, *, throttle_source=None, upload_replay=None):
        def begin(bound):
            previous = bound.sources.operation_for_key(key, tenant_id=context.tenant_id, actor_id=context.actor_id)
            if previous is not None:
                self.policy.admit_operation(context, previous, bound.graph)
            return operations.begin(bound.sources, context, action, request, key,
                                    self.clock(), throttle_source=throttle_source, upload_replay=upload_replay,
                                    scope_proofs=operations.capture_scopes(bound, context, request, skills))
        return self._read(context, action, skills, begin)

    def _replay(self, context, action, request, key, skills):
        def read(bound):
            previous = bound.sources.operation_for_key(key, tenant_id=context.tenant_id, actor_id=context.actor_id)
            if previous is None:
                return None
            self.policy.admit_operation(context, previous, bound.graph)
            operation, _ = operations.begin(bound.sources, context, action, request, key, self.clock())
            return operation
        return self._read(context, action, skills, read)

    def _policy_revision(self, context, bound, skills):
        revision = self.policy.revision(context, bound.graph, bound.reviews, skills)
        if not isinstance(revision, str) or not revision:
            raise SourceConflict("record_unavailable", "policy revision unavailable")
        return revision

    def _commit(self, context, action, operation, guards, callback):
        skills = tuple(guard.skill for guard in guards)
        base_factory = self.factory_for(context, action, skills)

        def factory(graph, reviews):
            bound = base_factory(graph, reviews)

            def admit():
                self.policy.admit(action, context, skills)
                bound.admit()
            return replace(bound, admit=admit)
        request = MutationRequest(operation_id=operation.operation_id, tenant_id=context.tenant_id,
            actor_id=context.actor_id, affected_skills=skills, expected_generations=guards,
            request_digest=operation.request_digest)

        # The lease is checked inside the transaction, not before it: a worker
        # that stalled past its lease, and was replaced, must not commit a result
        # on top of its successor's. The completion hook (the paid edition's
        # activity record) runs in the same transaction so it cannot report an
        # operation that then rolled back.
        def fenced(bound):
            current = bound.sources.get_operation(operation.operation_id, tenant_id=context.tenant_id)
            if (current is None or current.lease != operation.lease or current.lease.expires_at <= self.clock()):
                raise StaleMutation("operation_no_longer_active", "operation lease expired")
            result = callback(bound)
            if isinstance(result, OperationResult) and self.completion_hook is not None:
                self.completion_hook(bound, result)
            return result
        return self.repository.atomic_skill_change(request, factory, fenced)

    def _failed(self, context, action, operation, skills, error: Exception | None = None):
        # Written in a transaction of its own after the action's has rolled back,
        # so the client polling the operation learns it failed instead of
        # watching an operation that stays "applying" until its lease expires.
        def fail(sources):
            current = sources.get_operation(operation.operation_id, tenant_id=context.tenant_id)
            if current and current.result.state not in operations.TERMINAL:
                operations.finish(sources, operation, OperationResult(operation_id=operation.operation_id,
                    state="failed", committed=False, outcomes=tuple(SkillOutcome(skill=skill, state="failed",
                        code=error.code if isinstance(error, SourceError) else "source_operation_failed")
                        for skill in skills)))
        self.repository.atomic("source:failed", context.tenant_id,
            lambda graph, queue: fail(source_repository(graph)))

    def discover(self, context: ActionContext, request: DiscoveryRequest, *, idempotency_key: str) -> DiscoveryResult:
        if request.actor_id != context.actor_id or request.ref.tenant_id != context.tenant_id:
            raise SourceForbidden("source_scope_denied", "discovery scope mismatch")
        operation, fresh = self._begin(context, "discover", request, idempotency_key, ())
        if not fresh:
            if operation.discovery_id is None:
                raise SourceConflict("record_unavailable", "discovery not available")
            return self.discoveries.get(operation.discovery_id, tenant_id=context.tenant_id, actor_id=context.actor_id)
        try:
            self.discoveries.authorise(context.tenant_id, context.actor_id, request.ref.credential_profile_id)
            result = self.reader.discover(request)
            if (result.tenant_id != context.tenant_id or result.actor_id != context.actor_id
                    or result.credential_profile_id != request.ref.credential_profile_id):
                raise SourceForbidden("source_scope_denied", "discovery scope mismatch")

            def commit(bound):
                retained = Discoveries(bound.sources, authorise=self.discoveries.authorise, clock=self.clock)
                retained.save(result)
                operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                    state="complete", committed=False), discovery_id=result.discovery_id)
                return result
            completed = self._commit(context, "discover", operation, (), commit)
            if not isinstance(completed, DiscoveryResult):
                raise DiscoveryAccessError("discovery operation no longer available")
            return completed
        except Exception as error:
            logger.warning("Source discovery failed", extra={"operation_id": operation.operation_id})
            self._failed(context, "discover", operation, (), error=error)
            raise

    def _merge_policy(self, snapshot, check, **options):
        return MergePolicy(policy_version=self.builder.policy_version,
            projection_version=self.builder.projection_version, required_checks=(check,),
            file_checks=tuple(FileCheck("file:" + entry.path, entry.digest, check) for entry in snapshot.manifest),
            **options)

    def install(self, context: ActionContext, request: InstallRequest, *, idempotency_key: str) -> OperationResult:
        self.policy.admit_install(context, request)
        operation, fresh = self._begin(context, "install", request, idempotency_key, ())
        if not fresh:
            return operation.result
        skills = ()
        try:
            discovery = self.discoveries.get(request.discovery_id, tenant_id=context.tenant_id,
                                             actor_id=context.actor_id)
            generations = self._read(context, "install", (), lambda bound:
                {guard.skill: guard for guard in bound.sources.generations_for_tenant(tenant_id=context.tenant_id)})
            source, packages = installation.prepare(self.builder, discovery, request, context,
                                                      operation.operation_id, generations)
            proposed_source = source
            existing_source = self._read(context, "install", (), lambda bound:
                installation.admitted_source(self, context, proposed_source, bound, request))
            if existing_source is not None:
                source = existing_source
            paths = tuple(package.selection.package_path for package in packages)
            installed = self._read(context, "install", (), lambda bound:
                installation.installed_packages(self, context, source, bound, paths))
            skills = tuple(installed[package.selection.package_path].origin.skill
                if package.selection.package_path in installed else package.snapshot.ref.origin.skill
                for package in packages)
            if len(skills) != len(set(skills)):
                raise SourceConflict("skill_already_exists")
            guards = tuple(generations.get(skill, Generations(skill=skill, content=0, binding=0)) for skill in skills)
            policy_revision = self._read(context, "install", (),
                lambda bound: self._policy_revision(context, bound, skills))
            checks = []
            for package, guard in zip(packages, guards):
                if package.selection.package_path in installed:
                    checks.append(True)
                    continue
                local = LocalState(skill=package.snapshot.ref.origin.skill, content_generation=guard.content,
                                   digest="absent")
                check = self.policy.screen(package.snapshot, local)
                plan = build_plan(None, package.snapshot, local,
                    self._merge_policy(package.snapshot, check, history_evidence="initial"))
                checks.append(check.state == "passed" and not any(flag in plan.flags for flag in
                    (PlanFlag.SAFETY_HOLD, PlanFlag.REQUIRED_CHECK_UNAVAILABLE))
                    and all(part.evidence.kind != "unknown" for part in package.snapshot.effective_projection))

            # Everything above ran in separate reads with fetched data in between.
            # The commit re-reads each thing a decision rested on (policy
            # revision, who owns the URL, what is installed, which skills exist)
            # and refuses if any moved, instead of trusting the earlier reads.
            def commit(bound):
                self.policy.admit_install(context, request)
                if self._policy_revision(context, bound, skills) != policy_revision:
                    raise StaleMutation("policy_changed")
                if bound.sources.source_for_url(proposed_source.canonical_url,
                                                tenant_id=context.tenant_id) != existing_source:
                    raise StaleMutation("source_changed")
                self.discoveries.select(request.discovery_id, tuple(row.selection.package_path for row in packages),
                    tenant_id=context.tenant_id, actor_id=context.actor_id)
                if installation.installed_packages(self, context, source, bound, paths) != installed:
                    raise StaleMutation("binding_changed", "installation changed")
                new_skills = tuple(package.snapshot.ref.origin.skill for package in packages
                                   if package.selection.package_path not in installed)
                if any(bound.graph.get_skill(skill.skill_id, tenant_id=context.tenant_id) is not None
                       for skill in new_skills):
                    raise SourceConflict("skill_already_exists")
                if not all(checks):
                    return operations.finish(bound.sources, operation,
                                             OperationResult(operation_id=operation.operation_id,
                        state="blocked", committed=False,
                        outcomes=tuple(SkillOutcome(skill=skill, state="blocked", code="required_check_held")
                                       for skill in skills)))
                if new_skills:
                    bound.sources.put_source(source)
                histories, outcomes = [], []
                for package in packages:
                    existing = installed.get(package.selection.package_path)
                    if existing is not None:
                        outcomes.append(SkillOutcome(skill=existing.origin.skill, state="unchanged",
                                                     code="already_installed"))
                        continue
                    ref = package.snapshot.ref.origin.skill
                    bound.graph.upsert_skill(Skill(id=ref.skill_id, name=package.selection.local_name,
                        description="", domain=package.selection.domain, tenant_id=ref.tenant_id))
                    snapshot = apply_parts(bound, package.snapshot, package.snapshot.effective_projection,
                        now=self.clock(), operation_id=operation.operation_id)
                    bound.sources.put_snapshot(snapshot)
                    bound.sources.put_binding(Binding(origin=snapshot.ref.origin, source_id=source.source_id,
                        package_path=package.selection.package_path,
                        ref=package.acquired.resolved_ref.model_copy(update={"canonical_url": source.canonical_url}),
                        baseline=snapshot.ref, first_reconciliation=False, automatic_apply=self.automatic,
                        automation_actor_id=context.actor_id))
                    history = bound.history.capture_required(ref.skill_id, ref.tenant_id,
                        cause=SkillVersionCause.SOURCE_INSTALL, actor=context.actor_id,
                        source_operation_id=operation.operation_id, source_origin_id=snapshot.ref.origin.origin_id,
                        source_revision=snapshot.revision)
                    if history:
                        histories.append(history.id)
                    outcomes.append(SkillOutcome(skill=ref, state="applied"))
                event_ids = ()
                if new_skills:
                    event_id = operation.operation_id + ":installed"
                    bound.events.append_event(DurableEvent(event_id=event_id, tenant_id=context.tenant_id,
                        operation_id=operation.operation_id, action="source_install", skills=new_skills))
                    event_ids = (event_id,)
                return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                    state="complete", committed=bool(new_skills), outcomes=tuple(outcomes)),
                    history_ids=histories, event_ids=event_ids)
            from oms.sources.scoped import with_admission
            protected = with_admission(self,
                                       lambda bound: installation.admitted_source(self, context,
                                           proposed_source, bound, request))
            return protected._commit(context, "install", operation, guards, commit)
        except Exception as error:
            logger.warning("Source installation failed", extra={"operation_id": operation.operation_id})
            self._failed(context, "install", operation, skills, error=error)
            raise

    def check(self, context: ActionContext, request: CheckRequest, *, idempotency_key: str) -> OperationResult:
        from oms.sources.checks import check
        return check(self, context, request, idempotency_key=idempotency_key)

    def apply(self, context: ActionContext, request: ApplyRequest, *, idempotency_key: str) -> OperationResult:
        skill = request.skill
        operation, fresh = self._begin(context, "apply", request, idempotency_key, (skill,))
        if not fresh:
            return operation.result
        from oms.sources.apply import capture_apply

        def capture(bound):
            return capture_apply(self, context, request, bound)
        try:
            update, binding, incoming, local, guards = self._read(context, "apply", (skill,), capture)
            aligned, selected, manual_parts = review.selected_writes(update, incoming, local, request.removal_consents)
            if self.policy.screen_resolved(incoming, local, selected).state != "passed":
                raise SourceConflict("required_check_held")

            def commit(bound):
                if capture(bound) != (update, binding, incoming, local, guards):
                    raise StaleMutation("update_changed", "candidate changed")
                from oms.sources.apply import PreparedApply, commit_apply
                prepared = PreparedApply(update=update, holder=binding, incoming=incoming, aligned=aligned,
                    local=local, guards=guards, selected=selected, manual_parts=manual_parts)
                return commit_apply(bound, context, operation, prepared, now=self.clock(),
                                    policy_version=self.builder.policy_version, policy=self.policy)
            return self._commit(context, "apply", operation, guards, commit)
        except Exception as error:
            logger.warning("Source apply failed", extra={"operation_id": operation.operation_id})
            self._failed(context, "apply", operation, (skill,), error=error)
            raise

    def get_operation(self, context: ActionContext, operation_id: str) -> OperationResult:
        def read(bound):
            operation = bound.sources.get_operation(operation_id, tenant_id=context.tenant_id)
            if operation is None or operation.context.actor_id != context.actor_id:
                raise SourceNotFound("operation_not_found")
            self.policy.admit_operation(context, operation, bound.graph)
            operation = operations.refresh_batch(bound.sources, operation, self.clock())
            if (operation.result.state not in operations.TERMINAL and operation.lease is not None
                    and operation.lease.expires_at <= self.clock()):
                operation = operation.model_copy(update={"result": OperationResult(operation_id=operation_id,
                    state="failed", committed=False)})
                bound.sources.put_operation(operation)
            return (operation.result.model_copy(update={"discovery_id": operation.discovery_id})
                    if operation.discovery_id else operation.result)
        return self._read(context, "operation", (), read)

    def get_update(self, context: ActionContext, update_id: str) -> Update:
        def read(bound):
            from oms.sources.retirement import retired_update
            update = bound.sources.get_update(update_id, tenant_id=context.tenant_id)
            if update is None or retired_update(bound.sources, update):
                raise SourceNotFound("update_not_found")
            self.policy.admit("update", context, (update.plan.skill,))
            return update
        return self._read(context, "update", (), read)

    def save_draft(self, context: ActionContext, request: SaveDraftRequest, *, idempotency_key: str) -> OperationResult:
        from oms.sources.review_actions import decide
        return decide(self, context, request, idempotency_key, "save_draft")

    def skip(self, context: ActionContext, request: UpdateRequest, *, idempotency_key: str) -> OperationResult:
        from oms.sources.review_actions import decide
        return decide(self, context, request, idempotency_key, "skip")

    def adopt(self, context: ActionContext, request: UpdateRequest, *, idempotency_key: str) -> OperationResult:
        from oms.sources.review_actions import decide
        return decide(self, context, request, idempotency_key, "adopt")

    def recheck(self, context: ActionContext, request: UpdateRequest, *, idempotency_key: str) -> OperationResult:
        from oms.sources.review_actions import decide
        return decide(self, context, request, idempotency_key, "recheck")

    def submit_local(self, context: ActionContext, request: LocalImportRequest, *,
                     idempotency_key: str) -> OperationResult:
        from oms.sources.local_import import submit_local
        return submit_local(self, context, request, idempotency_key=idempotency_key)

    def link(self, context: ActionContext, request: LinkRequest, *, idempotency_key: str) -> OperationResult:
        from oms.sources.linking import switch
        return switch(self, context, request, idempotency_key, "link")

    def relink(self, context: ActionContext, request: LinkRequest, *, idempotency_key: str) -> OperationResult:
        from oms.sources.linking import switch
        return switch(self, context, request, idempotency_key, "relink")

    def retarget(self, context: ActionContext, request: RetargetRequest, *, idempotency_key: str) -> OperationResult:
        from oms.sources.linking import retarget
        return retarget(self, context, request, idempotency_key=idempotency_key)

    def unlink(self, context: ActionContext, request: UnlinkRequest, *, idempotency_key: str) -> OperationResult:
        from oms.sources.retirement import change_binding
        return change_binding(self, context, request, idempotency_key, "unlink")

    def remove_source(self, context: ActionContext, request: RemoveSourceRequest, *,
                      idempotency_key: str) -> OperationResult:
        from oms.sources.retirement import change_source
        return change_source(self, context, request, idempotency_key, "remove_source")

    def relocate_source(self, context: ActionContext, request: RelocateSourceRequest, *,
                        idempotency_key: str) -> OperationResult:
        from oms.sources.relocation import relocate
        return relocate(self, context, request, idempotency_key=idempotency_key)

    def configure_automation(self, context: ActionContext, request: AutomationRequest, *,
                             idempotency_key: str) -> OperationResult:
        from oms.sources.retirement import change_binding
        return change_binding(self, context, request, idempotency_key, "enable_automation" if request.enabled
                              else "disable_automation")

    def configure_schedule(self, context: ActionContext, request: ScheduleRequest, *,
                           idempotency_key: str) -> OperationResult:
        from oms.sources.retirement import change_source
        return change_source(self, context, request, idempotency_key, "configure_schedule")

    def submit_local_batch(self, context: ActionContext, request: LocalBatchRequest, *, idempotency_key: str,
                           upload_replay=None) -> OperationResult:
        from oms.sources.local_import import submit_local_batch
        return submit_local_batch(self, context, request, idempotency_key=idempotency_key, upload_replay=upload_replay)

    def create_source(self, context: ActionContext, request: CreateSourceRequest, *,
                      idempotency_key: str) -> OperationResult:
        from oms.sources.installation import create_source
        return create_source(self, context, request, idempotency_key=idempotency_key)

    def undo(self, context: ActionContext, request: UndoRequest, *, idempotency_key: str) -> OperationResult:
        from oms.sources.undo import undo
        return undo(self, context, request, idempotency_key=idempotency_key)

    def undo_status(self, context: ActionContext, undo_id: str) -> UndoStatus:
        from oms.sources.undo import undo_status
        return undo_status(self, context, undo_id)

    def bulk_apply(self, context: ActionContext, request: BulkApplyRequest, *, idempotency_key: str) -> OperationResult:
        from oms.sources.bulk import bulk_apply
        return bulk_apply(self, context, request, idempotency_key=idempotency_key)
