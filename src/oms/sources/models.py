"""The records the source feature stores and returns: snapshots, plans, bindings, updates, operations and
their outcomes. Credentials are never part of any of them."""
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from oms.domain.identity import SkillRef
from oms.sources.primitives import (
    AcquiredPackage, DiscoveryRequest, DiscoveryResult, DiscoveredPackage, Evidence, Generation, Generations,
    JSONValue, LocalPackage, ManifestEntry, NonEmpty, OriginRef, PackageRequest, PartKind, PlanFingerprint, Record,
    RefRequest, ResolvedRef, SnapshotRef,
)
from oms.sources.requests import (
    ApplyRequest, AutomationRequest, BulkApplyRequest, CheckRequest, CreateSourceRequest, DraftChoice, InstallRequest,
    InstallSelection, LinkRequest, LocalBatchRequest, LocalImportRequest, RelocateSourceRequest,
    RemoveSourceRequest, RetargetRequest,
    SaveDraftRequest, ScheduleRequest, UndoRequest, UnlinkRequest, UpdateRequest,
)


class ProjectionPart(Record):
    part_id: NonEmpty
    kind: PartKind
    evidence: Evidence
    order: int = 0
    curated: bool = False
    owner_origins: tuple[str, ...] = ()


class GraphMapping(Record):
    part_id: NonEmpty
    entity_ids: tuple[str, ...]
    owner_skills: tuple[SkillRef, ...] = ()
    section_id: str | None = None
    custody_digest: str | None = None


class PartOwnership(Record):
    skill: SkillRef
    part_id: NonEmpty
    origin_ids: tuple[NonEmpty, ...]
    entity_ids: tuple[NonEmpty, ...]
    proof_digest: NonEmpty
    section_id: str | None = None

    @model_validator(mode="after")
    def complete_proof(self) -> Self:
        if not self.origin_ids or not self.entity_ids:
            raise ValueError("Ownership requires explicit origins and graph identities")
        if len(set(self.origin_ids)) != len(self.origin_ids) or len(set(self.entity_ids)) != len(self.entity_ids):
            raise ValueError("Ownership identities must be unique")
        return self


class SourceSnapshot(Record):
    ref: SnapshotRef
    revision: NonEmpty
    manifest: tuple[ManifestEntry, ...] = ()
    raw_frontmatter: Evidence
    parsed_projection: tuple[ProjectionPart, ...] = ()
    effective_projection: tuple[ProjectionPart, ...] = ()
    graph_mappings: tuple[GraphMapping, ...] = ()
    projection_version: NonEmpty
    policy_version: NonEmpty

    @model_validator(mode="after")
    def identities(self) -> Self:
        paths = [entry.path.casefold() for entry in self.manifest]
        if len(paths) != len(set(paths)):
            raise ValueError("Manifest paths collide")
        for parts in (self.parsed_projection, self.effective_projection, self.graph_mappings):
            ids = [part.part_id for part in parts]
            if len(ids) != len(set(ids)):
                raise ValueError("Projection identities collide")
        if any(owner.tenant_id != self.ref.origin.skill.tenant_id
               for mapping in self.graph_mappings for owner in mapping.owner_skills):
            raise ValueError("Graph mapping crosses tenant ownership")
        return self


class PlanFlag(StrEnum):
    CONFLICT = "conflict"
    UNKNOWN_IDENTITY = "unknown_identity"
    DELETION_CONSENT = "deletion_consent"
    SCRIPT_CHANGES = "script_changes"
    DECLARED_LICENCE_CHANGED = "declared_licence_changed"
    UNPROVEN_HISTORY = "unproven_history"
    REWRITTEN_HISTORY = "rewritten_history"
    FIRST_RECONCILIATION = "first_reconciliation"
    SAFETY_HOLD = "safety_hold"
    REQUIRED_CHECK_UNAVAILABLE = "required_check_unavailable"


class Change(Record):
    part_id: NonEmpty
    kind: PartKind
    action: Literal["keep", "add", "replace", "remove", "conflict"]
    base: Evidence
    local: Evidence
    incoming: Evidence
    linked_removals: tuple[str, ...] = ()


class Conflict(Record):
    part_id: NonEmpty
    reason: NonEmpty
    allowed_choices: tuple[Literal["keep_oms", "use_upstream", "merged_text"], ...]


class MergePlan(Record):
    skill: SkillRef
    origin: OriginRef
    base: SnapshotRef | None
    incoming: SnapshotRef
    fingerprint: PlanFingerprint
    changes: tuple[Change, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    flags: tuple[PlanFlag, ...] = ()

    @model_validator(mode="after")
    def same_origin(self) -> Self:
        if self.skill != self.origin.skill or self.incoming.origin != self.origin:
            raise ValueError("Candidate must belong to the plan's skill and origin")
        if self.base is not None and not self.base.is_base_for(self.origin):
            raise ValueError("Base must belong to the same origin and an earlier or current generation")
        if (self.origin.kind == "github"
                and self.fingerprint.binding_generation != self.origin.generation):
            raise ValueError("Plan fingerprint must match the origin generation")
        return self


class LocalState(Record):
    skill: SkillRef
    content_generation: Generation
    digest: NonEmpty
    document_mode: Literal[0o100644, 0o100755] | None = None
    parts: tuple[ProjectionPart, ...] = ()
    manifest: tuple[ManifestEntry, ...] = ()
    graph_mappings: tuple[GraphMapping, ...] = ()


class CheckResult(Record):
    state: Literal["passed", "held", "unavailable"]
    code: NonEmpty
    reasons: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        raise TypeError("Inspect check.state explicitly")


class ActionContext(Record):
    """Constructed by edition admission; request bodies cannot grant authority."""
    tenant_id: NonEmpty
    actor_id: NonEmpty
    domain_scope: tuple[str, ...]
    capabilities: frozenset[str]
    service: bool = False


class SourceStatus(Record):
    state: Literal["unchecked", "up_to_date", "updates_available", "failed", "missing", "redirect"]
    checked_at: AwareDatetime | None = None
    code: str | None = None


class WorkspaceIdentity(Record):
    tenant_id: NonEmpty
    workspace_id: NonEmpty


class Source(Record):
    source_id: NonEmpty
    tenant_id: NonEmpty
    canonical_url: NonEmpty
    confirmed_aliases: tuple[str, ...] = ()
    discovery_root: str = ""
    credential_profile_id: NonEmpty | None = None
    generation: Generation = 0
    scheduled: bool = False
    status: SourceStatus | None = None


class Binding(Record):
    origin: OriginRef
    source_id: NonEmpty
    package_path: str
    ref: ResolvedRef
    baseline: SnapshotRef | None = None
    automatic_apply: bool = True
    automation_actor_id: str | None = None
    first_reconciliation: bool = True
    status: SourceStatus | None = None
    active: bool = True

    @model_validator(mode="after")
    def github_base(self) -> Self:
        if self.origin.kind != "github":
            raise ValueError("Binding requires a GitHub origin")
        if self.baseline is not None and not self.baseline.is_base_for(self.origin):
            raise ValueError("Binding baseline belongs to another origin or generation")
        return self


class LocalStream(Record):
    origin: OriginRef
    baseline: SnapshotRef | None = None
    transport: Literal["zip", "folder", "cli"] | None = None
    uploader_id: str | None = None
    filename: str | None = None

    @model_validator(mode="after")
    def local_base(self) -> Self:
        if self.origin.kind != "local":
            raise ValueError("Local stream requires a local origin")
        if self.baseline is not None and not self.baseline.is_base_for(self.origin):
            raise ValueError("Local baseline belongs to another origin or generation")
        return self


class Update(Record):
    update_id: NonEmpty
    plan: MergePlan
    generation: Generation
    status: Literal["open", "applied", "skipped", "adopted", "superseded", "closed"]
    review_item_id: NonEmpty
    drafts: tuple[DraftChoice, ...] = ()
    operation_id: str | None = None


class SkillOutcome(Record):
    skill: SkillRef
    state: Literal["applied", "awaiting_review", "blocked", "failed", "unchanged"]
    update_id: str | None = None
    code: str | None = None


class OperationResult(Record):
    operation_id: NonEmpty
    state: Literal["fetching", "planning", "awaiting_review", "applying", "complete", "blocked", "failed"]
    committed: bool
    outcomes: tuple[SkillOutcome, ...] = ()
    source_id: NonEmpty | None = None
    discovery_id: NonEmpty | None = None

    @model_validator(mode="after")
    def committed_state(self) -> Self:
        if self.committed and self.state in {"fetching", "planning", "applying"}:
            raise ValueError("An acquisition or planning operation cannot claim committed content")
        return self


class Lease(Record):
    tenant_id: NonEmpty
    resource_id: NonEmpty
    owner_id: NonEmpty
    run_id: NonEmpty
    fencing_token: Generation
    expires_at: AwareDatetime


class BatchMember(Record):
    child_operation_id: NonEmpty
    skill: SkillRef


class OperationScope(Record):
    skill: SkillRef
    domain: str
    content_generation: Generation


class UploadReplay(Record):
    """What a retry restates once its staged preview, and the bytes it named, are gone."""
    upload_id: NonEmpty
    selections_digest: NonEmpty


class Operation(Record):
    operation_id: NonEmpty
    context: ActionContext
    idempotency_key: NonEmpty
    request_digest: NonEmpty
    result: OperationResult
    lease: Lease | None = None
    history_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    discovery_id: NonEmpty | None = None
    batch_members: tuple[BatchMember, ...] = ()
    scope_proofs: tuple[OperationScope, ...] = ()
    upload_replay: UploadReplay | None = None

    @model_validator(mode="after")
    def scoped_result(self) -> Self:
        if self.operation_id != self.result.operation_id:
            raise ValueError("Operation result identity mismatch")
        if any(item.skill.tenant_id != self.context.tenant_id for item in self.result.outcomes):
            raise ValueError("Operation outcome crosses tenant boundary")
        if self.lease is not None and self.lease.tenant_id != self.context.tenant_id:
            raise ValueError("Operation lease crosses tenant boundary")
        if any(member.skill.tenant_id != self.context.tenant_id for member in self.batch_members):
            raise ValueError("Batch operation crosses tenant boundary")
        if len({member.child_operation_id for member in self.batch_members}) != len(self.batch_members):
            raise ValueError("Batch operation members must be unique")
        if any(member.child_operation_id == self.operation_id for member in self.batch_members):
            raise ValueError("Batch operation cannot contain itself")
        if any(proof.skill.tenant_id != self.context.tenant_id for proof in self.scope_proofs):
            raise ValueError("Operation scope crosses tenant boundary")
        if len({proof.skill for proof in self.scope_proofs}) != len(self.scope_proofs):
            raise ValueError("Operation scope must be unique")
        return self


class BlockedAttempt(Record):
    attempt_id: NonEmpty
    origin: OriginRef
    actor_id: NonEmpty
    reason: NonEmpty
    existing_update_id: NonEmpty
    retry_needed: bool = True
    action: NonEmpty = "check"


class DurableEvent(Record):
    event_id: NonEmpty
    tenant_id: NonEmpty
    operation_id: NonEmpty
    action: NonEmpty
    skills: tuple[SkillRef, ...]
    state: Literal["pending", "delivered", "failed"] = "pending"
    attempts: Annotated[int, Field(ge=0)] = 0
    next_attempt_at: AwareDatetime | None = None
    error_code: str | None = None
    created_at: AwareDatetime | None = None
    actor_id: str | None = None
    # Legacy events remain audit-only; delivery requires an explicit paid decision.
    notification_eligible: bool = False

    @model_validator(mode="after")
    def scoped_skills(self) -> Self:
        if len(set(self.skills)) != len(self.skills):
            raise ValueError("Event skill scope must be unique")
        if any(skill.tenant_id != self.tenant_id for skill in self.skills):
            raise ValueError("Event skill scope crosses tenant boundary")
        return self


class UndoWrite(Record):
    skill: SkillRef
    part_id: NonEmpty
    kind: PartKind
    before: Evidence
    after: Evidence


class UndoCustody(Record):
    skill: SkillRef
    part_id: NonEmpty
    before: Evidence
    after: Evidence


class UndoEdge(Record):
    type: Literal["SUPERSEDES"] = "SUPERSEDES"
    from_id: NonEmpty
    to_id: NonEmpty
    before: bool
    after: bool


class UndoRecord(Record):
    undo_id: NonEmpty
    operation_id: NonEmpty
    origin: OriginRef
    previous_base: SnapshotRef | None
    resulting_base: SnapshotRef
    writes: tuple[UndoWrite, ...]
    expected_generations: tuple[Generations, ...]
    policy_version: NonEmpty
    update_id: NonEmpty | None = None
    policy_digest: NonEmpty | None = None
    previous_first_reconciliation: bool | None = None
    previous_ownership: tuple[PartOwnership, ...] = ()
    resulting_ownership: tuple[PartOwnership, ...] = ()
    previous_mappings: tuple[GraphMapping, ...] = ()
    resulting_mappings: tuple[GraphMapping, ...] = ()
    custody: tuple[UndoCustody, ...] = ()
    edges: tuple[UndoEdge, ...] = ()
    undone_by_operation_id: str | None = None

    @model_validator(mode="after")
    def guarded_origin(self) -> Self:
        if self.previous_base is not None and not self.previous_base.is_base_for(self.origin):
            raise ValueError("Previous undo baseline belongs to another origin or future generation")
        if self.resulting_base.origin != self.origin:
            raise ValueError("Resulting undo baseline must match the current origin")
        guarded = [guard.skill for guard in self.expected_generations]
        if len(set(guarded)) != len(guarded):
            raise ValueError("Undo generation guards must be unique")
        claims = (*self.previous_ownership, *self.resulting_ownership)
        affected = {self.origin.skill, *(write.skill for write in self.writes),
                    *(claim.skill for claim in claims), *(item.skill for item in self.custody)}
        if len({(item.skill, item.part_id) for item in self.custody}) != len(self.custody):
            raise ValueError("Undo custody units must be unique")
        if len({(edge.type, edge.from_id, edge.to_id) for edge in self.edges}) != len(self.edges):
            raise ValueError("Undo edges must be unique")
        if any(skill.tenant_id != self.origin.skill.tenant_id for skill in affected | set(guarded)):
            raise ValueError("Undo crosses tenant boundary")
        if not affected.issubset(guarded):
            raise ValueError("Every affected undo skill requires a generation guard")
        if any(owner.tenant_id != self.origin.skill.tenant_id
               for mapping in (*self.previous_mappings, *self.resulting_mappings)
               for owner in mapping.owner_skills):
            raise ValueError("Undo graph ownership crosses tenant boundary")
        return self


class UndoStatus(Record):
    undo_id: NonEmpty
    update_id: NonEmpty
    skill: SkillRef
    expected_generations: tuple[Generations, ...]
    policy_version: NonEmpty
    available: bool
    reasons: tuple[str, ...] = ()


class CheckThrottle(Record):
    tenant_id: NonEmpty
    source_id: NonEmpty
    actor_id: NonEmpty
    next_allowed_at: AwareDatetime


class Page[T](Record):
    items: tuple[T, ...]
    next_cursor: str | None = None
