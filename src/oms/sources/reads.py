"""Read views of sources, bindings, updates and history for the routes.

Every page is filtered by what the caller may see before it is cut, so a page never
contains a skill outside their scope and a cursor never skips past one they may see.
"""
from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import datetime, timezone
import json

from oms.domain.identity import SkillRef
from oms.ports.blob_store import BlobStore
from oms.ports.graph_store import GraphStore
from oms.publish.publisher import Publisher
from oms.sources.api_models import DiscoveryView, SkillGenerationBody, SourceHistoryEntry, UndoView, UpdateView
from oms.sources.errors import SourceConflict, SourceForbidden, SourceNotFound, StaleMutation
from oms.sources.files import FileSide, require_path, verified_bytes
from oms.sources.local_projection import project_local
from oms.sources.retirement import retired_binding, retired_update
from oms.sources.models import ActionContext, CheckResult, Page, Source, Binding, Update
from oms.sources.service import SourceService


def page[T](items: Iterable[T], key: Callable[[T], str], cursor: str | None,
            limit: int) -> Page[T]:
    if not 1 <= limit <= 100:
        raise ValueError("Page size must be between 1 and 100")
    rows = sorted((row for row in items if cursor is None or key(row) > cursor), key=key)
    chosen = tuple(rows[:limit])
    return Page(items=chosen, next_cursor=key(chosen[-1]) if len(rows) > limit else None)


class SourceReads:
    def __init__(self, service: SourceService, blobs: BlobStore, *,
                 publisher_factory: Callable[[GraphStore], Publisher] = Publisher):
        self.service, self.blobs, self.publisher_factory = service, blobs, publisher_factory

    def workspace(self, context: ActionContext) -> str:
        existing = self.service._read(context, "read", (), lambda bound:
            bound.sources.workspace_id(tenant_id=context.tenant_id, create=False), read_only=True)
        return existing or self.service._read(context, "read", (), lambda bound:
            bound.sources.workspace_id(tenant_id=context.tenant_id))

    def _skill(self, context: ActionContext, graph: GraphStore, skill: SkillRef) -> None:
        if skill.tenant_id != context.tenant_id or graph.get_skill(skill.skill_id, tenant_id=context.tenant_id) is None:
            raise SourceNotFound("skill_not_found")
        try:
            self.service.policy.admit("read", context, (skill,))
        except (SourceForbidden, PermissionError) as exc:
            raise SourceNotFound("skill_not_found") from exc

    def _scope(self, context: ActionContext, graph: GraphStore) -> tuple[SkillRef, ...]:
        allowed = []
        for skill in graph.skills_for_tenant(context.tenant_id):
            ref = SkillRef(context.tenant_id, skill.id)
            try:
                self._skill(context, graph, ref)
            except SourceNotFound:
                continue
            allowed.append(ref)
        return tuple(sorted(allowed))

    def sources(self, context: ActionContext, *, cursor=None, limit=100) -> Page[Source]:
        def read(bound):
            scope = self._scope(context, bound.graph)
            result = bound.sources.sources(tenant_id=context.tenant_id, skill_scope=scope, cursor=cursor, limit=limit)
            return result.model_copy(update={"items": tuple(self._visible_source(bound, source, scope)
                                                            for source in result.items)})
        return self.service._read(context, "list_sources", (), read, read_only=True)

    @staticmethod
    def _visible_source(bound, source, scope):
        from oms.sources.review import summarize_status
        bindings = tuple(binding for ref in scope if (binding := bound.sources.get_binding(ref)) is not None
                         and binding.active and binding.source_id == source.source_id)
        return source.model_copy(update={"status": summarize_status(bindings)})

    def source(self, context: ActionContext, source_id: str, *, cursor=None, limit=100):
        def read(bound):
            scope = self._scope(context, bound.graph)
            source = bound.sources.get_source(source_id, tenant_id=context.tenant_id)
            if source is None or not bound.sources.bindings(source_id, tenant_id=context.tenant_id,
                                                           skill_scope=scope, limit=1).items:
                raise SourceNotFound("source_not_found")
            return {"source": self._visible_source(bound, source, scope), "bindings": bound.sources.bindings(source_id,
                tenant_id=context.tenant_id, skill_scope=scope, cursor=cursor, limit=limit)}
        return self.service._read(context, "list_sources", (), read, read_only=True)

    def binding(self, context: ActionContext, skill_id: str):
        ref = SkillRef(context.tenant_id, skill_id)

        def read(bound):
            self._skill(context, bound.graph, ref)
            binding = bound.sources.get_binding(ref)
            return {"binding": None if retired_binding(bound.sources, binding) else binding,
                    "local_stream": bound.sources.get_local_stream(ref),
                    "generations": bound.sources.get_generations(ref),
                    "blocked_attempts": bound.sources.blocked_attempts(ref)}
        return self.service._read(context, "read", (), read, read_only=True)

    def updates(self, context: ActionContext, *, cursor=None, limit=100) -> Page[Update]:
        def read(bound):
            updates = (update for ref in self._scope(context, bound.graph)
                       if (update := bound.sources.open_update(ref)) is not None)
            return page(updates, lambda row: row.update_id, cursor, limit)
        return self.service._read(context, "list_sources", (), read, read_only=True)

    def update(self, context: ActionContext, update_id: str) -> UpdateView:
        def read(bound):
            update = bound.sources.get_update(update_id, tenant_id=context.tenant_id)
            if update is None or retired_update(bound.sources, update):
                raise SourceNotFound("update_not_found")
            self._skill(context, bound.graph, update.plan.skill)
            from oms.sources.review import part_fingerprint
            ref = update.plan.skill
            base = bound.sources.get_snapshot(update.plan.base) if update.plan.base else None
            guard = bound.sources.get_generations(ref)
            local = project_local(bound.graph, ref, baseline=base, content_generation=guard.content,
                ownership=bound.sources.ownership_for_skill(ref), policy_version=self.service.builder.policy_version)
            fp = update.plan.fingerprint
            stale = (guard.content != fp.content_generation or guard.binding != fp.binding_generation
                     or local.digest != fp.local_digest or self.service.policy.revision(context, bound.graph,
                          bound.reviews, (ref,)) != fp.policy_digest)
            parts = {part.part_id: part for part in local.parts}
            mappings = {mapping.part_id: mapping for mapping in local.graph_mappings}
            ownership = {}
            for change in update.plan.changes:
                part, mapping = parts.get(change.part_id), mappings.get(change.part_id)
                label = "unknown"
                if not stale and part is not None and mapping is not None and mapping.owner_skills:
                    if any(owner != ref for owner in mapping.owner_skills):
                        label = "shared"
                    elif part.owner_origins == (update.plan.origin.origin_id,) and not part.curated:
                        label = "source"
                    else:
                        label = "local"
                ownership[change.part_id] = label
            view = UpdateView(**update.model_dump(), stale=stale, ownership=ownership,
                part_fingerprints={change.part_id: part_fingerprint(update, change.part_id)
                                   for change in update.plan.changes})
            return view, bound.sources.get_snapshot(update.plan.incoming), local
        view, incoming, local = self.service._read(context, "read", (), read, read_only=True)
        if view.stale or incoming is None:
            checked = CheckResult(state="unavailable", code="review_state_changed")
        else:
            from oms.sources.review import selected_writes
            try:
                _, selected, _ = selected_writes(view, incoming, local, (), preview=True)
            except SourceConflict:
                checked = CheckResult(state="held", code="review_decisions_required")
            else:
                checked = self.service.policy.screen_resolved(incoming, local, selected)
        self.service.policy.admit("read", context, (view.plan.skill,))
        return view.model_copy(update={"resolved_check": checked})

    def history(self, context: ActionContext, skill_id: str, *, cursor=None, limit=100):
        if not 1 <= limit <= 100:
            raise ValueError("Page size must be between 1 and 100")
        before, before_id = None, None
        if cursor is not None:
            boundary = json.loads(cursor)
            if (not isinstance(boundary, list) or len(boundary) != 2
                    or any(not isinstance(value, str) or not value for value in boundary)):
                raise ValueError("Invalid history cursor")
            before, before_id = datetime.fromisoformat(boundary[0]), boundary[1]
            if before.tzinfo is None:
                raise ValueError("Invalid history cursor")
        ref = SkillRef(context.tenant_id, skill_id)

        def read(bound):
            self._skill(context, bound.graph, ref)
            versions = bound.graph.skill_versions(context.tenant_id, skill_id,
                limit=limit + 1, before=before, before_id=before_id)
            chosen = tuple(versions[:limit])
            following = (json.dumps([chosen[-1].at.astimezone(timezone.utc).isoformat(), chosen[-1].id],
                separators=(",", ":")) if len(versions) > limit else None)
            return Page(items=chosen, next_cursor=following)
        versions = self.service._read(context, "history", (), read, read_only=True)
        entries = []
        for version in versions.items:
            undo = None
            if version.source_operation_id:
                try:
                    status = self.service.undo_status(context, version.source_operation_id + ":undo")
                except (SourceNotFound, SourceForbidden, PermissionError):
                    pass
                else:
                    undo = UndoView(undo_id=status.undo_id, update_id=status.update_id, skill_id=status.skill.skill_id,
                        expected_generations=tuple(SkillGenerationBody(skill_id=row.skill.skill_id, content=row.content,
                            binding=row.binding) for row in status.expected_generations),
                            policy_version=status.policy_version,
                        available=status.available, reasons=status.reasons)
            entries.append(SourceHistoryEntry(**asdict(version), undo=undo))
        self.service.policy.admit("read", context, (ref,))
        return Page(items=tuple(entries), next_cursor=versions.next_cursor)

    def verify_undo(self, context: ActionContext, update_id: str, undo_id: str, skill_id: str) -> None:
        ref = SkillRef(context.tenant_id, skill_id)

        def read(bound):
            self._skill(context, bound.graph, ref)
            update = bound.sources.get_update(update_id, tenant_id=context.tenant_id)
            undo = bound.sources.get_undo(undo_id, tenant_id=context.tenant_id)
            if (update is None or undo is None or update.plan.skill != ref or undo.origin.skill != ref
                    or update.operation_id != undo.operation_id or retired_update(bound.sources, update)):
                raise SourceNotFound("undo_not_found")
        self.service._read(context, "read", (), read, read_only=True)

    def discovery(self, context: ActionContext, discovery_id: str, *, cursor=None, limit=100) -> DiscoveryView:
        self.service.policy.admit("discover", context, ())
        result = self.service.discoveries.get(discovery_id, tenant_id=context.tenant_id, actor_id=context.actor_id)
        return DiscoveryView(discovery_id=result.discovery_id, resolved_ref=result.resolved_ref,
            expires_at=result.expires_at.isoformat(), preselected_path=result.preselected_path,
            packages=page(result.packages, lambda row: row.path, cursor, limit))

    def delete_discovery(self, context: ActionContext, discovery_id: str) -> None:
        self.discovery(context, discovery_id, limit=1)
        self.service._read(context, "discover", (), lambda bound: bound.sources.delete_discovery(
            discovery_id, tenant_id=context.tenant_id, actor_id=context.actor_id))

    def discovery_file(self, context: ActionContext, discovery_id: str, package_path: str, path: str) -> bytes:
        require_path(path)
        self.service.policy.admit("discover", context, ())
        package, = self.service.discoveries.select(discovery_id, (package_path,),
            tenant_id=context.tenant_id, actor_id=context.actor_id)
        entry = next((entry for entry in package.manifest if entry.path == path), None)
        if entry is None:
            raise SourceNotFound("file_not_found")
        return verified_bytes(self.blobs, entry)

    def update_file(self, context: ActionContext, update_id: str, side: FileSide, path: str) -> bytes:
        require_path(path)

        def read(bound):
            update = bound.sources.get_update(update_id, tenant_id=context.tenant_id)
            if update is None or retired_update(bound.sources, update):
                raise SourceNotFound("update_not_found")
            ref = update.plan.skill
            self._skill(context, bound.graph, ref)
            selected = update.plan.base if side == "base" else update.plan.incoming
            if side == "local":
                base = bound.sources.get_snapshot(update.plan.base) if update.plan.base else None
                guard = bound.sources.get_generations(ref)
                local = project_local(bound.graph, ref, baseline=base, content_generation=guard.content,
                    ownership=bound.sources.ownership_for_skill(ref),
                    policy_version=self.service.builder.policy_version)
                if (guard.content != update.plan.fingerprint.content_generation
                        or guard.binding != update.plan.fingerprint.binding_generation
                        or local.digest != update.plan.fingerprint.local_digest):
                    raise StaleMutation("content_changed", "local file changed")
                if path == "SKILL.md":
                    return self.publisher_factory(bound.graph).render_skill(ref.skill_id, ref.tenant_id).encode()
                manifest = local.manifest
            elif side in {"base", "upstream"} and selected is not None:
                snapshot = bound.sources.get_snapshot(selected)
                manifest = snapshot.manifest if snapshot else ()
            else:
                raise SourceNotFound("file_not_found")
            entry = next((entry for entry in manifest if entry.path == path), None)
            if entry is None:
                raise SourceNotFound("file_not_found")
            return verified_bytes(self.blobs, entry)
        return self.service._read(context, "read", (), read, read_only=True)
