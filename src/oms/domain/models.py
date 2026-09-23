from dataclasses import dataclass, field
from datetime import datetime, timezone

from oms.domain.auth import INGEST_AUTOWRITE, INGEST_WRITE
from oms.domain.types import (
    SignalType, SourceRuntime, RuleStatus, BlockStatus, SkillStatus, SkillOrigin,
    Vector, EdgeType,
    Verdict, Polarity, Plane, ExampleKind, SectionKind, ArtefactKind, Mutability,
    SkillVersionCause, ConstraintStatus, ConstraintSource,
)
from oms.domain.auth import Assurance


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Rule:
    id: str
    body: str
    tenant_id: str
    embedding: Vector | None = None
    status: RuleStatus = RuleStatus.ACTIVE
    corroboration_count: int = 1
    reference_only: bool = False
    polarity: Polarity = Polarity.PRESCRIBE
    # Publication reads the category independently of the rule's status.
    plane: Plane = Plane.DATA
    created_at: datetime = field(default_factory=_now)


@dataclass
class Skill:
    id: str
    name: str
    description: str
    domain: str
    tenant_id: str
    status: SkillStatus = SkillStatus.ACTIVE
    # Field names an administrator set by hand: any of "name", "description",
    # "domain", "tags". Import treats a skill's source file as authority for
    # everything except these, so an edit made in the console survives the next
    # re-import instead of being silently reverted (and, for tags, deleted -
    # `SkillImporter._import_tags` diffs against the file).
    #
    # A set rather than a timestamp or a per-field author: the only question
    # import needs answered is "did a human choose this", and recording who and
    # when is what the AdminEvent stream is for.
    curated: frozenset[str] = frozenset()
    # Defaulting to IMPORTED rather than to a null or a fourth "unknown" value.
    # Every skill in an existing graph predates this field, most of them came
    # from a package, and a badge that reads "OMS wrote this" must never appear
    # over a skill a person wrote. When the default is wrong it is wrong in the
    # direction that claims nothing.
    origin: SkillOrigin = SkillOrigin.IMPORTED
    # The normalised git remote this skill's rules are about, when it is a repo
    # bundle rather than an ordinary skill. None on every skill that is not one.
    #
    # Carried for display and for querying. It is deliberately NOT what decides
    # whether the skill publishes: see `oms.domain.repo.is_repo_skill`, which
    # reads the id. A nullable field whose default is the publishable answer,
    # in a store whose adapter names every field by hand, is one forgotten line
    # away from putting a repo's private rules in everybody's global file.
    repo: str | None = None
    # Workspace-wide publication choice, independent of review status and
    # personal overlays. Imports must preserve the operator's choice.
    publish_enabled: bool = True
    # Source custody is independent of the display name and the frontmatter
    # name, which is only unique within its original package.
    import_source_ref: str | None = None
    import_name: str | None = None


@dataclass
class Learning:
    id: str
    body: str
    source_transaction: str
    tenant_id: str
    embedding: Vector | None = None


@dataclass
class Constraint:
    id: str
    body: str
    tenant_id: str
    immutable: bool = True
    polarity: Polarity = Polarity.PRESCRIBE
    # Both default to what every constraint already in a graph is, so no
    # migration is needed: a node written before these fields existed carries
    # neither property, and both adapters read a missing one as the default.
    # An existing constraint therefore keeps binding, and keeps saying it came
    # from the source tree, which is true of every one of them.
    status: ConstraintStatus = ConstraintStatus.ACTIVE
    source: ConstraintSource = ConstraintSource.IMPORT


@dataclass
class Tag:
    id: str
    name: str


@dataclass
class Transaction:
    id: str
    signal_type: SignalType
    source_runtime: SourceRuntime
    sanitised_payload_ref: str
    timestamp: datetime
    tenant_id: str
    source_ref: str | None = None
    skill_hint: str | None = None     # skill the posting agent says this learning refers to
    principal_id: str | None = None   # authoritative writer identity from the auth layer (Section 8.3)
    source_agent_id: str | None = None  # the agent the payload claimed; kept for audit, not authority
    summary: str | None = None        # short sanitised snippet of the correction, for display and search
    held_reason: str | None = None    # set when compilation is held pending review (e.g. the
                                      # sanitiser altered the correction); held transactions are
                                      # excluded from pending_transactions until released
    person_id: str | None = None        # proven at ingest; None = unbound (§8)
    assurance: Assurance | None = None  # how well that proof was established
    admitted: bool = False              # a human admitted this unattributed transaction at
                                        # review (§9.2): the compile gate stops parking it,
                                        # but person_id stays None. Admission is a decision
                                        # about the work, not a claim about who wrote it,
                                        # and nothing is back-filled (§12)
    # Submitter-supplied confidence, retained as evidence rather than authority.
    signal_confidence: float | None = None
    # Normalised repository context reported by the submitter.
    repo: str | None = None
    # Persist an explicit scope-review decision independently of admission.
    scope_reviewed: bool = False
    # Independent of safety/identity holds. Remains in the pending pool so
    # the ordinary worker can resume it after renewal, without a mass reset.
    licence_hold: str | None = None
    # Additive edition-neutral workflow, retained alongside legacy compile flags.
    workflow_state: str | None = None
    workflow_decision: str | None = None
    workflow_rule_id: str | None = None
    workflow_safety_digest: str | None = None


@dataclass(frozen=True)
class Principal:
    """The authenticated writer behind an ingestion call (Section 8.1). Resolved
    from a credential, never trusted from the payload."""
    id: str
    tenant_id: str
    scopes: frozenset[str] = frozenset()
    display_name: str | None = None
    person_id: str | None = None        # None = unbound: resolvable transport,
                                        # unattributed human (identity spec §5)
    assurance: Assurance | None = None  # how well the binding was proven


def unbound_principal(tenant_id: str) -> Principal:
    """A caller whose credential is absent, unknown or revoked (§8.1).

    Deliberately not a rejection. A lapsed enrolment must never silently
    destroy a learning, and unbound is already the lowest privilege, so
    refusing the call gains nothing. It carries INGEST_WRITE only, because a
    caller that cannot pass the scope check never reaches the trust decision.
    """
    return Principal(id="unbound", tenant_id=tenant_id,
                     scopes=frozenset({INGEST_WRITE}))






@dataclass
class Edge:
    type: EdgeType
    from_id: str
    to_id: str
    properties: dict = field(default_factory=dict)


@dataclass
class Example:
    id: str
    body: str
    kind: ExampleKind
    tenant_id: str
    name: str | None = None              # heading-derived: "Pricing Page Test" from `### Example: ...`
    original_label: str | None = None    # authorial marker: "Strong", "Sample Input", "Before"
    parent_section_id: str | None = None # DEFAULT attachment
    parent_rule_id: str | None = None    # OVERRIDE: explicit per-rule binding
    parent_skill_id: str | None = None   # FALLBACK: orphans before any section
    source_ref: str | None = None
    order: int | None = None             # authorial sequence within the parent section
    created_at: datetime = field(default_factory=_now)


@dataclass
class ReviewItem:
    id: str
    kind: str               # "conflict" | "duplicate_low_confidence" | "sanitisation" | "block_revision"
                            # | "new_skill" | "skill_assignment" | "held_transaction" | "block_conflict"
                            # | "injection" | "control_plane" (safety holds)
    subject_id: str         # the incoming rule (or, for block_revision, the block proposed against)
    other_id: str | None    # the existing rule / constraint (or, for block_revision, the section_id)
    verdict: Verdict
    reason: str
    tenant_id: str
    resolved: bool = False
    proposed_body: str | None = None  # block_revision: the proposed replacement body
    rule_id: str | None = None        # block_revision: the companion rule absorbed on approval
    transaction_id: str | None = None # block_revision: originating transaction (DERIVED_FROM on approval)
    resolution: str | None = None     # "approved" / "rejected" / "assigned" /
                                      # "keep_subject" / "keep_counterpart" / "keep_both" once resolved
    candidate_skill_ids: list[str] | None = None  # candidate skills for new_skill / skill_assignment items
    # The document revision the edit was made against, for `proposed_edit`
    # only. The portal write route refuses a stale edit in milliseconds; this
    # is the same promise kept across the days an item may wait in the queue.
    # None on every other kind, and on an item written before this field
    # existed, which reads as "cannot tell" and is handled at the approval.
    base_revision: str | None = None
    # Optional display grouping copied from a session source_ref at enqueue.
    # Grouped items retain separate transactions and decisions.
    group_id: str | None = None
    priority: str = "normal"          # "low" | "normal" | "high"; new_skill and skill_assignment queue low
    created_at: datetime = field(default_factory=_now)
    # Who decided, and when. `decided_by` is a person_id, and None carries the
    # same meaning it does on `Transaction.person_id`: nobody was proven. That
    # happens on a deployment with no reviewer gate configured, and when the
    # operator's break-glass token was used instead of a person's credential -
    # a shared secret cannot name a human, and recording one it cannot know
    # would be worse than recording nothing. `decided_at` is set either way,
    # so "when" survives even where "who" does not.
    decided_by: str | None = None
    decided_at: datetime | None = None


@dataclass
class Section:
    """A `##` block of a SKILL.md. Section kind determines mutability and merge behaviour."""
    id: str
    skill_id: str
    kind: SectionKind
    heading: str
    order: int
    mutability: Mutability
    tenant_id: str
    created_at: datetime = field(default_factory=_now)


@dataclass
class RulePlacement:
    """A rule's authorial position within one section (carried on the
    CONTAINS_RULE relationship): its sequence in the source file and the
    `###` subsection heading it sat under, so publish can reconstruct the
    author's structure. Rules added later by the compiler have no placement
    and render after the placed ones."""
    rule_id: str
    order: int | None = None
    group: str | None = None


@dataclass
class ContentBlock:
    """The byte-stable body of an authorial section.

    The text lives here, on the node. `content_ref` is the sha256 of that text:
    block ids and the supersession chain are keyed on it, so it must stay in
    step with `body`, but it is not a pointer into storage. (Artefacts are the
    other way round - their bytes are in the BlobStore, because a script or an
    image cannot sit on a node.)
    """
    id: str
    content_ref: str         # "sha256-<hex>" of body
    kind: SectionKind        # mirrors parent section's kind
    tenant_id: str
    source_ref: str          # e.g. "skills/seo-audit/SKILL.md#output-format"
    body: str                # the section's text; a block without text is invalid
    status: BlockStatus = BlockStatus.ACTIVE
    created_at: datetime = field(default_factory=_now)


@dataclass
class Artefact:
    """A file alongside SKILL.md: script, reference markdown, README, fixture, etc."""
    id: str
    content_ref: str
    kind: ArtefactKind
    name: str                # filename
    size: int                # bytes
    tenant_id: str
    source_ref: str          # path including the artefact dir
    created_at: datetime = field(default_factory=_now)


@dataclass
class Publication:
    """A record of one file the publisher wrote, used by re-import to short-circuit on no-op."""
    id: str                  # "publication-{tenant}-{slug(source_ref)}"
    skill_id: str
    source_ref: str          # path under the published root
    content_hash: str        # sha256 hex of bytes the publisher wrote
    published_at: datetime
    tenant_id: str


@dataclass
class UsageEvent:
    """One observed fetch of a skill (spec §7.5.1, "selected / fetched").

    Tier 2 records one per `query_skill` / `query_skill_resource` call over MCP,
    which is why consumption on that tier is telemetry for free. Applied-frequency
    is the count of these over a window; it feeds rule ranking, and a skill-level
    dormancy report once Tier 1 hosts report skill loads (spec §7.5.1)."""
    id: str
    skill_id: str
    tenant_id: str
    timestamp: datetime
    source_runtime: SourceRuntime
    principal_id: str | None = None
    resource: str | None = None   # None = the SKILL.md body; else the Level 3 resource fetched


@dataclass
class PublishBlock:
    """The `block_publish` flag (eval re-architecture): quality/safety concerns
    do not run at publish. When the compile-step safety gate or the periodic
    check finds unsafe or dodgy content it raises this flag; the publish step
    only reads it and refuses while it is set. Absence means publishing is
    allowed. The publish step never inspects content itself."""
    tenant_id: str
    reasons: list[str]
    source: str              # what raised it: "compile" | "periodic" | "sweep"
    set_at: datetime


@dataclass
class TenantVocabulary:
    """Per-tenant mapping from organisation words to universal concepts. Default-seeded; learnable."""
    tenant_id: str
    version: int
    body: dict
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


@dataclass
class SkillUploadRecord:
    """One skill package a person has offered, waiting on an administrator.

    Deliberately not a `ReviewItem`. That type is shaped around a single rule:
    `subject_id` is a rule id, `other_id` is the rule or constraint it clashes
    with, and `verdict` grades one sentence. An upload is a directory that may
    create dozens of rules across several skills, add sections and carry binary
    artefacts, so every one of those fields would have to be left empty or bent
    out of meaning. Two queues, both rendered in one console, is the honest
    model.

    The record holds no archive bytes. `archive_ref` names them in the
    `BlobStore` instead, because a pending upload may wait days while
    `UploadService` keeps its staged uploads in a plain dict and its extracted
    files under a staging directory, and both of those die with the process.
    The staging directory is therefore a cache that may be swept at any time,
    and this record plus the blob it names is the pair that has to survive a
    restart.
    """
    id: str
    tenant_id: str
    # The uploader, and never None. `ReviewItem.decided_by` and
    # `Transaction.person_id` may be None because a shared secret cannot name a
    # human, so an act performed through one is honestly unattributed. Nothing
    # of the kind can happen here: an upload record is only ever created behind
    # a guard that has already resolved a named, active person in this tenant,
    # so a None in this field would mean a bug on that path rather than an
    # unattributed act, and it would silently break the "my uploads" filter,
    # which is the one thing keeping one person's uploads away from another's.
    person_id: str
    filename: str           # as the uploader sent it, so the queue is readable
    archive_ref: str        # BlobStore content_ref, "sha256-<hex>"
    status: str             # "pending" | "approved" | "rejected"
    skill_ids: list[str]    # what the archive claims to touch, for the queue list
    diff_summary: str       # JSON snapshot of the SkillDiff list, for audit
    # What staging reported to the uploader: sanitiser redactions, curated
    # fields an apply would leave alone, and anything else the diff could not
    # express. Kept with the record because the administrator is being asked to
    # decide on the same evidence the uploader was shown.
    warnings: list[str]
    created_at: datetime
    # Who decided, and when. `decided_by` is optional for exactly the reason it
    # is optional on `ReviewItem`: an administrator acting through the
    # `OMS_ADMIN_TOKEN` break-glass presents a shared secret, a shared secret
    # names no human, and inventing a name would be worse than recording
    # nothing. `decided_at` is stamped either way, so "when" survives even
    # where "who" does not. `reason` carries the administrator's own words back
    # to a rejected uploader, and it is the only part of the decision the
    # portal shows them.
    decided_by: str | None = None
    decided_at: datetime | None = None
    reason: str | None = None

@dataclass(frozen=True)
class QuarantinedPayload:
    """A contribution the sanitiser refused, recorded so it is not simply lost.

    Sanitisation failing is the third way work leaves the pipeline without
    anyone being told - `mark_compile_failed` and a review hold are the other
    two, and the console shows both. This one writes the exception type to a text file
    under `_flagged_errors/` and answered the caller 422. Nothing queryable
    existed, so the health strip could not list it, the activity feed never
    knew, and the person who sent it saw an error while an administrator saw
    nothing at all.

    Metadata ONLY, and deliberately so. The reason this payload is quarantined
    is that it could not be scrubbed, so it may carry exactly the PII the
    sanitiser exists to remove; putting it in the graph would spread what the
    refusal was meant to contain. `reason` is the sanitiser's own exception
    text, never the payload, and the raw bytes stay in the quarantine file
    where an operator with filesystem access - and a reason to look - can
    still find them.
    """
    transaction_id: str
    tenant_id: str
    reason: str                     # the sanitiser's exception, never the payload
    quarantined_at: datetime
    principal_id: str | None = None  # who sent it, where the door proved anybody


@dataclass(frozen=True)
class RulePage:
    """One window onto a tenant's rules, and the size of the whole match.

    `total` is what the filter matched BEFORE the window, and it is the reason
    this is a page rather than a list. The inventory route used to read the
    newest 2,000 rules and filter them in Python, which is sound at a few
    hundred rules and silently wrong past that: `acme` holds 5,437, so 3,437 of
    them - every rule older than the 2,000th - could not be listed, searched or
    retracted. A filter that reports "no matches" for a rule that exists is
    worse than a slow one.

    So the filters moved into the store, where they run against every row, and
    the window moved to `offset`/`limit`. `total` is what lets a screen say
    which slice of what it is showing.
    """
    items: list[Rule]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True)
class SkillChange:
    """A recent change summary, without loading a skill's snapshot documents."""
    id: str
    skill_id: str
    skill_name: str
    at: datetime
    revision: str
    cause: SkillVersionCause
    actor_person_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class SkillVersion:
    """One snapshot of a whole skill: the rendered document's parts, the
    metadata, and the full rule set (retired rules included, because the
    rendered document holds only publishing rules and "bring back the rule
    that was dropped" is the point of history).

    JSON strings rather than nested dataclasses: written once, read back only
    to display and compare, and a string keeps the Neo4j node flat.
    `actor_person_id` carries the same None-means-unproven rule as
    `AdminEvent`."""
    id: str
    skill_id: str
    tenant_id: str
    at: datetime
    revision: str                     # document_revision(parts)
    cause: SkillVersionCause
    actor_person_id: str | None = None
    detail: str | None = None
    group_id: str | None = None       # ties one admin save's writes together
    parts_json: str = "[]"
    metadata_json: str = "{}"
    rules_json: str = "[]"
