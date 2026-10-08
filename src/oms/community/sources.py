"""Wire the source feature into the Community edition.

The local operator is the only actor, every action is allowed, and the collaborators
are bound to the existing workflow transaction; nothing here calls a model.
"""
from collections.abc import Callable

from oms.domain.identity import SkillRef
from oms.ports.blob_store import BlobStore
from oms.ports.graph_store import GraphStore
from oms.ports.mutation import MutationContext
from oms.ports.source_reader import SourceReaderFactory
from oms.ports.workflow import WorkflowRepository
from oms.publish.publisher import Publisher
from oms.skills.history import SkillHistory
from oms.sources.discovery import Discoveries
from oms.sources.errors import SourceForbidden
from oms.sources.git_reader import GitReader
from oms.sources.models import (ActionContext, Binding, CheckResult, InstallRequest, LocalImportRequest,
                                LocalState, ManifestEntry, MergePlan, ProjectionPart, SourceSnapshot)
from oms.sources.mutation import source_repository
from oms.sources.projection import ProjectionBuilder
from oms.sources.primitives import exact_digest
from oms.sources.reads import SourceReads
from oms.sources.service import SourceService
from oms.sources.settings import SourceSettings
from oms.sources.safety import screen_policy, revision_state


class LocalSourcePolicy:
    def __init__(self, tenant_id: str, actor_id: str, settings: SourceSettings, blobs: BlobStore, *,
                 graph=None, reviews=None):
        self.tenant_id, self.actor_id, self.settings, self.blobs = tenant_id, actor_id, settings, blobs
        self.graph, self.reviews = graph, reviews

    def context(self) -> ActionContext:
        return ActionContext(tenant_id=self.tenant_id, actor_id=self.actor_id, domain_scope=(),
                             capabilities=frozenset({"sources:manage"}))

    def admit(self, action: str, context: ActionContext, skill_refs: tuple[SkillRef, ...]) -> None:
        # One operator, one tenant, no service contexts: the Community edition
        # has no scheduler or automatic apply, so an action naming either
        # (`automatic_apply`, the schedule settings) is refused rather than
        # allowed by default.
        allowed = {"discover", "create_source", "install", "check", "link", "relink", "retarget", "save_draft",
                   "recheck",
                   "apply", "skip", "adopt", "undo", "bulk_apply", "unlink", "remove_source", "relocate_source",
                   "read", "list_sources", "history", "operation", "update", "local_import"}
        if (action not in allowed or context.tenant_id != self.tenant_id or context.actor_id != self.actor_id
                or context.service or any(ref.tenant_id != self.tenant_id for ref in skill_refs)):
            raise SourceForbidden("source_action_forbidden")

    def admit_install(self, context: ActionContext, request: InstallRequest) -> None:
        self.admit("install", context, ())
        if any(not selection.domain.strip() for selection in request.selections):
            raise SourceForbidden("source_scope_denied", "domain required")

    def admit_profile(self, context: ActionContext, profile_id: str | None) -> None:
        self.admit("discover", context, ())
        if profile_id is not None and profile_id not in {profile.profile_id for profile in self.settings.profiles}:
            raise SourceForbidden("source_action_forbidden", "profile unavailable")

    def admit_local(self, context: ActionContext, request: LocalImportRequest) -> None:
        self.admit("local_import", context, (request.skill,))
        if request.create and not request.domain.strip():
            raise SourceForbidden("source_scope_denied", "domain required")

    def admit_operation(self, context, operation, graph) -> None:
        refs = {row.skill for row in operation.result.outcomes} | {row.skill for row in operation.scope_proofs}
        refs.update(row.skill for row in operation.batch_members)
        self.admit("operation", context, tuple(sorted(refs)))

    def screen(self, snapshot: SourceSnapshot, local: LocalState) -> CheckResult:
        return screen_policy(self.graph, self.reviews, self.blobs, snapshot, local)

    def screen_resolved(self, snapshot: SourceSnapshot, local: LocalState,
                        writes: tuple[ProjectionPart, ...], *,
                        manifest: tuple[ManifestEntry, ...] | None = None) -> CheckResult:
        return screen_policy(self.graph, self.reviews, self.blobs, snapshot, local, writes=writes, manifest=manifest)

    def revision(self, context, graph, reviews, skill_refs) -> str:
        return exact_digest({"policy": "community-source-2",
            "live_holds": revision_state(graph, reviews, context.tenant_id, skill_refs),
            "constraints": sorted((row.id, row.body, row.status.value, row.polarity.value, row.immutable)
                                  for row in graph.all_constraints(context.tenant_id)),
            "reviews": sorted((row.id, row.kind, row.subject_id, row.other_id, row.reason)
                              for row in reviews.pending(context.tenant_id) if row.kind != "skill_update")})

    def automatic_eligibility(self, plan: MergePlan, binding: Binding) -> CheckResult:
        # Community never applies unattended; the local operator decides every
        # update (spec §8.4).
        return CheckResult(state="held", code="manual_edition")


def compose_sources(repository: WorkflowRepository, blobs: BlobStore, settings: SourceSettings, *,
                    tenant_id: str, actor_id: str, history_factory: Callable[[GraphStore], SkillHistory],
                    publisher_factory: Callable[[GraphStore], Publisher],
                    reader_factory: SourceReaderFactory | None = None) -> tuple[SourceService, SourceReads]:
    policy = LocalSourcePolicy(tenant_id, actor_id, settings, blobs, graph=repository.store, reviews=repository.queue)

    def factory_for(context, action, skills):
        def factory(graph, reviews):
            sources = source_repository(graph)
            history = history_factory(graph)
            return MutationContext(graph=graph, reviews=reviews, sources=sources, history=history,
                events=sources, admit=lambda: policy.admit(action, context, skills), rollback_participants=(history,))
        return factory

    def authorise(tenant, actor, profile):
        if tenant != tenant_id or actor != actor_id:
            raise SourceForbidden("source_scope_denied", "discovery scope mismatch")
        policy.admit_profile(policy.context(), profile)
    grants = frozenset(profile.profile_id for profile in settings.profiles)
    reader = (reader_factory(settings, blob_store=blobs) if reader_factory else
              GitReader(settings, blob_store=blobs, profile_grants=lambda tenant: grants if tenant == tenant_id
                        else frozenset()))
    discoveries = Discoveries(source_repository(repository.store), authorise=authorise)
    builder = ProjectionBuilder(blobs, settings.cache_root.parent / "source-projections")
    # The source repository doubles as the event writer: Community has no
    # notification delivery, so events are retained as the audit record only.
    service = SourceService(repository, factory_for, reader, builder, policy, discoveries, automatic=False)
    return service, SourceReads(service, blobs, publisher_factory=publisher_factory)
