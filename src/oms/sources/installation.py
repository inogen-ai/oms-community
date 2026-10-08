"""Install skills from a discovery, and create sources.

The selected packages are projected from the retained discovery bytes and checked
before anything is written; the ordinary importer and its extensions are not
involved. A source is created from the discovery the actor was admitted to.
"""
from dataclasses import dataclass

from oms.domain.identity import SkillRef
from oms.domain.ids import slug
from oms.domain.repo import REPO_ID_PREFIX, ReservedSkillId, reserved_id_refusal
from oms.sources import operations
from oms.sources.discovery import Discoveries, DiscoveryAccessError
from oms.sources.errors import SourceConflict, SourceNotFound
from oms.sources.models import (AcquiredPackage, ActionContext, DiscoveryResult, DurableEvent, Generations,
                                InstallRequest,
    InstallSelection, OperationResult, OriginRef, Source, SourceSnapshot)
from oms.sources.projection import ProjectionBuilder
from oms.sources.primitives import exact_digest
from oms.sources.scoped import policy_for, with_admission


@dataclass(frozen=True)
class Installation:
    selection: InstallSelection
    snapshot: SourceSnapshot
    acquired: AcquiredPackage


def prepare(builder: ProjectionBuilder, discovery: DiscoveryResult, request: InstallRequest,
            context: ActionContext, operation_id: str,
            generations: dict[SkillRef, Generations]) -> tuple[Source, tuple[Installation, ...]]:
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
        snapshot = builder.build(acquired, origin, snapshot_id="snapshot-" + exact_digest([operation_id, skill_id]))
        result.append(Installation(selection, snapshot, acquired))
    return source_from_discovery(discovery), tuple(result)


def source_from_discovery(discovery: DiscoveryResult) -> Source:
    from oms.sources.links import parse_link
    canonical = parse_link(discovery.resolved_ref.canonical_url).repository_url
    return Source(source_id="source-" + exact_digest([discovery.tenant_id, canonical.casefold().removesuffix(".git"),
        discovery.credential_profile_id]), tenant_id=discovery.tenant_id, canonical_url=canonical,
        credential_profile_id=discovery.credential_profile_id, discovery_root=discovery.resolved_ref.package_path)


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
