"""Install skills from a discovery, and create sources.

The selected packages are projected from the retained discovery bytes and checked
before anything is written; the ordinary importer and its extensions are not
involved. A source is created from the discovery the actor was admitted to.
"""
from collections.abc import Callable
from dataclasses import dataclass

from oms.domain.identity import SkillRef
from oms.domain.ids import slug
from oms.domain.models import Skill
from oms.domain.repo import REPO_ID_PREFIX, ReservedSkillId, reserved_id_refusal
from oms.domain.types import SkillStatus, SkillVersionCause
from oms.ports.mutation import MutationContext
from oms.sources import operations, review
from oms.sources.discovery import Discoveries, DiscoveryAccessError
from oms.sources.errors import SourceConflict, SourceError, SourceNotFound
from oms.sources.findings import describe_hold, distinct_findings
from oms.sources.local_projection import project_local
from oms.sources.merge import build_plan
from oms.sources.models import (AcquiredPackage, ActionContext, Binding, CheckResult, DiscoveryResult, DurableEvent,
                                Generations, InstallRequest, InstallSelection, Operation, OperationResult, OriginRef,
                                SkillOutcome, Source, SourceSnapshot, SourceStatus)
from oms.sources.projection import ProjectionBuilder
from oms.sources.primitives import exact_digest
from oms.sources.scoped import policy_for, with_admission


@dataclass(frozen=True)
class Installation:
    selection: InstallSelection
    snapshot: SourceSnapshot | None
    acquired: AcquiredPackage
    # A safe reason when the package could not be read; the snapshot is then None.
    error: str | None = None

    def skill(self, tenant_id: str) -> SkillRef:
        """The skill this package installs as, named from the local name even when it could not be read."""
        if self.snapshot is not None:
            return self.snapshot.ref.origin.skill
        return SkillRef(tenant_id, slug(self.selection.local_name))


# The projection's own messages are fixed strings, but they are matched here and
# replaced rather than passed on, so no exception text ever reaches the person.
# SKILL.md is parsed in a child process, so its "not a valid skill document"
# arrives as the builder's "could not be projected".
_UNREADABLE = {
    "Colliding source heading identities": "two sections have headings OMS cannot tell apart",
    "Package does not contain a valid skill document": "the package has no valid SKILL.md",
    "Package document could not be projected": "the package has no valid SKILL.md",
    "Package requires SKILL.md": "the package has no valid SKILL.md",
}


def unreadable_reason(error: ValueError) -> str:
    return _UNREADABLE.get(str(error), "the package could not be read")


def prepare(builder: ProjectionBuilder, discovery: DiscoveryResult, request: InstallRequest,
            context: ActionContext, operation_id: str,
            generations: dict[SkillRef, Generations]) -> tuple[Source, tuple[Installation, ...]]:
    """Project each selected package on its own.

    A request that cannot be honoured as a whole (a bad selection, a reserved or
    repeated name, a package from another ref) is refused before anything is
    built. A package whose content cannot be projected fails alone, with a safe
    reason, and the rest of the batch carries on (spec 5.1).
    """
    paths = tuple(selection.package_path for selection in request.selections)
    if not paths or len(set(paths)) != len(paths):
        raise SourceConflict("invalid_package_selection")
    valid = {package.path for package in discovery.packages if package.valid}
    packages = {package.package_path: package for package in discovery.acquired_packages}
    result = []
    identities = set()
    for selection in request.selections:
        skill_id = slug(selection.local_name)
        if skill_id.startswith(REPO_ID_PREFIX):
            raise ReservedSkillId(reserved_id_refusal(skill_id))
        if (not skill_id or skill_id in identities or selection.package_path not in valid
            or selection.package_path not in packages):
            raise SourceConflict("invalid_package_selection")
        identities.add(skill_id)
        ref = SkillRef(context.tenant_id, skill_id)
        generation = generations.get(ref)
        origin = OriginRef(skill=ref, origin_id="origin-" + exact_digest([operation_id, skill_id]),
            kind="github", generation=(generation.binding if generation else 0) + 1)
        acquired = packages[selection.package_path]
        if acquired.resolved_ref != discovery.resolved_ref:
            raise SourceConflict("source_changed", "discovery package ref mismatch")
        try:
            snapshot = builder.build(acquired, origin,
                                     snapshot_id="snapshot-" + exact_digest([operation_id, skill_id]))
        except SourceError:
            raise
        except ValueError as error:
            result.append(Installation(selection, None, acquired, error=unreadable_reason(error)))
            continue
        result.append(Installation(selection, snapshot, acquired))
    return source_from_discovery(discovery), tuple(result)


def hold_new_skill(service, bound: MutationContext, context: ActionContext, operation: Operation, *,
                   snapshot: SourceSnapshot, name: str, domain: str, guard: Generations, check: CheckResult,
                   action: str, put_origin_state: Callable[[MutationContext], None],
                   binding_generation: int | None = None,
                   import_source_ref: str | None = None) -> tuple[SkillOutcome, str | None]:
    """Create a held package's skill empty and marked held, with a review card (spec 5.3).

    Nothing from the package becomes live: the skill has identity fields only, the
    origin's state record has no baseline, and the content waits on the card until a
    reviewer applies it. `put_origin_state(bound)` writes that record: a binding for
    a repository, a local stream for an upload. `binding_generation` is the skill's
    binding generation once that record is written, when writing it leaves the
    generation where it was; by default it is the origin's own, which a new binding
    advances to. `import_source_ref` is the skill's recorded import origin, for an
    origin that records one on the skill. Returns the outcome and the history
    entry's id.
    """
    ref = snapshot.ref.origin.skill
    bound.graph.upsert_skill(Skill(id=ref.skill_id, name=name, description="", domain=domain,
                                   tenant_id=ref.tenant_id, status=SkillStatus.HELD,
                                   import_source_ref=import_source_ref))
    # The card pins the skill as this commit leaves it, because that is what
    # apply compares against. Creating the skill changes its content, and the
    # tracker advances a changed skill's content generation by exactly one when
    # the transaction commits (ContentTracker.advance), so the generation the
    # card must carry is the guard's plus one. The policy digest is the skill's
    # own, as apply computes it, and is taken after the batch's other writes.
    local = project_local(bound.graph, ref, baseline=None, content_generation=guard.content + 1,
        ownership=bound.sources.ownership_for_skill(ref), policy_version=service.builder.policy_version)
    plan = build_plan(None, snapshot, local, service._merge_policy(snapshot, check,
        update_generation=1, policy_digest=service._policy_revision(context, bound, (ref,)),
        binding_generation=binding_generation, first_reconciliation=True, history_evidence="initial"))
    put_origin_state(bound)
    outcome = review.stage_update(bound, plan, snapshot, operation_id=operation.operation_id,
                                  actor_id=context.actor_id, action=action)
    # No source revision: the skill holds none of that revision's content yet.
    history = bound.history.capture_required(ref.skill_id, ref.tenant_id, cause=SkillVersionCause.SOURCE_INSTALL,
        actor=context.actor_id, source_operation_id=operation.operation_id,
        source_origin_id=snapshot.ref.origin.origin_id)
    if outcome.state == "awaiting_review":
        outcome = outcome.model_copy(update={"code": "safety_review_required",
                                             "detail": describe_hold(distinct_findings(plan.check_findings))})
    return outcome, history.id if history else None


def hold_package(service, bound: MutationContext, context: ActionContext, operation: Operation, source: Source,
                 package: Installation, guard: Generations, check: CheckResult) -> tuple[SkillOutcome, str | None]:
    """Install a held package as an empty skill marked held, with a review card (spec 5.3).

    The binding is written with no baseline. Returns the outcome and the history
    entry's id.
    """
    def put_binding(current: MutationContext) -> None:
        current.sources.put_binding(Binding(origin=package.snapshot.ref.origin, source_id=source.source_id,
            package_path=package.selection.package_path,
            ref=package.acquired.resolved_ref.model_copy(update={"canonical_url": source.canonical_url}),
            baseline=None, first_reconciliation=True, automatic_apply=service.automatic,
            automation_actor_id=context.actor_id,
            status=SourceStatus(state="updates_available", checked_at=service.clock())))
    return hold_new_skill(service, bound, context, operation, snapshot=package.snapshot,
                          name=package.selection.local_name, domain=package.selection.domain, guard=guard,
                          check=check, action="install", put_origin_state=put_binding)


def source_from_discovery(discovery: DiscoveryResult) -> Source:
    from oms.sources.links import parse_link
    canonical = parse_link(discovery.resolved_ref.canonical_url).repository_url
    return Source(source_id="source-" + exact_digest([discovery.tenant_id, canonical.casefold().removesuffix(".git"),
        discovery.credential_profile_id]), tenant_id=discovery.tenant_id, canonical_url=canonical,
        credential_profile_id=discovery.credential_profile_id, discovery_root=discovery.resolved_ref.package_path,
        repository=discovery.repository)


def admitted_source(service, context, proposed, bound, request=None):
    existing = bound.sources.source_for_url(proposed.canonical_url, tenant_id=context.tenant_id)
    policy = policy_for(service, bound)
    if request is not None:
        policy.admit_install(context, request)
    policy.admit_profile(context, (existing or proposed).credential_profile_id)
    return existing


def installed_packages(service, context, source, bound, paths):
    """Resolve repository/path identity only after admitting each existing skill."""
    from oms.sources.errors import SourceForbidden, SourceNotFound
    policy = policy_for(service, bound)
    result = {}
    for binding in bound.sources.active_bindings(source.source_id, tenant_id=context.tenant_id):
        if binding.package_path not in paths:
            continue
        try:
            policy.admit("read", context, (binding.origin.skill,))
            if bound.graph.get_skill(binding.origin.skill.skill_id, tenant_id=context.tenant_id) is None:
                raise SourceNotFound("skill_not_found", "installation unavailable")
        except (PermissionError, SourceForbidden, SourceNotFound):
            raise SourceNotFound("skill_not_found", "installation unavailable") from None
        result[binding.package_path] = binding
    return result


def create_source(service, context, request, *, idempotency_key):
    operation, fresh = service._begin(context, "create_source", request, idempotency_key, ())
    if not fresh:
        if operation.result.source_id:
            def readmitted(bound):
                source = bound.sources.get_source(operation.result.source_id, tenant_id=context.tenant_id)
                if source is None:
                    raise SourceNotFound("source_not_found")
                policy_for(service, bound).admit_profile(context, source.credential_profile_id)
            service._read(context, "create_source", (), readmitted)
        return operation.result
    try:
        discovery = service.discoveries.get(request.discovery_id, tenant_id=context.tenant_id,
                                            actor_id=context.actor_id)
        proposed = source_from_discovery(discovery)
        service._read(context, 'create_source', (), lambda bound: admitted_source(service, context, proposed, bound))

        def commit(bound):
            retained = Discoveries(bound.sources, authorise=service.discoveries.authorise, clock=service.clock)
            if retained.get(request.discovery_id, tenant_id=context.tenant_id, actor_id=context.actor_id) != discovery:
                raise ValueError("discovery changed")
            existing = bound.sources.source_for_url(proposed.canonical_url, tenant_id=context.tenant_id)
            source = existing or proposed
            if discovery.expires_at <= service.clock():
                raise DiscoveryAccessError("discovery unavailable or expired")
            if existing is None:
                bound.sources.put_source(source)
            event_id = operation.operation_id + ":source"
            bound.events.append_event(DurableEvent(event_id=event_id, tenant_id=context.tenant_id,
                operation_id=operation.operation_id, action="source_created" if existing is None
                else "source_reused", skills=()))
            return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                state="complete", committed=False, source_id=source.source_id), event_ids=(event_id,))
        protected = with_admission(service, lambda bound: admitted_source(service, context, proposed, bound))
        return protected._commit(context, "create_source", operation, (), commit)
    except Exception as error:
        service._failed(context, "create_source", operation, (), error=error)
        raise
