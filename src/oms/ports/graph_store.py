from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from oms.domain.models import (
    Rule, Skill, Transaction, Edge, Learning, Constraint, Example,
    Section, ContentBlock, Artefact, Publication, PublishBlock, QuarantinedPayload,
    RulePage, RulePlacement, UsageEvent, SkillVersion, SkillChange,
)
from oms.domain.types import (CompileStatus, ConstraintStatus, EdgeType,
                              RuleStatus)


class SectionMutabilityError(Exception):
    """Raised when an op targets a section whose mutability doesn't permit it."""


#: The transaction fields `clear_transaction_context` removes: the snippet of
#: the contribution and the session context, all of them the contributor's
#: agent's own words. Named once so both adapters clear the same set.
TRANSACTION_CONTEXT_FIELDS = (
    "summary", "session_summary", "project_name", "reuse_case", "learning_evidence")


@dataclass(frozen=True)
class RuleContext:
    """What a rule is about and who started it, for a caller rendering a list.

    Assembled for the activity feed, which had none of it: a row said what
    happened without saying which skill it touched, which area of the business
    that skill governs, or who originally raised the correction being decided.
    A reader could see that somebody approved something and learn nothing else.

    `skills` and `domains` are NAMES, not ids, because every consumer is
    display. `contributor_person_id` is the person on the OLDEST transaction
    the rule was derived from - the correction it was born from, not the most
    recent one that corroborated it - and None where nobody was proven, which
    carries the same meaning it does everywhere else in this codebase.
    """
    rule_id: str
    skills: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    contributor_person_id: str | None = None


@runtime_checkable
class GraphStore(Protocol):
    def graph_node(self, node_id: str, tenant_id: str) -> dict | None:
        """A public core graph node, only when visible to this workspace."""
        ...

    def graph_neighbours(self, node_id: str, tenant_id: str, *, offset: int = 0, limit: int = 100) -> dict:
        """All incident core relationships, paged by neighbouring node id."""
        ...

    def workflow_state_for(self, transaction_id: str) -> str: ...
    # structural traversal: the primary retrieval path
    def upsert_rule(self, rule: Rule) -> None: ...
    def create_rule_if_absent(self, rule: Rule) -> None:
        """Atomically create a rule while preserving any existing record."""
        ...
    def get_rule(self, rule_id: str) -> Rule | None: ...
    def upsert_transaction(self, transaction: Transaction) -> None: ...
    def get_transaction(self, transaction_id: str) -> Transaction | None: ...
    def upsert_skill(self, skill: Skill) -> None: ...
    def get_skill(self, skill_id: str) -> Skill | None: ...
    def delete_skill(self, skill_id: str) -> None:
        """Remove the skill node and every edge touching it; no-op when absent.
        Callers own the safety checks (only unused auto-created skills)."""
        ...
    def upsert_tag(self, tag_id: str, name: str) -> None: ...
    def attach_edge(self, edge: Edge) -> None: ...
    def detach_edge(self, edge: Edge) -> None:
        """Remove all edges matching (type, from_id, to_id); no-op when none match."""
        ...
    def superseders_of(self, rule_id: str) -> list[str]:
        """Ids of the rules recorded as having superseded this one.

        Exists so a supersession can be undone. `detach_edge` matches on an
        exact (type, from, to) triple, so restoring a superseded rule needs the
        id at the other end of the edge, and nothing else on this port answers
        "who beat this rule". Normally one id; a list because the graph does
        not constrain it to one and a restore must clear whatever is there.
        """
        ...
    def skills_for_rule(self, rule_id: str) -> list[Skill]:
        """Skills the rule BELONGS_TO (deduplicated, deterministic adapter-defined order)."""
        ...
    def skills_by_rule(self, rule_ids: list[str]) -> dict[str, list[Skill]]:
        """The same answer for many rules at once, in one read.

        The batched form exists because `rules_page` bounded the inventory to
        one window and the anchor lookup did not follow it: resolving the
        skills for a 200-row page cost either 200 round trips (one per rule)
        or a full scan of every rule in the tenant to build a map and use 200
        entries of it. Neither is a page.

        A rule with no skill is ABSENT from the result rather than present with
        an empty list, so a caller reads `.get(rule_id, [])` and an unanchored
        rule costs nothing to represent. An empty `rule_ids` returns an empty
        dict without touching the store.
        """
        ...
    def rules_by_tags(self, tags: set[str], tenant_id: str, limit: int = 50) -> list[Rule]: ...
    def skills_by_tags(self, tags: set[str], tenant_id: str) -> list[Skill]:
        """Skills TAGGED_WITH any of these tags (deduplicated, adapter order).

        The topic-level counterpart to `rules_by_tags`, and the reason a skill
        carries its own tags rather than having them copied onto its rules: a
        caller asking "which skills are about billing" wants one answer per
        skill, not one per rule, and reading it through the rules made the
        answer's WEIGHT depend on how many rules a skill happened to have.

        No `limit`, unlike its neighbour. A tag can match thousands of rules,
        so truncating there is a real protection; skills are bounded by the
        tenant's catalogue, and a silent cut would drop a candidate the caller
        is trying to find.
        """
        ...
    def tags_for_skill(self, skill_id: str) -> list[str]:
        """This skill's own tag NAMES (deduplicated, adapter order).

        Empty for a skill with no explicitly assigned tags.
        """
        ...
    def rules_for_skill(self, skill_id: str) -> list[Rule]: ...
    def lineage(self, rule_id: str) -> list[Transaction]:
        """Every transaction this rule was DERIVED_FROM, OLDEST FIRST.

        Use these dated records for provenance timelines. Their number need
        not equal `Rule.corroboration_count`, which is a separate stored value
        used for publication ordering; neither may be inferred from the other.
        """
        ...
    # lexical full-text: precise entry from free text, scoped to one tenant
    def text_search(self, query: str, tenant_id: str, limit: int = 20) -> list[tuple[str, float]]: ...
    # Shared persistence and publication operations.
    def upsert_learning(self, learning: Learning) -> None: ...
    def get_learning(self, learning_id: str) -> Learning | None: ...
    def upsert_constraint(self, constraint: Constraint) -> None: ...

    def active_constraints(self, tenant_id: str) -> list[Constraint]:
        """The constraints that BIND: everything this tenant holds at status
        ACTIVE.

        A constraint written before `status` existed carries no such property.
        Both adapters read a missing one as ACTIVE, so nothing already in a
        graph stops binding.
        """
        ...

    def all_constraints(self, tenant_id: str) -> list[Constraint]:
        """Every constraint, retired ones included.

        Separate from `active_constraints` rather than a flag on it, because
        exactly one caller wants the retired rows - the administration panel,
        which has to show a retired constraint in order to offer to restore it.
        A default argument on the method above would put the burden of getting
        it right on six callers that must never see one.
        """
        ...

    def set_constraint_status(self, constraint_id: str,
                              status: ConstraintStatus) -> None:
        """Retire a constraint, or put it back.

        One method for both directions, as the rule inventory's retract and
        restore are one write with two values. Deliberately not a delete: what
        once bound the tenant's agents is part of the record of why a rule was
        rejected, and deleting the constraint would leave that decision
        unexplainable.
        """
        ...
    def skills_for_tenant(self, tenant_id: str) -> list[Skill]: ...
    def pending_transactions(self, limit: int = 100,
                             tenant_id: str | None = None) -> list[Transaction]:
        """Uncompiled transactions, oldest first, excluding those marked
        compile-failed and those held for review (held_reason set).

        `tenant_id` narrows to one tenant; None includes every tenant."""
        ...
    def pending_tenants(self) -> list[str]:
        """Distinct tenant ids with at least one pending transaction, sorted."""
        ...
    def release_transaction(self, transaction_id: str) -> None:
        """Clear a transaction's hold so it becomes pending again (a reviewer
        approved compiling it as stored)."""
        ...
    def mark_compile_failed(self, transaction_id: str, reason: str,
                            fault: bool = True) -> None:
        """Take an unprocessable transaction (e.g. lost payload) out of the
        pending queue permanently, recording why.

        `fault=False` says this settlement is not a defect: the review
        rejections use it, because a transaction an administrator decided
        against is the record of a decision, not a thing that went wrong.
        Both still leave the pending pool identically and both are still
        terminal; the flag only decides whether the console's health strip
        counts it as a fault demanding attention. Defaulting to True keeps
        every existing caller honest - a new failure path has to say it is
        benign rather than inherit it."""
        ...

    def dismiss_failure(self, transaction_id: str, tenant_id: str,
                        actor_person_id: str | None = None) -> bool:
        """Acknowledge one fault so it stops being listed, returning whether a
        listable fault was actually dismissed.

        Nothing is deleted. Faults are terminal by design, so without this the
        health strip accumulates them for the life of the deployment and an
        administrator who has dealt with the cause has no way to say so - which
        makes the count useless precisely when it matters, because a permanent
        "3 failed" is indistinguishable from a new failure.

        The boolean is what lets the route answer 404 for an id that is not a
        listable fault, rather than reporting success over a no-op."""
        ...

    def dismiss_all_failures(self, tenant_id: str,
                             actor_person_id: str | None = None) -> int:
        """Dismiss every currently listable fault for a tenant; returns how
        many. One systemic cause (a misconfigured payload root, say) fails
        every contribution that arrives while it lasts, and clearing those one
        at a time is a punishment for having had the outage."""
        ...
    def set_licence_hold(self, transaction_id: str, reason: str | None) -> None:
        """Set only the licensing reason; never alter review or terminal state."""
        ...

    def claim_compile(self, transaction_id: str, owner: str) -> bool:
        """Atomically claim eligible, unstarted work; honour all existing holds.

        Claims never expire automatically. After a worker crash an operator
        must confirm it stopped before releasing its recorded owner token.
        """
        ...

    def release_compile(self, transaction_id: str, owner: str) -> None:
        """Release only this owner's claim; never clear a review or safety hold."""
        ...

    def set_compile_status(self, transaction_id: str, status: CompileStatus) -> None:
        """Take a transaction out of the pending pool with a reason.

        A no-op on a transaction already marked FAILED: that status is
        terminal ("never retried"), and demoting it to a recoverable one would
        hand a rejected transaction back to the §9.2 admission path. Setting
        FAILED for the first time is mark_compile_failed's job and is
        unaffected; only overwriting an existing FAILED is refused."""
        ...
    def admit_transaction(self, transaction_id: str) -> None:
        """Clear the compile status and record an explicit admission together.

        Preserve `person_id`: admitting work does not identify its author.
        A FAILED transaction is terminal and must remain unchanged.
        """
        ...
    def admit_scope_review(self, transaction_id: str) -> None:
        """Clear the scope hold and persist `scope_reviewed` together.

        Leave identity and `admitted` unchanged: scope review is a separate
        decision. A FAILED transaction is terminal and must remain unchanged.
        """
        ...
    def transactions_for_person(self, person_id: str, tenant_id: str) -> list[Transaction]:
        """Every transaction attributed to this person in this tenant, oldest
        first (the same order as `pending_transactions`, so the two cannot
        disagree about what "first" means).

        Attribution is `Transaction.person_id`, which is set only where the
        credential resolved to a binding (§8). Unbound work carries None,
        belongs to nobody, and is never returned for any person id: counting
        it towards someone would invent an attribution §12 forbids inventing.
        Tenant-scoped, like every other read here: the same person id in
        another tenant is a different person."""
        ...
    def transaction_count_for_person(self, person_id: str, tenant_id: str) -> int:
        """How many transactions `transactions_for_person` would return, without
        returning them.

        Exactly `len(transactions_for_person(person_id, tenant_id))`, and the
        conformance contract asserts that equality directly rather than against
        a literal: this is a cheaper route to the same answer, not a different
        question. Both invariants above therefore carry over unchanged.
        Attribution is `Transaction.person_id`; unbound work (None) belongs to
        nobody and is counted for no person id, a None argument included.
        Tenant-scoped.

        It exists because the only caller wanted a number. The person list
        renders `contribution_count` per row, so taking `len()` of the list made
        loading a roster of N people hydrate every transaction those N people
        had ever written - in the Neo4j adapter a full Transaction label scan
        per person, every matching record over the wire, and a `Transaction`
        built from each one - to display N integers. An aggregate keeps the work
        in the store."""
        ...
    def transactions_for_tenant(self, tenant_id: str,
                                since: datetime | None = None,
                                limit: int = 200) -> list[Transaction]:
        """This tenant's contributions, NEWEST FIRST, optionally narrowed to
        those at or after `since` and capped at `limit`.

        Newest first, unlike `transactions_for_person` and
        `pending_transactions`, which both promise oldest first. The difference
        is not an inconsistency: those two feed a compile queue that must drain
        in arrival order, and this feeds a reverse-chronological activity table
        whose first page is the most recent thing that happened.

        Unattributed work is INCLUDED, unlike the two per-person reads that
        exclude it. There the None means "belongs to nobody, so count it for
        nobody"; here the question is what happened rather than who it belongs
        to, and an unbound contribution is still something that happened.

        The cap is part of the contract rather than a caller's concern, because
        an unbounded read of every transaction a tenant has ever written is not
        a query any surface wants.
        """
        ...

    def clear_transaction_context(self, transaction_ids: list[str], tenant_id: str) -> int:
        """Remove the contributor's own words from these transactions' rows.

        Five fields: the four session context fields (`session_summary`,
        `project_name`, `reuse_case`, `learning_evidence`) and `summary`. They
        are text the contributor's agent wrote, copied from the sanitised
        payload at ingest so a review can show it without opening the payload
        file, so deleting that file does not erase them. An erasure clears
        them here too. Every other field stays, because the row is also the
        record that a contribution arrived and what became of it.

        Only this tenant's transactions among `transaction_ids` change; an id
        that names no transaction, or another tenant's, is skipped. Returns how
        many carried at least one of the five fields, so a second run answers
        0. No ids cost no write.
        """
        ...

    def rules_for_tenant(self, tenant_id: str, limit: int = 500) -> list[Rule]:
        """Every rule in this tenant, NEWEST FIRST, capped at `limit`.

        Newest first for the same reason `transactions_for_tenant` is: this
        feeds the console's rule inventory, whose first page is the most recent
        thing the compiler wrote. Unanchored rules are INCLUDED - a rule that
        belongs to no skill is exactly the row an administrator most needs to
        see, and the per-skill read (`rules_for_skill`) can never return it.
        Status is not filtered here; the inventory's whole point is showing
        retired and pending rows beside the live ones."""
        ...

    def rules_page(self, tenant_id: str, *, status: RuleStatus | None = None,
                   skill_id: str | None = None, query: str | None = None,
                   limit: int = 200, offset: int = 0) -> RulePage:
        """A filtered, paged window onto a tenant's rules, newest first, with
        the total that matched.

        This exists because filtering after a capped read is wrong at real
        volume. The inventory route read the newest 2,000 rules and applied its
        status/text/skill filters to those in Python, so on `acme` - 5,437
        rules, 5,393 of them written in one bulk import - the other 3,437 were
        unreachable. Searching for a rule that existed returned nothing, and a
        rule nobody could list is a rule nobody can retract.

        Every filter therefore runs in the store, against every row, and the
        window is `offset`/`limit` rather than a scan cap. `total` counts the
        same match the window is drawn from, so a screen can say "showing 1-50
        of 5,437" without a second, drifting query.

        `query` is a case-insensitive substring of the rule body: an inventory
        filter for somebody who half-remembers the wording, not the full-text
        or vector search the explorer already offers.

        Ordering is newest first, matching `rules_for_tenant`, and ends in the
        rule id. That last key is load-bearing rather than decorative: 5,393 of
        acme's rules share a creation timestamp to the second, so without a
        total order a page boundary would repeat or skip rows between calls."""
        ...

    def record_quarantine(self, item: QuarantinedPayload) -> None:
        """Record that a contribution was refused by the sanitiser.

        Idempotent on `transaction_id`, matching ingest's own idempotency: a
        client that retries a payload the sanitiser will refuse again must not
        accumulate one row per attempt.

        Metadata only - see `QuarantinedPayload`. A store implementing this
        must never be handed the payload, because the payload is quarantined
        precisely because nobody could prove what is in it."""
        ...

    def quarantined_payloads(self, tenant_id: str,
                             limit: int = 100) -> list[QuarantinedPayload]:
        """Refused contributions in this tenant, NEWEST FIRST.

        The third way work leaves the pipeline unannounced, beside
        `failed_transactions` and the review holds. Listing it is the whole
        point: a sanitiser that starts refusing everything - a misconfigured
        model, a bad rule set - looks from the console exactly like a quiet
        week, while every contributor gets a 422 and gives up."""
        ...

    def failed_transactions(self, tenant_id: str,
                            limit: int = 100) -> list[tuple[Transaction, str]]:
        """FAULTS in this tenant, NEWEST FIRST, each with the reason
        `mark_compile_failed` recorded.

        The counterpart `pending_transactions` deliberately excludes these so
        they do not starve the drain, which also made them invisible: nothing
        could list what had been dropped or say why. The reason travels with
        the row because it is the only thing an administrator can act on.

        Faults, not everything terminal. Three kinds of settlement share
        `compile_status='failed'` and only one of them is a defect:

        * a SKILL_IMPORT transaction is provenance for work the importer
          already did, and reaches this status only when its skill produced no
          rules (`pending_transactions` excludes the rest by their
          DERIVED_FROM edge, so which imports appeared depended on the shape
          of the skill - prose-only ones surfaced, rule-bearing ones did not);
        * a review rejection is an administrator's own decision, recorded by
          `mark_compile_failed(fault=False)`;
        * a lost payload or a not-a-correction verdict is a real fault.

        Only the third is returned, and dismissed rows drop out too. Excluding
        imports on `signal_type` rather than on the reason text is deliberate:
        a filter that matches prose breaks silently the day somebody rewords a
        message."""
        ...

    def transaction_counts_by_person_for_tenant(self, tenant_id: str) -> dict[str, int]:
        """Every person in this tenant who has written anything, mapped to how
        much: `{person_id: transaction_count_for_person(person_id, tenant_id)}`
        for every person the answer is non-zero for, in one read.

        A person with nothing is **absent from the mapping** rather than present
        with a zero, because the store is answering from the transactions it
        holds and has no roster to enumerate against. Callers read a missing key
        as zero; the person list does, and asserts it.

        `None` is never a key. Unbound work (§8.1's HELD outcome) belongs to
        nobody, and here that rule has to be enforced rather than inherited:
        the per-person methods get it free from a comparison against a null
        parameter, but a query that groups by `person_id` returns null as its
        own group, which would render everybody's unattributed work as one more
        person's total. Tenant-scoped, like every other read here.

        It exists because the per-person count is still one round trip per
        person, and the person list renders one row per person. Cheaper queries
        do not change 1 + 2N into anything but 1 + 2N; asking once does."""
        ...

    def rules_conflicting_with_constraints(self, tenant_id: str) -> list[Rule]: ...
    def upsert_example(self, example: Example) -> None: ...
    def examples_for_rule(self, rule_id: str) -> list[Example]: ...
    def examples_for_skill(self, skill_id: str) -> list[Example]: ...
    # Skill-package custody operations.
    def upsert_section(self, section: Section) -> None: ...
    def get_section(self, section_id: str) -> Section | None: ...
    def sections_for_skill(self, skill_id: str) -> list[Section]: ...
    def attach_rule(self, rule: Rule, section_id: str,
                    order: int | None = None, group: str | None = None) -> None: ...
    def inherit_placements(self, retired_rule_id: str, successor: Rule) -> None:
        """Copy the retired rule's CONTAINS_RULE section memberships (with order
        and group) to `successor`, so a superseding rule renders in the retired
        rule's place instead of only in the aggregated tail. The retired rule
        keeps its own membership for audit; the render filters it out by status."""
        ...
    def attach_block(self, block: ContentBlock, section_id: str) -> None: ...
    def supersede_block(
        self, old_block_id: str, new_block: ContentBlock, section_id: str,
        transaction_id: str | None = None,
    ) -> None:
        """Revise an authorial section's body (Section 6.5). Upsert `new_block`
        as the ACTIVE block, attach it to the section, mark the old block
        SUPERSEDED (kept attached for history), create (new)-[:SUPERSEDES]->(old),
        and when `transaction_id` is given (new)-[:DERIVED_FROM]->(txn)."""
        ...
    def rules_for_section(self, section_id: str) -> list[Rule]: ...
    def rule_placements_for_section(self, section_id: str) -> list[RulePlacement]: ...
    def blocks_for_section(self, section_id: str) -> list[ContentBlock]:
        """The section's active blocks only (publisher, renderer and explorer all
        want the active body); superseded blocks stay attached for audit."""
        ...

    def blocks_revised_for_rule(self, rule_id: str) -> list[ContentBlock]:
        """Active content blocks a revision rewrote to state this rule.

        Lineage, not lexical guesswork: the rule DERIVED_FROM its transactions
        (creation and every corroboration), and a block a revision rewrote
        DERIVED_FROM the same transaction. Used when the rule is superseded,
        so the prose that was patched to say the old value is patched again
        to say the new one.
        """
        ...
    def sections_with_multiple_active_blocks(self, tenant_id: str) -> list[str]:
        """Section ids (within the tenant) carrying more than one ACTIVE block.
        The publish gate's custody invariant (Section 7.3) requires every
        authorial section to hold exactly one active ContentBlock; any returned
        id is a violation. Blocks with no status property read as active for
        backward compatibility (coalesce(status,'active')='active')."""
        ...

    def active_blocks_without_body(self, tenant_id: str) -> list[str]:
        """Ids of this tenant's ACTIVE content blocks that carry no text.

        A block's text lives on the node, so an empty one would publish its
        section as blank. The case that produces them is historical: blocks
        imported when the importer inlined only bodies under 1024 characters
        and left longer ones in the blob store. `scripts/migrate_block_bodies.py`
        backfills those. Superseded blocks are excluded - they are history, and
        only what publishes has to be intact."""
        ...
    def attach_example(self, example: Example) -> None: ...
    def upsert_content_block(self, block: ContentBlock) -> None: ...
    def rule_context(self, rule_ids: list[str]) -> dict[str, RuleContext]:
        """Skills, domains and the original contributor, for many rules at once.

        Batched because the caller is the activity feed: a page of 200 rows
        would otherwise be 200 `skills_for_rule` calls plus 200 `lineage` calls
        to render one column. Ids with no such rule are simply absent from the
        result rather than mapping to an empty context, so a caller can tell
        "this rule is gone" from "this rule has no skills".
        """
        ...
    def rules_derived_from(self, transaction_ids: list[str]) -> dict[str, str]:
        """`{transaction_id: rule_id}` for the rules DERIVED_FROM each one.

        The reverse of `lineage`, batched, and the join that lets a
        contribution in the activity feed be grouped with the review decisions
        that followed it. A transaction that produced no rule (held, rejected
        before compile, or corroborated an existing rule under a different id)
        is absent. Where a transaction somehow has several, the answer is the
        adapter's first, which is deterministic but arbitrary.
        """
        ...
    def get_content_block(self, block_id: str) -> ContentBlock | None:
        """One block by id, whatever its status. `blocks_for_section` is the
        publish-side read and filters to ACTIVE; this one does not, because a
        review item can outlive the block it names - a `block_conflict` queued
        against prose that has since been revised must still be able to show
        the reviewer the text the verdict was actually about."""
        ...
    def section_for_block(self, block_id: str) -> Section | None:
        """The section that CONTAINS_BLOCK this block, or None if the block is
        unknown or unattached. The reverse of `blocks_for_section`: a review
        item holds a block id and needs the heading and skill around it, and
        ContentBlock itself carries no section_id (custody lives on the edge)."""
        ...
    def upsert_artefact(self, artefact: Artefact, skill_id: str, path: str) -> None: ...
    def artefacts_for_skill(self, skill_id: str) -> list[tuple[Artefact, str]]: ...
    def upsert_publication(self, publication: Publication) -> None: ...
    def get_publication(self, tenant_id: str, source_ref: str) -> Publication | None: ...
    def publications_for_skill(self, tenant_id: str, skill_id: str) -> list[Publication]:
        """Every ledger row for one skill's published files: the proof of
        ownership prune-on-publish requires before deleting anything."""
        ...
    def delete_publications_for_skill(self, tenant_id: str, skill_id: str) -> None: ...
    def delete_publication(self, tenant_id: str, source_ref: str) -> None:
        """Drop one ledger row, keyed the way `get_publication` reads it.

        The by-skill delete is too blunt for the root-level artefacts, which
        all share the one "(org)" skill id: pruning a single retired root file
        must not take the rest of the bundle's proof of ownership with it.
        Deleting a row that isn't there is not an error."""
        ...

    def append_skill_version(self, version: SkillVersion) -> None: ...
    def recent_skill_changes(self, tenant_id: str, limit: int = 8) -> list[SkillChange]:
        """Newest summaries for existing skills in this tenant, ordered by at/id DESC."""
        ...
    def skill_versions(self, tenant_id: str, skill_id: str,
                       limit: int = 50,
                       before: datetime | None = None) -> list[SkillVersion]:
        """Newest first. `before` pages strictly older rows."""
        ...
    def get_skill_version(self, version_id: str) -> SkillVersion | None: ...
    def latest_skill_version(self, tenant_id: str,
                             skill_id: str) -> SkillVersion | None: ...
    def trim_skill_versions(self, tenant_id: str, skill_id: str,
                            keep: int) -> int:
        """Drop all but the newest `keep` versions, always keeping the oldest
        row (the `created` snapshot). Returns how many were removed."""
        ...

    # The skill-fetch stream (spec §7.5.1). Tier 2 read tools write one event
    # per query_skill call; applied-frequency is the count over a window.
    def record_usage_event(self, event: UsageEvent) -> None: ...
    def usage_events(self, tenant_id: str, skill_id: str | None = None,
                     since: datetime | None = None) -> list[UsageEvent]:
        """A tenant's fetch events, oldest first, optionally narrowed to one
        skill and to events at or after `since`."""
        ...

    # The block_publish flag (eval re-architecture): the bridge between the
    # compile-step safety gate / periodic check (which set it) and the publish
    # step (which only reads it). set_publish_block replaces any existing block.
    def set_publish_block(self, tenant_id: str, reasons: list[str], source: str) -> None: ...
    def clear_publish_block(self, tenant_id: str) -> None: ...
    def get_publish_block(self, tenant_id: str) -> PublishBlock | None: ...

    def observe_rule_in(self, rule_id: str, transaction_id: str, source_ref: str) -> None:
        """Record the source, adding one corroboration only when it is new.

        Preserve existing manual and inherited counts. Newly imported rules
        start at zero and acquire their first count from this operation.
        """
        ...
    def examples_for_section(self, section_id: str) -> list[Example]: ...
    def upsert_cross_skill_edge(
        self, edge_type: EdgeType, from_rule_id: str, to_rule_id: str,
        confidence: float, kind: str | None = None,
    ) -> None: ...
    def cross_skill_neighbours(self, rule_id: str) -> list[tuple[EdgeType, str, float]]: ...
