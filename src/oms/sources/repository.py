"""The source records and the invariants every write to them must satisfy.

An adapter keeps sources, bindings, snapshots, updates, generations, operations,
leases and events as JSON rows and supplies a transaction; everything a write
must check lives here, so the memory and Neo4j adapters cannot drift apart.
"""
from collections.abc import Callable
from datetime import datetime
from functools import wraps
import json
from typing import TypeVar
from uuid import uuid4

from oms.domain.identity import SkillRef
from oms.sources.errors import SourceConflict, StaleMutation
from oms.sources.links import parse_link
from oms.sources.models import (
    Binding, BlockedAttempt, CheckThrottle, DiscoveryResult, DurableEvent,
    Generations, Lease, LocalStream, Operation, OriginRef, Page, Record,
    SnapshotRef, Source, SourceSnapshot, UndoRecord, Update, PartOwnership, WorkspaceIdentity,
)

T = TypeVar("T")


def record_key(*parts: str) -> str:
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def _tenant(value: object) -> str:
    if isinstance(value, (SkillRef, Source, Lease, CheckThrottle, DiscoveryResult, DurableEvent)):
        return value.tenant_id
    if isinstance(value, (Binding, LocalStream, BlockedAttempt, UndoRecord)):
        return value.origin.skill.tenant_id
    if isinstance(value, SourceSnapshot):
        return value.ref.origin.skill.tenant_id
    if isinstance(value, Update):
        return value.plan.skill.tenant_id
    if isinstance(value, Generations):
        return value.skill.tenant_id
    if isinstance(value, PartOwnership):
        return value.skill.tenant_id
    if isinstance(value, Operation):
        return value.context.tenant_id
    if isinstance(value, OriginRef):
        return value.skill.tenant_id
    raise TypeError("Source write needs an explicit tenant")


def transactional(method):
    # Every write runs inside the adapter's transaction for the tenant of its
    # first argument, so a method body never has to open one and a caller
    # already inside one is simply joined.
    @wraps(method)
    def write(self, *args, **kwargs):
        tenant = kwargs.get("tenant_id") or _tenant(args[0])
        return self._atomic(tenant, lambda bound: method(bound, *args, **kwargs))
    return write


class SourceRecords:
    """Adapters provide JSON custody and a transaction; this class owns invariants."""

    def _atomic(self, tenant_id: str, operation: Callable[["SourceRecords"], T]) -> T:
        raise NotImplementedError

    def _tracks_content(self) -> bool:
        return False

    def workspace_id(self, *, tenant_id: str, create: bool = True) -> str | None:
        identity = self._load(WorkspaceIdentity, tenant_id, "workspace", "identity")
        if identity is not None or not create:
            return identity.workspace_id if identity is not None else None
        return self._create_workspace(tenant_id=tenant_id)

    @transactional
    def _create_workspace(self, *, tenant_id: str) -> str:
        identity = self._load(WorkspaceIdentity, tenant_id, "workspace", "identity")
        if identity is None:
            identity = WorkspaceIdentity(tenant_id=tenant_id, workspace_id=str(uuid4()))
            self._save(tenant_id, "workspace", "identity", identity)
        return identity.workspace_id

    def _get(self, tenant: str, kind: str, key: str) -> str | None:
        raise NotImplementedError

    def _put(self, tenant: str, kind: str, key: str, value: str) -> None:
        raise NotImplementedError

    def _delete(self, tenant: str, kind: str, key: str) -> None:
        raise NotImplementedError

    def _rows(self, tenant: str, kind: str, *, prefix: str = "") -> list[tuple[str, str]]:
        raise NotImplementedError

    def _load[R: Record](self, model: type[R], tenant: str, kind: str, key: str) -> R | None:
        data = self._get(tenant, kind, key)
        return model.model_validate_json(data) if data is not None else None

    def _save(self, tenant: str, kind: str, key: str, value: Record) -> None:
        self._put(tenant, kind, key, value.model_dump_json())

    def _require_snapshots(self, *refs: SnapshotRef | None) -> None:
        if any(ref is not None and self.get_snapshot(ref) is None for ref in refs):
            raise SourceConflict("record_unavailable", "retained snapshot missing")

    def _claim(self, tenant: str, category: str, key: str, owner: str) -> None:
        # A row store has no unique constraint across rows, so uniqueness (one
        # source per repository URL, one skill per package, one open update per
        # skill) is a row of its own that the current owner may rewrite and
        # nobody else may take.
        claim = record_key(category, key)
        current = self._get(tenant, "claim", claim)
        if current is not None and current != owner:
            raise SourceConflict(f"{category}_already_owned")
        self._put(tenant, "claim", claim, owner)

    def _unclaim(self, tenant: str, category: str, key: str, owner: str) -> None:
        claim = record_key(category, key)
        if self._get(tenant, "claim", claim) == owner:
            self._delete(tenant, "claim", claim)

    @staticmethod
    def _page[R: Record](rows: list[tuple[str, R]], cursor: str | None, limit: int) -> Page[R]:
        if not 1 <= limit <= 100:
            raise ValueError("Page size must be between 1 and 100")
        selected = [(key, row) for key, row in sorted(rows) if cursor is None or key > cursor]
        return Page(items=tuple(row for _, row in selected[:limit]),
                    next_cursor=selected[limit - 1][0] if len(selected) > limit else None)

    def get_source(self, source_id: str, *, tenant_id: str) -> Source | None:
        return self._load(Source, tenant_id, "source", source_id)

    def source_for_url(self, url: str, *, tenant_id: str) -> Source | None:
        canonical = parse_link(url).repository_url
        owner = self._get(tenant_id, 'claim', record_key('repository_url', canonical))
        if owner is not None:
            found = self.get_source(owner, tenant_id=tenant_id)
            if found is not None:
                return found
        # The claim is the index. The scan behind it covers a claim that points
        # at a removed source; two matches there is an identity nobody resolved,
        # and picking one would bind a skill to the wrong repository.
        matches = [source for _, data in self._rows(tenant_id, 'source')
                 if (source := Source.model_validate_json(data)) is not None
                 and canonical in {parse_link(value).repository_url
                                   for value in (source.canonical_url, *source.confirmed_aliases)}]
        if len(matches) > 1:
            raise SourceConflict('source_already_exists', 'repository identity ambiguous')
        return matches[0] if matches else None

    def active_bindings(self, source_id: str, *, tenant_id: str) -> tuple[Binding, ...]:
        return tuple(sorted((binding for _, data in self._rows(tenant_id, 'binding')
            if (binding := Binding.model_validate_json(data)).active and binding.source_id == source_id),
            key=lambda binding: binding.origin.skill))

    def active_bindings_for_tenant(self, *, tenant_id: str) -> tuple[Binding, ...]:
        # One scan of bindings and one of retirement fences: a list read must not cost a query per skill.
        fences = {key: Generations.model_validate_json(data).binding for key, data in self._rows(tenant_id, "retirement")}
        return tuple(sorted((binding for _, data in self._rows(tenant_id, "binding")
            if (binding := Binding.model_validate_json(data)).active
            and binding.origin.generation > fences.get(binding.origin.skill.skill_id, -1)),
            key=lambda binding: binding.origin.skill))

    @transactional
    def put_source(self, source: Source) -> None:
        old = self.get_source(source.source_id, tenant_id=source.tenant_id)
        # A source may move to a new address only as a deliberate step: the
        # generation advances and both the old and the new URL are confirmed
        # aliases, which is what accepting a redirect produces. Anything else
        # that rewrites the canonical URL is a different source wearing this id.
        if old is not None and old.canonical_url != source.canonical_url:
            if (source.generation <= old.generation
                    or not {old.canonical_url, source.canonical_url}.issubset(source.confirmed_aliases)):
                raise SourceConflict("source_changed", "source identity changed")
        if old is not None and old.generation > source.generation:
            raise StaleMutation("source_changed", "source generation changed")
        # A confirmed alias is never dropped: bindings and package claims were
        # made against it, and a URL that once resolved to this source must
        # keep doing so rather than become free for a second source to claim.
        if old is not None and not set(old.confirmed_aliases).issubset(source.confirmed_aliases):
            raise SourceConflict("source_changed", "confirmed repository identity removed")
        for url in sorted({parse_link(value).repository_url
                           for value in (source.canonical_url, *source.confirmed_aliases)}):
            existing = self.source_for_url(url, tenant_id=source.tenant_id)
            if existing is not None and existing.source_id != source.source_id:
                raise SourceConflict('source_already_exists', 'repository url already owned')
            self._claim(source.tenant_id, 'repository_url', url, source.source_id)
        for _, data in self._rows(source.tenant_id, "binding"):
            binding = Binding.model_validate_json(data)
            if binding.source_id == source.source_id and binding.active:
                for key in self._package_keys_for(source, binding.package_path):
                    self._claim(source.tenant_id, "package", key, binding.origin.skill.skill_id)
        self._save(source.tenant_id, "source", source.source_id, source)

    def sources(self, *, tenant_id: str, skill_scope: tuple[SkillRef, ...],
                cursor: str | None = None, limit: int = 100) -> Page[Source]:
        allowed = {binding.source_id for ref in skill_scope if ref.tenant_id == tenant_id
                   if (binding := self.get_binding(ref)) is not None and binding.active}
        rows = [(key, Source.model_validate_json(data)) for key, data in self._rows(tenant_id, "source")
                if key in allowed]
        return self._page(rows, cursor, limit)

    def get_binding(self, skill: SkillRef) -> Binding | None:
        return self._load(Binding, skill.tenant_id, "binding", skill.skill_id)

    @staticmethod
    def _package_keys_for(source: Source, path: str) -> tuple[str, ...]:
        return tuple(sorted({record_key(parse_link(url).repository_url.casefold().removesuffix(".git"), path)
                             for url in (source.canonical_url, *source.confirmed_aliases)}))

    def _package_keys(self, binding: Binding) -> tuple[str, ...]:
        source = self.get_source(binding.source_id, tenant_id=binding.origin.skill.tenant_id)
        if source is None:
            raise SourceConflict("record_unavailable", "binding source missing")
        target = parse_link(binding.ref.canonical_url).repository_url.casefold().removesuffix(".git")
        if target not in {parse_link(url).repository_url.casefold().removesuffix(".git")
                          for url in (source.canonical_url, *source.confirmed_aliases)}:
            raise SourceConflict("repository_endpoint_changed", "binding repository changed")
        return self._package_keys_for(source, binding.package_path)

    @transactional
    def put_binding(self, binding: Binding) -> None:
        ref = binding.origin.skill
        old = self.get_binding(ref)
        if old is not None and old.origin.generation > binding.origin.generation:
            raise StaleMutation("binding_changed", "binding generation changed")
        # Two writers that both derived the same next generation from the same
        # read cannot both be right; a write that changes what the binding
        # points at without advancing is the second of them. A branch's commit
        # is exempt: it moves on every check without the binding changing.
        if old is not None and old.origin.generation == binding.origin.generation:
            if (old.active != binding.active or old.origin != binding.origin
                    or old.source_id != binding.source_id or old.package_path != binding.package_path
                    or old.ref.kind != binding.ref.kind or old.ref.name != binding.ref.name
                    or (old.ref.kind != "branch" and old.ref.commit != binding.ref.commit)):
                raise StaleMutation("binding_changed", "binding generation changed")
        if binding.baseline is not None and self.get_snapshot(binding.baseline) is None:
            raise SourceConflict("record_unavailable", "binding baseline missing")
        # One skill per package per source, under every confirmed URL of the
        # source: the old claims go first so a binding that moves packages
        # within one write does not collide with itself.
        packages = self._package_keys(binding)
        if old is not None and old.active:
            for package in self._package_keys(old):
                self._unclaim(ref.tenant_id, "package", package, ref.skill_id)
        if binding.active:
            for package in packages:
                self._claim(ref.tenant_id, "package", package, ref.skill_id)
        generations = self.get_generations(ref)
        if binding.origin.generation < generations.binding:
            raise StaleMutation("binding_changed", "binding generation changed")
        if binding.origin.generation > generations.binding:
            self.advance_generations(generations, Generations(skill=ref, content=generations.content,
                                                              binding=binding.origin.generation))
        self._save(ref.tenant_id, "binding", ref.skill_id, binding)

    def bindings(self, source_id: str, *, tenant_id: str, skill_scope: tuple[SkillRef, ...],
                 cursor: str | None = None, limit: int = 100) -> Page[Binding]:
        rows = [(ref.skill_id, binding) for ref in set(skill_scope) if ref.tenant_id == tenant_id
                if (binding := self.get_binding(ref)) is not None
                and binding.source_id == source_id and binding.active]
        return self._page(rows, cursor, limit)

    def get_local_stream(self, skill: SkillRef) -> LocalStream | None:
        return self._load(LocalStream, skill.tenant_id, "local", skill.skill_id)

    @transactional
    def put_local_stream(self, stream: LocalStream) -> None:
        ref = stream.origin.skill
        old = self.get_local_stream(ref)
        if old is not None and old.origin.origin_id != stream.origin.origin_id:
            raise SourceConflict("source_already_exists", "local stream already exists")
        if old is not None and old.origin.generation > stream.origin.generation:
            raise StaleMutation("content_changed", "local generation changed")
        if stream.baseline is not None and self.get_snapshot(stream.baseline) is None:
            raise SourceConflict("record_unavailable", "local baseline missing")
        self._save(ref.tenant_id, "local", ref.skill_id, stream)

    def get_snapshot(self, ref: SnapshotRef) -> SourceSnapshot | None:
        # The id alone is not the identity: a snapshot retained for one origin
        # must not be read back as the base of another skill or binding that
        # happens to present the same id.
        value = self._load(SourceSnapshot, ref.origin.skill.tenant_id, "snapshot", ref.snapshot_id)
        return value if value is not None and value.ref == ref else None

    @transactional
    def put_snapshot(self, snapshot: SourceSnapshot) -> None:
        tenant, key = snapshot.ref.origin.skill.tenant_id, snapshot.ref.snapshot_id
        old = self._load(SourceSnapshot, tenant, "snapshot", key)
        # Snapshots are the evidence a merge, a review and an undo are judged
        # against; rewriting one in place would change what those decisions
        # were about after the fact.
        if old is not None and old != snapshot:
            raise SourceConflict("request_identity_changed", "snapshot is immutable")
        self._save(tenant, "snapshot", key, snapshot)

    def get_update(self, update_id: str, *, tenant_id: str) -> Update | None:
        return self._load(Update, tenant_id, "update", update_id)

    def open_update(self, skill: SkillRef) -> Update | None:
        key = self._get(skill.tenant_id, "claim", record_key("open_update", skill.skill_id))
        return self.get_update(key, tenant_id=skill.tenant_id) if key else None

    @transactional
    def put_update(self, update: Update) -> None:
        ref = update.plan.skill
        self._require_snapshots(update.plan.base, update.plan.incoming)
        old = self.get_update(update.update_id, tenant_id=ref.tenant_id)
        if old is not None and (old.plan.skill != ref or old.generation > update.generation):
            raise StaleMutation("update_changed", "update generation changed")
        if update.generation != update.plan.fingerprint.update_generation:
            raise StaleMutation("update_changed", "update fingerprint changed")
        if old is not None:
            if ((old.plan.origin.kind, old.plan.origin.origin_id, old.review_item_id)
                    != (update.plan.origin.kind, update.plan.origin.origin_id, update.review_item_id)):
                raise SourceConflict("update_changed", "update origin changed")
            if old.status != "open" and old != update:
                raise SourceConflict("update_changed", "update is terminal")
            if old.generation == update.generation and (old.plan != update.plan or old.drafts != update.drafts):
                raise StaleMutation("update_changed", "update generation changed")
        if update.status == "open":
            self._claim(ref.tenant_id, "open_update", ref.skill_id, update.update_id)
        else:
            self._unclaim(ref.tenant_id, "open_update", ref.skill_id, update.update_id)
        self._save(ref.tenant_id, "update", update.update_id, update)

    def get_generations(self, skill: SkillRef) -> Generations:
        # A skill nobody has written through this path yet is at generation
        # zero, so a guard taken before its first tracked write is still valid.
        return self._load(Generations, skill.tenant_id, "generations", skill.skill_id) or Generations(
            skill=skill, content=0, binding=0)

    def generations_for_tenant(self, *, tenant_id: str) -> tuple[Generations, ...]:
        return tuple(Generations.model_validate_json(data) for _, data in self._rows(tenant_id, "generations"))

    def get_retirement(self, skill: SkillRef) -> Generations | None:
        return self._load(Generations, skill.tenant_id, "retirement", skill.skill_id)

    def _ownership_rows(self, skill: SkillRef) -> list[tuple[str, PartOwnership]]:
        # Keys start with the skill's id, so other skills' claims are never parsed.
        prefix = record_key(skill.skill_id)[:-1] + ","
        return [(key, owner) for key, data in self._rows(skill.tenant_id, "ownership", prefix=prefix)
                if (owner := PartOwnership.model_validate_json(data)).skill == skill]

    def ownership_for_skill(self, skill: SkillRef) -> tuple[PartOwnership, ...]:
        return tuple(owner for _, owner in self._ownership_rows(skill))

    @transactional
    def put_ownership(self, ownership: PartOwnership) -> None:
        ref = ownership.skill
        key = record_key(ref.skill_id, ownership.part_id, record_key(*sorted(ownership.entity_ids)),
                         ownership.section_id or "")
        old = self._load(PartOwnership, ref.tenant_id, "ownership", key)
        if old == ownership:
            return
        self._reach_content(ref)
        self._save(ref.tenant_id, "ownership", key, ownership)
        self._advance_ownership_fence(ref)

    @transactional
    def clear_ownership(self, skill: SkillRef, part_id: str, *, entity_ids: tuple[str, ...] | None = None,
                        section_id: str | None = None) -> None:
        removed = False
        for key, owner in self._ownership_rows(skill):
            if (owner.part_id == part_id and (entity_ids is None or
                    (set(owner.entity_ids) == set(entity_ids) and owner.section_id == section_id))):
                self._reach_content(skill)
                self._delete(skill.tenant_id, "ownership", key)
                removed = True
        if removed:
            self._advance_ownership_fence(skill)

    def _reach_content(self, skill: SkillRef) -> None:
        # Ownership is part of the skill's fingerprint: the transaction tracking
        # content snapshots the skill before this write changes it.
        if self._tracks_content():
            from oms.sources.mutation import reach_skills
            reach_skills(skill.tenant_id, skill_ids=(skill.skill_id,))

    def _advance_ownership_fence(self, skill: SkillRef) -> None:
        # Under a content-tracking transaction the tracker advances the skill,
        # since ownership is in its fingerprint; a repository used on its own
        # has to step the generation itself or an ownership change would leave
        # every guard on the skill valid.
        if not self._tracks_content():
            old = self.get_generations(skill)
            self.advance_generations(old, Generations(skill=skill, content=old.content + 1, binding=old.binding))

    @transactional
    def retire_skill(self, skill: SkillRef) -> tuple[Update, ...]:
        """Fence deleted content and close its streams without deleting retained evidence."""
        # The retirement row keeps the last generations under the skill's id.
        # Without it a deleted skill would read as a fresh one at generation
        # zero, and a stale request carrying old guards could recreate it. The
        # binding is deactivated rather than deleted for the same reason the
        # snapshots stay: it is the record of what was installed from where.
        generation = self.get_generations(skill)
        self._save(skill.tenant_id, "retirement", skill.skill_id, Generations(
            skill=skill, content=generation.content, binding=generation.binding + 1))
        binding = self.get_binding(skill)
        if binding is not None:
            self.put_binding(Binding.model_validate(binding.model_dump() | {"active": False,
                "origin": binding.origin.model_dump() | {"generation": generation.binding + 1}}))
        else:
            self.advance_generations(generation, Generations(skill=skill, content=generation.content,
                                                             binding=generation.binding + 1))
        self._delete(skill.tenant_id, "local", skill.skill_id)
        for owner in self.ownership_for_skill(skill):
            self.clear_ownership(skill, owner.part_id)
        for attempt in self.blocked_attempts(skill):
            self.clear_blocked_attempt(attempt.attempt_id, tenant_id=skill.tenant_id)
        update = self.open_update(skill)
        if update is None:
            return ()
        closed = Update.model_validate(update.model_dump() | {"status": "closed"})
        self.put_update(closed)
        return (closed,)

    def lock_skills(self, skills: tuple[SkillRef, ...]) -> None:
        # The Neo4j adapter takes row locks in this order so two transactions
        # locking overlapping sets cannot deadlock. The contract is checked
        # here, in the base, so the memory adapter fails the same tests.
        if skills != tuple(sorted(set(skills))):
            raise ValueError("Skill locks must be sorted and unique")

    def compare_generations(self, expected: tuple[Generations, ...]) -> None:
        for generation in expected:
            actual = self.get_generations(generation.skill)
            if actual.content != generation.content:
                raise StaleMutation("content_changed", "content generation changed")
            if actual.binding != generation.binding:
                raise StaleMutation("binding_changed", "binding generation changed")

    @transactional
    def advance_generations(self, expected: Generations, replacement: Generations) -> None:
        self.compare_generations((expected,))
        if (expected.skill != replacement.skill or replacement.content < expected.content
                or replacement.binding < expected.binding or replacement == expected):
            raise ValueError("Generations must advance monotonically")
        self._save(expected.skill.tenant_id, "generations", expected.skill.skill_id, replacement)

    def get_operation(self, operation_id: str, *, tenant_id: str) -> Operation | None:
        return self._load(Operation, tenant_id, "operation", operation_id)

    def operation_for_key(self, idempotency_key: str, *, tenant_id: str, actor_id: str) -> Operation | None:
        key = self._get(tenant_id, "operation_key", record_key(actor_id, idempotency_key))
        return self.get_operation(key, tenant_id=tenant_id) if key is not None else None

    @transactional
    def reserve_operation(self, operation: Operation) -> Operation:
        context = operation.context
        old = self.operation_for_key(operation.idempotency_key, tenant_id=context.tenant_id,
                                     actor_id=context.actor_id)
        # The key is scoped to the actor: a retry of the same request gets the
        # operation already reserved for it; the same key on a different
        # request is a client error, not a second operation.
        if old is not None:
            if old.request_digest != operation.request_digest:
                raise SourceConflict("idempotency_key_reused")
            return old
        if self.get_operation(operation.operation_id, tenant_id=context.tenant_id) is not None:
            raise SourceConflict("request_identity_changed", "operation id reused")
        self._put(context.tenant_id, "operation_key", record_key(context.actor_id, operation.idempotency_key),
                  operation.operation_id)
        self._save(context.tenant_id, "operation", operation.operation_id, operation)
        return operation

    @transactional
    def put_operation(self, operation: Operation) -> None:
        old = self.get_operation(operation.operation_id, tenant_id=operation.context.tenant_id)
        if old is None:
            self.reserve_operation(operation)
            return
        if (old.context != operation.context or old.idempotency_key != operation.idempotency_key
                or old.request_digest != operation.request_digest or old.scope_proofs != operation.scope_proofs
                or old.upload_replay != operation.upload_replay):
            raise SourceConflict("request_identity_changed", "operation identity changed")
        if old.batch_members and old.batch_members != operation.batch_members:
            raise SourceConflict("batch_changed", "batch members changed")
        if old.result.state in {"complete", "blocked", "failed", "awaiting_review"} and old != operation:
            raise SourceConflict("operation_no_longer_active", "operation is terminal")
        self._save(operation.context.tenant_id, "operation", operation.operation_id, operation)

    @transactional
    def put_blocked_attempt(self, attempt: BlockedAttempt) -> None:
        self._save(attempt.origin.skill.tenant_id, "blocked", attempt.attempt_id, attempt)

    def blocked_attempts(self, skill: SkillRef) -> tuple[BlockedAttempt, ...]:
        return tuple(attempt for _, data in self._rows(skill.tenant_id, "blocked")
                     if (attempt := BlockedAttempt.model_validate_json(data)).origin.skill == skill)

    @transactional
    def clear_blocked_attempt(self, attempt_id: str, *, tenant_id: str) -> None:
        self._delete(tenant_id, "blocked", attempt_id)

    def get_undo(self, undo_id: str, *, tenant_id: str) -> UndoRecord | None:
        return self._load(UndoRecord, tenant_id, "undo", undo_id)

    @transactional
    def put_undo(self, undo: UndoRecord) -> None:
        self._require_snapshots(undo.previous_base, undo.resulting_base)
        old = self.get_undo(undo.undo_id, tenant_id=undo.origin.skill.tenant_id)
        if old is not None and old.model_dump(exclude={"undone_by_operation_id"}) != undo.model_dump(
                exclude={"undone_by_operation_id"}):
            raise SourceConflict("request_identity_changed", "undo record is immutable")
        if old is not None and old.undone_by_operation_id and old != undo:
            raise SourceConflict("undo_already_used", "undo already consumed")
        self._save(undo.origin.skill.tenant_id, "undo", undo.undo_id, undo)

    @transactional
    def claim_lease(self, lease: Lease, *, now: datetime) -> bool:
        # The fencing token outlives the lease. A holder that lost its lease
        # to expiry can only come back with a higher token, so a worker that
        # stalled and woke up cannot resume on the strength of its old claim.
        if lease.expires_at <= now:
            return False
        old = self._load(Lease, lease.tenant_id, "lease", lease.resource_id)
        if old is not None:
            if old.expires_at > now:
                return old == lease
            if lease.fencing_token <= old.fencing_token:
                return False
        self._save(lease.tenant_id, "lease", lease.resource_id, lease)
        return True

    def get_lease(self, resource_id: str, *, tenant_id: str) -> Lease | None:
        return self._load(Lease, tenant_id, "lease", resource_id)

    @transactional
    def release_lease(self, lease: Lease) -> None:
        old = self._load(Lease, lease.tenant_id, "lease", lease.resource_id)
        # Released by expiring the row, not deleting it, so the fencing token
        # stays on record and the next claim still has to exceed it.
        if old == lease:
            expired = Lease.model_validate(lease.model_dump()
                                           | {"expires_at": datetime.min.replace(tzinfo=lease.expires_at.tzinfo)})
            self._save(lease.tenant_id, "lease", lease.resource_id, expired)

    @transactional
    def append_event(self, event: DurableEvent) -> None:
        old = self._load(DurableEvent, event.tenant_id, "event", event.event_id)
        if old is not None and old != event:
            raise SourceConflict("request_identity_changed", "event identity reused")
        self._save(event.tenant_id, "event", event.event_id, event)

    def get_event(self, event_id: str, *, tenant_id: str) -> DurableEvent | None:
        return self._load(DurableEvent, tenant_id, "event", event_id)

    def pending_events(self, *, tenant_id: str, now: datetime, limit: int = 100) -> tuple[DurableEvent, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Event page size must be between 1 and 100")
        return tuple(event for _, data in self._rows(tenant_id, "event")
                     if (event := DurableEvent.model_validate_json(data)).state == "pending"
                     and (event.next_attempt_at is None or event.next_attempt_at <= now))[:limit]

    @transactional
    def put_event(self, event: DurableEvent) -> None:
        old = self._load(DurableEvent, event.tenant_id, "event", event.event_id)
        if old is None or (old.operation_id, old.action, old.skills, old.created_at, old.actor_id,
                           old.notification_eligible) != (
                event.operation_id, event.action, event.skills, event.created_at, event.actor_id,
                event.notification_eligible):
            raise SourceConflict("request_identity_changed", "event identity changed")
        if event.attempts < old.attempts or (old.state in {"delivered", "failed"} and old != event):
            raise SourceConflict("operation_no_longer_active", "event delivery is terminal")
        self._save(event.tenant_id, "event", event.event_id, event)

    def get_discovery(self, discovery_id: str, *, tenant_id: str, actor_id: str,
                      now: datetime) -> DiscoveryResult | None:
        # A discovery lists what one actor's credential could see at one
        # moment. It is read back only by that actor and only until it expires,
        # so nobody installs from a listing made with somebody else's access.
        row = self._load(DiscoveryResult, tenant_id, "discovery", discovery_id)
        return row if row is not None and row.actor_id == actor_id and row.expires_at > now else None

    @transactional
    def put_discovery(self, discovery: DiscoveryResult) -> None:
        old = self._load(DiscoveryResult, discovery.tenant_id, "discovery", discovery.discovery_id)
        if old is not None and old != discovery:
            raise SourceConflict("request_identity_changed", "discovery is immutable")
        self._save(discovery.tenant_id, "discovery", discovery.discovery_id, discovery)

    @transactional
    def delete_discovery(self, discovery_id: str, *, tenant_id: str, actor_id: str) -> None:
        old = self._load(DiscoveryResult, tenant_id, "discovery", discovery_id)
        if old is not None and old.actor_id == actor_id:
            self._delete(tenant_id, "discovery", discovery_id)

    @transactional
    def claim_manual_check(self, throttle: CheckThrottle, *, now: datetime) -> bool:
        old = self._load(CheckThrottle, throttle.tenant_id, "throttle", throttle.source_id)
        if old is not None and old.next_allowed_at > now:
            return False
        if throttle.next_allowed_at <= now:
            raise ValueError("Check throttle must expire in the future")
        self._save(throttle.tenant_id, "throttle", throttle.source_id, throttle)
        return True

    def get_manual_check(self, source_id: str, *, tenant_id: str) -> CheckThrottle | None:
        return self._load(CheckThrottle, tenant_id, "throttle", source_id)

    # Keyed by binding generation as well as revision: an undo suppresses the
    # revision it reverted for this binding only, and a relink, which advances
    # the generation, may offer the same upstream revision again (spec §8.5).
    @transactional
    def suppress_candidate(self, origin: OriginRef, revision: str) -> None:
        self._put(origin.skill.tenant_id, "suppressed", record_key(
            origin.skill.skill_id, origin.origin_id, str(origin.generation), revision), "true")

    def candidate_suppressed(self, origin: OriginRef, revision: str) -> bool:
        return self._get(origin.skill.tenant_id, "suppressed", record_key(
            origin.skill.skill_id, origin.origin_id, str(origin.generation), revision)) is not None
