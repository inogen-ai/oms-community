import math
from dataclasses import replace
from datetime import datetime, timezone

from oms.domain.models import (
    Rule, Skill, Transaction, Edge, Learning, Constraint, Example,
    Section, ContentBlock, Artefact, Publication, PublishBlock, QuarantinedPayload,
    RulePage, RulePlacement, UsageEvent, SkillVersion, SkillChange,
)
from oms.domain.types import (CompileStatus, ConstraintSource, ConstraintStatus, EdgeType,
                              BlockStatus, Mutability, SignalType, RuleStatus)
from oms.ports.graph_store import RuleContext, SectionMutabilityError


class InMemoryGraphStore:
    def graph_node(self, node_id, tenant_id):
        from dataclasses import asdict
        from oms.adapters.graph_view import graph_record
        for label, records in (("Skill", self.skills), ("Rule", self.rules),
                ("Transaction", self.transactions), ("Section", self.sections),
                ("ContentBlock", self.content_blocks), ("Artefact", self.artefacts),
                ("Example", self.examples), ("Constraint", self.constraints)):
            value = records.get(node_id)
            if value is not None and value.tenant_id == tenant_id:
                return graph_record(asdict(value), label)
        if node_id in self.tags and any(e.to_id == node_id and
                any(e.from_id in records and records[e.from_id].tenant_id == tenant_id
                    for records in (self.skills, self.rules)) for e in self.edges):
            return graph_record({"id": node_id, "name": self.tags[node_id]}, "Tag")
        return None

    def graph_neighbours(self, node_id, tenant_id, *, offset=0, limit=100):
        edges = {(e.from_id, e.to_id, e.type.value) for e in self.edges}
        edges.update((s.skill_id, s.id, "HAS_SECTION") for s in self.sections.values())
        edges.update((sid, rid, "CONTAINS_RULE") for sid, ids in self.section_to_rules.items() for rid in ids)
        edges.update((sid, bid, "CONTAINS_BLOCK") for sid, ids in self.section_to_blocks.items() for bid in ids)
        edges.update((sid, aid, "HAS_ARTEFACT") for sid, files in self.skill_to_artefacts.items() for aid, _ in files)
        edges.update((frm, to, kind.value) for kind, frm, to, *_ in self.cross_edges)
        for example in self.examples.values():
            for field in ("parent_section_id", "parent_skill_id", "parent_rule_id"):
                parent = getattr(example, field, None)
                if parent:
                    edges.add((parent, example.id, "HAS_EXAMPLE"))
        neighbours = {}
        for source, target, kind in sorted(edges):
            if node_id not in (source, target):
                continue
            other = target if source == node_id else source
            found = self.graph_node(other, tenant_id)
            if found:
                neighbours.setdefault(other, (found, []))[1].append({"source": source, "target": target, "type": kind})
        chosen = sorted(neighbours)[offset:offset + limit]
        return {"nodes": [neighbours[key][0] for key in chosen],
                "edges": [edge for key in chosen for edge in neighbours[key][1]],
                "total": len(neighbours),
                "next_offset": offset + limit if offset + limit < len(neighbours) else None}

    def __init__(self) -> None:
        import threading
        self._compile_mutex = threading.Lock()
        self._workflow_mutex = threading.RLock()
        self._compile_claims: dict[str, str] = {}
        self.rules: dict[str, Rule] = {}
        self.transactions: dict[str, Transaction] = {}
        self.skills: dict[str, Skill] = {}
        self.tags: dict[str, str] = {}
        self.edges: list[Edge] = []
        self.learnings: dict[str, Learning] = {}
        self.constraints: dict[str, Constraint] = {}
        self.examples: dict[str, Example] = {}
        # Skill-package custody storage.
        self.sections: dict[str, Section] = {}
        self.content_blocks: dict[str, ContentBlock] = {}
        self.artefacts: dict[str, Artefact] = {}
        self.publications: dict[tuple[str, str], Publication] = {}
        self.publish_blocks: dict[str, PublishBlock] = {}          # tenant_id -> block flag
        self.observations: dict[str, set[tuple[str, str]]] = {}   # rule_id -> {(txn_id, source_ref)}
        self.compile_failures: dict[str, str] = {}                 # txn_id -> reason
        self.non_fault_failures: dict[str, bool] = {}              # txn_id -> settled, not a defect
        self.dismissed_failures: dict[str, str | None] = {}        # txn_id -> who acknowledged it
        self.compile_status: dict[str, CompileStatus] = {}         # txn_id -> out of pending, why
        self.cross_edges: list[tuple[EdgeType, str, str, float, str | None]] = []
        self.section_to_rules: dict[str, list[str]] = {}
        self.section_rule_placements: dict[str, dict[str, RulePlacement]] = {}   # section_id -> rule_id -> placement
        self.section_to_blocks: dict[str, list[str]] = {}
        self.skill_to_artefacts: dict[str, list[tuple[str, str]]] = {}    # skill_id -> [(artefact_id, path)]
        self.usage: dict[str, UsageEvent] = {}                     # skill-fetch stream (spec §7.5.1)
        # Contributions the sanitiser refused, keyed by transaction id: the
        # third way work leaves the pipeline, beside failures and holds.
        self.quarantined: dict[str, QuarantinedPayload] = {}
        self._skill_versions: list[SkillVersion] = []

    def upsert_rule(self, rule: Rule) -> None:
        self.rules[rule.id] = rule

    def create_rule_if_absent(self, rule: Rule) -> None:
        with self._workflow_mutex:
            self.rules.setdefault(rule.id, rule)

    def get_rule(self, rule_id: str) -> Rule | None:
        return self.rules.get(rule_id)

    def upsert_transaction(self, transaction: Transaction) -> None:
        self.transactions[transaction.id] = transaction

    def workflow_state_for(self, transaction_id: str) -> str:
        txn = self.transactions.get(transaction_id)
        if txn and txn.workflow_state:
            return txn.workflow_state
        if self.compile_status.get(transaction_id) is CompileStatus.FAILED:
            return "failed"
        return "received"

    def get_transaction(self, transaction_id: str) -> Transaction | None:
        return self.transactions.get(transaction_id)

    def upsert_skill(self, skill: Skill) -> None:
        self.skills[skill.id] = skill

    def get_skill(self, skill_id: str) -> Skill | None:
        return self.skills.get(skill_id)

    def delete_skill(self, skill_id: str) -> None:
        # Cascade to what the skill owned. Dropping only the node and its edges
        # left sections and their blocks behind, still holding their text and
        # still naming this skill; because upsert_section MERGEs on the section
        # id, a later re-import of the same skill id would reattach to them and
        # silently restore prose an administrator had deleted.
        #
        # Rules are NOT removed: they survive unanchored, because provenance
        # outlives the folder.
        self.skills.pop(skill_id, None)
        self.edges = [e for e in self.edges
                      if e.from_id != skill_id and e.to_id != skill_id]

        doomed = {sid for sid, sec in self.sections.items()
                  if sec.skill_id == skill_id}
        for section_id in doomed:
            for block_id in self.section_to_blocks.pop(section_id, []):
                self.content_blocks.pop(block_id, None)
            self.section_to_rules.pop(section_id, None)
            self.section_rule_placements.pop(section_id, None)
            self.sections.pop(section_id, None)
        # Examples attach to a section, a rule, or straight to the skill. The
        # first and third belonged to this skill; the rule-attached ones stay,
        # because their rule stays.
        self.examples = {
            eid: ex for eid, ex in self.examples.items()
            if ex.parent_section_id not in doomed and ex.parent_skill_id != skill_id
        }
        self.skill_to_artefacts.pop(skill_id, None)
        # Skill ids are a deterministic slug of the name, so a new skill
        # created under the same name inherits the old id. A version left
        # behind would hand it the deleted skill's whole history - parts_json
        # and rules_json included - and a later restore would write back
        # prose an administrator deleted. The AdminEvent stream keeps the
        # fact of the deletion, as it does today.
        self._skill_versions = [v for v in self._skill_versions
                                if v.skill_id != skill_id]

    def upsert_tag(self, tag_id: str, name: str) -> None:
        self.tags[tag_id] = name

    def attach_edge(self, edge: Edge) -> None:
        # Match Neo4j's MERGE identity. Reinforcement reattaches membership
        # deliberately; it must not duplicate a rule in the next publication.
        if not any(existing.type is edge.type and existing.from_id == edge.from_id
                   and existing.to_id == edge.to_id for existing in self.edges):
            self.edges.append(edge)

    def detach_edge(self, edge: Edge) -> None:
        self.edges = [
            e for e in self.edges
            if not (e.type is edge.type and e.from_id == edge.from_id and e.to_id == edge.to_id)
        ]

    def superseders_of(self, rule_id: str) -> list[str]:
        seen: list[str] = []
        for e in self.edges:
            if e.type is EdgeType.SUPERSEDES and e.to_id == rule_id and e.from_id not in seen:
                seen.append(e.from_id)
        return seen

    def skills_for_rule(self, rule_id: str) -> list[Skill]:
        out: list[Skill] = []
        seen: set[str] = set()
        for e in self.edges:
            if e.type is EdgeType.BELONGS_TO and e.from_id == rule_id:
                skill = self.skills.get(e.to_id)
                if skill is not None and skill.id not in seen:
                    seen.add(skill.id)
                    out.append(skill)
        return out

    def skills_by_rule(self, rule_ids: list[str]) -> dict[str, list[Skill]]:
        wanted = set(rule_ids)
        out: dict[str, list[Skill]] = {}
        for rule_id in sorted(wanted):
            skills = self.skills_for_rule(rule_id)
            if skills:
                out[rule_id] = skills
        return out

    def rules_by_tags(self, tags: set[str], tenant_id: str, limit: int = 50) -> list[Rule]:
        out: list[Rule] = []
        for e in self.edges:
            if e.type is EdgeType.TAGGED_WITH and self.tags.get(e.to_id) in tags:
                rule = self.rules.get(e.from_id)
                if rule and rule.tenant_id == tenant_id and rule not in out:
                    out.append(rule)
        return out[:limit]

    def skills_by_tags(self, tags: set[str], tenant_id: str) -> list[Skill]:
        out: list[Skill] = []
        seen: set[str] = set()
        for e in self.edges:
            if e.type is not EdgeType.TAGGED_WITH or self.tags.get(e.to_id) not in tags:
                continue
            skill = self.skills.get(e.from_id)
            if skill is not None and skill.tenant_id == tenant_id and skill.id not in seen:
                seen.add(skill.id)
                out.append(skill)
        return out

    def tags_for_skill(self, skill_id: str) -> list[str]:
        out: list[str] = []
        for e in self.edges:
            if e.type is not EdgeType.TAGGED_WITH or e.from_id != skill_id:
                continue
            name = self.tags.get(e.to_id)
            if name is not None and name not in out:
                out.append(name)
        return out

    def rules_for_skill(self, skill_id: str) -> list[Rule]:
        return [
            self.rules[e.from_id]
            for e in self.edges
            if e.type is EdgeType.BELONGS_TO and e.to_id == skill_id and e.from_id in self.rules
        ]

    def lineage(self, rule_id: str) -> list[Transaction]:
        rows = [
            self.transactions[e.to_id]
            for e in self.edges
            if e.type is EdgeType.DERIVED_FROM and e.from_id == rule_id and e.to_id in self.transactions
        ]
        # Oldest first, promised by the port: the rule story reads this as a
        # timeline and edge-insertion order is not chronological order.
        return sorted(rows, key=lambda t: t.timestamp)

    def rule_context(self, rule_ids: list[str]) -> dict[str, RuleContext]:
        out: dict[str, RuleContext] = {}
        for rule_id in rule_ids:
            if rule_id not in self.rules:
                continue   # absent, not empty: the caller can tell them apart
            skills = self.skills_for_rule(rule_id)
            # The correction it was BORN from, not the latest one that
            # corroborated it. lineage promises oldest first.
            born = next((t for t in self.lineage(rule_id) if t.person_id), None)
            out[rule_id] = RuleContext(
                rule_id=rule_id,
                skills=tuple(s.name for s in skills),
                domains=tuple(sorted({s.domain for s in skills if s.domain})),
                contributor_person_id=born.person_id if born else None)
        return out

    def rules_derived_from(self, transaction_ids: list[str]) -> dict[str, str]:
        wanted = set(transaction_ids)
        out: dict[str, str] = {}
        for edge in self.edges:
            if edge.type is not EdgeType.DERIVED_FROM or edge.to_id not in wanted:
                continue
            # Rules only: DERIVED_FROM also hangs off revised ContentBlocks.
            if edge.from_id in self.rules:
                out.setdefault(edge.to_id, edge.from_id)
        return out

    def transactions_for_tenant(self, tenant_id: str,
                                since: datetime | None = None,
                                limit: int = 200) -> list[Transaction]:
        rows = [t for t in self.transactions.values()
                if t.tenant_id == tenant_id
                and (since is None or t.timestamp >= since)]
        return sorted(rows, key=lambda t: t.timestamp, reverse=True)[:limit]

    def rules_for_tenant(self, tenant_id: str, limit: int = 500) -> list[Rule]:
        rows = [r for r in self.rules.values() if r.tenant_id == tenant_id]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    def rules_page(self, tenant_id: str, *, status: RuleStatus | None = None,
                   skill_id: str | None = None, query: str | None = None,
                   limit: int = 200, offset: int = 0) -> RulePage:
        belongs = {(e.from_id, e.to_id) for e in self.edges
                   if e.type is EdgeType.BELONGS_TO}
        needle = (query or "").strip().lower()
        matched = [r for r in self.rules.values()
                   if r.tenant_id == tenant_id
                   and (status is None or r.status is status)
                   and (not needle or needle in r.body.lower())
                   and (skill_id is None or (r.id, skill_id) in belongs)]
        # Newest first, then the id. The id is not decoration: acme's rules were
        # written by a bulk import and share a created_at to the second, so
        # created_at alone is not a total order and a page boundary would repeat
        # or skip rows between two calls.
        matched.sort(key=lambda r: (-r.created_at.timestamp(), r.id))
        return RulePage(items=matched[offset:offset + limit], total=len(matched),
                        offset=offset, limit=limit)

    def record_quarantine(self, item: QuarantinedPayload) -> None:
        # Keyed on the transaction id, so a retried payload the sanitiser
        # refuses again is one row rather than one per attempt.
        self.quarantined[item.transaction_id] = item

    def quarantined_payloads(self, tenant_id: str,
                             limit: int = 100) -> list[QuarantinedPayload]:
        rows = [q for q in self.quarantined.values() if q.tenant_id == tenant_id]
        rows.sort(key=lambda q: q.quarantined_at, reverse=True)
        return rows[:limit]

    def failed_transactions(self, tenant_id: str,
                            limit: int = 100) -> list[tuple[Transaction, str]]:
        # The `or` matches the Neo4j adapter's coalesce: an empty reason renders
        # as "no reason recorded" from both, so the contract can pin one answer.
        rows = [(t, self.compile_failures[t.id] or "no reason recorded")
                for t in self.transactions.values()
                if t.id in self.compile_failures and t.tenant_id == tenant_id
                and self._is_listable_fault(t)]
        rows.sort(key=lambda pair: pair[0].timestamp, reverse=True)
        return rows[:limit]

    def _is_listable_fault(self, txn: Transaction) -> bool:
        """A defect an administrator has not yet acknowledged. See the port."""
        if txn.signal_type is SignalType.SKILL_IMPORT:
            return False
        if txn.id in self.dismissed_failures:
            return False
        # Absent means fault, matching the Neo4j adapter's coalesce: a row
        # written before this flag existed is an unknown, and an unknown fault
        # is safer shown than hidden.
        return self.non_fault_failures.get(txn.id, False) is False

    def text_search(self, query: str, tenant_id: str, limit: int = 20) -> list[tuple[str, float]]:
        terms = {t.lower() for t in query.split()}
        scored = [
            (r.id, float(sum(r.body.lower().count(t) for t in terms)))
            for r in self.rules.values() if r.tenant_id == tenant_id
        ]
        scored = [s for s in scored if s[1] > 0]
        scored.sort(key=lambda s: s[1], reverse=True)
        return scored[:limit]

    def upsert_learning(self, learning: Learning) -> None:
        self.learnings[learning.id] = learning

    def get_learning(self, learning_id: str) -> Learning | None:
        return self.learnings.get(learning_id)

    def upsert_constraint(self, constraint: Constraint) -> None:
        self.constraints[constraint.id] = constraint

    def active_constraints(self, tenant_id: str) -> list[Constraint]:
        return [c for c in self.constraints.values()
                if c.tenant_id == tenant_id
                and c.status is ConstraintStatus.ACTIVE]

    def all_constraints(self, tenant_id: str) -> list[Constraint]:
        return [c for c in self.constraints.values() if c.tenant_id == tenant_id]

    def set_constraint_status(self, constraint_id: str,
                              status: ConstraintStatus) -> None:
        # Silent on a miss, as the Neo4j adapter's MATCH is: the route above
        # has already read the constraint to decide whether it may be touched,
        # and a second existence check here would answer a question that was
        # asked one line earlier.
        held = self.constraints.get(constraint_id)
        if held is None:
            return
        self.constraints[constraint_id] = replace(held, status=status)

    def skills_for_tenant(self, tenant_id: str) -> list[Skill]:
        return [s for s in self.skills.values() if s.tenant_id == tenant_id]

    def transactions_for_person(self, person_id: str, tenant_id: str) -> list[Transaction]:
        # Attribution predicate in `_attributed_to`, shared with the count.
        rows = [t for t in self.transactions.values()
                if self._attributed_to(t, person_id, tenant_id)]
        rows.sort(key=lambda t: t.timestamp)
        return rows

    def transaction_count_for_person(self, person_id: str, tenant_id: str) -> int:
        # Shares `_attributed_to` with the list method rather than restating the
        # predicate. The contract pins the two against each other, so a
        # divergence would be caught either way; sharing means there is nothing
        # to diverge. No sort: order is not observable through a count, and this
        # adapter's whole reason for existing is to behave like the Cypher one,
        # which is aggregating in the database.
        return sum(1 for t in self.transactions.values()
                   if self._attributed_to(t, person_id, tenant_id))

    def transaction_counts_by_person_for_tenant(self, tenant_id: str) -> dict[str, int]:
        # One pass over the tenant's transactions instead of one pass per
        # person. `t.person_id is not None` is load-bearing here in a way it is
        # not in the per-person methods: there is no person argument for the
        # equality to filter against, so without it unbound work would become a
        # None key and the caller would render everybody's unattributed work as
        # somebody's total. The Cypher adapter needs the identical clause for
        # the identical reason - a GROUP BY returns null as its own group - so
        # for once the two adapters are guarding against the same thing rather
        # than one compensating for the other.
        #
        # No key for a person with nothing: this counts what it holds, and the
        # roster is the caller's to enumerate.
        counts: dict[str, int] = {}
        for t in self.transactions.values():
            if t.person_id is not None and t.tenant_id == tenant_id:
                counts[t.person_id] = counts.get(t.person_id, 0) + 1
        return counts

    @staticmethod
    def _attributed_to(transaction: Transaction, person_id: str,
                       tenant_id: str) -> bool:
        # `person_id is not None` is not redundant with the equality: it is
        # what keeps a None argument from sweeping up every unbound
        # transaction in the tenant, which the Cypher adapter cannot do
        # anyway (a property match against a null parameter yields nothing).
        # Without it the two adapters would answer differently for the same
        # call, and unbound work would be counted towards a person.
        return (transaction.person_id is not None
                and transaction.person_id == person_id
                and transaction.tenant_id == tenant_id)

    def pending_transactions(self, limit: int = 100,
                             tenant_id: str | None = None) -> list[Transaction]:
        compiled = {e.to_id for e in self.edges if e.type is EdgeType.DERIVED_FROM}
        pending = [t for t in self.transactions.values()
                   if t.id not in compiled and t.id not in self.compile_failures
                   and t.id not in self.compile_status
                   and t.id not in self._compile_claims
                   and t.workflow_state in (None, "queued_automation")
                   and t.held_reason is None
                   and (tenant_id is None or t.tenant_id == tenant_id)]
        pending.sort(key=lambda t: t.timestamp)
        return pending[:limit]

    def pending_tenants(self) -> list[str]:
        return sorted({t.tenant_id for t in self.pending_transactions(limit=10_000)})

    def release_transaction(self, transaction_id: str) -> None:
        txn = self.transactions.get(transaction_id)
        if txn is not None:
            txn.held_reason = None
            if txn.workflow_state == "held_safety":
                txn.workflow_state = "queued_automation"

    def mark_compile_failed(self, transaction_id: str, reason: str,
                            fault: bool = True) -> None:
        self.compile_failures[transaction_id] = reason
        if not fault:
            self.non_fault_failures[transaction_id] = True
        self.set_compile_status(transaction_id, CompileStatus.FAILED)

    def dismiss_failure(self, transaction_id: str, tenant_id: str,
                        actor_person_id: str | None = None) -> bool:
        txn = self.transactions.get(transaction_id)
        if txn is None or txn.tenant_id != tenant_id:
            return False
        if transaction_id not in self.compile_failures:
            return False
        if not self._is_listable_fault(txn):
            return False
        self.dismissed_failures[transaction_id] = actor_person_id
        return True

    def dismiss_all_failures(self, tenant_id: str,
                             actor_person_id: str | None = None) -> int:
        # Reads the same listable set the route just showed, so "dismiss all"
        # can never clear something the administrator was not looking at.
        ids = [t.id for t, _reason in self.failed_transactions(tenant_id, limit=10_000)]
        for txn_id in ids:
            self.dismissed_failures[txn_id] = actor_person_id
        return len(ids)

    def claim_compile(self, transaction_id: str, owner: str) -> bool:
        from contextlib import nullcontext
        with getattr(self, "_workflow_mutex", nullcontext()), self._compile_mutex:
            txn = self.transactions.get(transaction_id)
            if (txn is None or txn.held_reason is not None
                    or txn.workflow_state not in (None, "queued_automation")
                    or transaction_id in self.compile_status
                    or transaction_id in self._compile_claims
                    or self.rules_derived_from([transaction_id])):
                return False
            self._compile_claims[transaction_id] = owner
            if txn.workflow_state is not None:
                txn.workflow_state = "processing"
            return True

    def release_compile(self, transaction_id: str, owner: str) -> None:
        with self._compile_mutex:
            if self._compile_claims.get(transaction_id) == owner:
                del self._compile_claims[transaction_id]
                txn = self.transactions.get(transaction_id)
                if txn is not None and txn.workflow_state == "processing":
                    txn.workflow_state = "queued_automation"

    def set_licence_hold(self, transaction_id: str, reason: str | None) -> None:
        txn = self.transactions.get(transaction_id)
        if txn is not None:
            txn.licence_hold = reason

    def set_compile_status(self, transaction_id: str, status: CompileStatus) -> None:
        # FAILED is terminal ("never retried"), so an existing FAILED is never
        # overwritten by a later park. Without this the §9.2 gate would demote
        # a rejected transaction to AWAITING_ADMISSION, which in the Neo4j
        # adapter is enough to put it back in the pending pool and let
        # admit_transaction clear the gate: explicitly rejected work would
        # resume. mark_compile_failed still SETS the failure in the first
        # place; only overwriting one is refused.
        if self.compile_status.get(transaction_id) is CompileStatus.FAILED:
            return
        self.compile_status[transaction_id] = status
        txn = self.transactions.get(transaction_id)
        if txn is not None and txn.workflow_state is not None:
            txn.workflow_state = "failed" if status is CompileStatus.FAILED else "awaiting_manual_review"

    def admit_transaction(self, transaction_id: str) -> None:
        # A permanently FAILED transaction (mark_compile_failed: "never
        # retried") stays excluded even across admission - admit_transaction
        # exists to lift the AWAITING_ADMISSION park (§9.2), not to override a
        # terminal failure. Guarding here (rather than relying only on the
        # untouched compile_failures map pending_transactions also checks)
        # keeps the guard visible at the call that could otherwise look like
        # it un-parks anything, and matches the Neo4j adapter's WHERE clause.
        if self.compile_status.get(transaction_id) is CompileStatus.FAILED:
            return
        self.compile_status.pop(transaction_id, None)
        # The half that makes admission stick: without it the §9.2 gate
        # re-parks the transaction on the next drain, since it is still
        # unattributed and always will be.
        txn = self.transactions.get(transaction_id)
        if txn is not None:
            txn.admitted = True
            if txn.workflow_state is not None:
                txn.workflow_state = "queued_automation"

    def admit_scope_review(self, transaction_id: str) -> None:
        # Same terminal-FAILED guard as admit_transaction, for the same
        # reason: a permanently FAILED transaction (mark_compile_failed:
        # "never retried") stays excluded even across a scope override.
        if self.compile_status.get(transaction_id) is CompileStatus.FAILED:
            return
        self.compile_status.pop(transaction_id, None)
        # Keep the explicit decision when clearing its hold.
        txn = self.transactions.get(transaction_id)
        if txn is not None:
            txn.scope_reviewed = True
            if txn.workflow_state is not None:
                txn.workflow_state = "queued_automation"

    def rules_conflicting_with_constraints(self, tenant_id: str) -> list[Rule]:
        out: list[Rule] = []
        for e in self.edges:
            if e.type is EdgeType.CONFLICTS_WITH and e.to_id in self.constraints:
                rule = self.rules.get(e.from_id)
                if rule and rule.tenant_id == tenant_id and rule not in out:
                    out.append(rule)
        return out

    def upsert_example(self, example: Example) -> None:
        self.examples[example.id] = example

    def examples_for_rule(self, rule_id: str) -> list[Example]:
        return [e for e in self.examples.values() if e.parent_rule_id == rule_id]

    def examples_for_skill(self, skill_id: str) -> list[Example]:
        # Mirrors the Neo4j adapter: direct skill parents plus the skill's
        # sections' examples (single-parent storage).
        section_ids = {s.id for s in self.sections.values() if s.skill_id == skill_id}
        return [e for e in self.examples.values()
                if e.parent_skill_id == skill_id or e.parent_section_id in section_ids]

    # -- Skill-package custody operations ----------------------------------

    def upsert_section(self, section: Section) -> None:
        self.sections[section.id] = section

    def get_section(self, section_id: str) -> Section | None:
        return self.sections.get(section_id)

    def sections_for_skill(self, skill_id: str) -> list[Section]:
        sections = [s for s in self.sections.values() if s.skill_id == skill_id]
        sections.sort(key=lambda s: s.order)
        return sections

    def attach_rule(self, rule: Rule, section_id: str,
                    order: int | None = None, group: str | None = None) -> None:
        sec = self.sections.get(section_id)
        if sec is None:
            raise KeyError(section_id)
        if sec.mutability is not Mutability.SYSTEM_AGGREGATED:
            raise SectionMutabilityError(
                f"section {section_id} is {sec.mutability.value}; cannot attach rule"
            )
        bucket = self.section_to_rules.setdefault(section_id, [])
        if rule.id not in bucket:
            bucket.append(rule.id)
        placements = self.section_rule_placements.setdefault(section_id, {})
        placements[rule.id] = RulePlacement(rule_id=rule.id, order=order, group=group)

    def inherit_placements(self, retired_rule_id: str, successor: Rule) -> None:
        for section_id, placements in list(self.section_rule_placements.items()):
            p = placements.get(retired_rule_id)
            if p is None:
                continue
            self.attach_rule(successor, section_id, order=p.order, group=p.group)

    def attach_block(self, block: ContentBlock, section_id: str) -> None:
        sec = self.sections.get(section_id)
        if sec is None:
            raise KeyError(section_id)
        if sec.mutability is not Mutability.AUTHORIAL_PASSTHROUGH:
            raise SectionMutabilityError(
                f"section {section_id} is {sec.mutability.value}; cannot attach block"
            )
        bucket = self.section_to_blocks.setdefault(section_id, [])
        if block.id not in bucket:
            bucket.append(block.id)

    def attach_example(self, example: Example) -> None:
        self.examples[example.id] = example

    def rules_for_section(self, section_id: str) -> list[Rule]:
        # Authorially placed rules first, in source order; unplaced (learned)
        # rules after, by corroboration.
        rules = [self.rules[i] for i in self.section_to_rules.get(section_id, []) if i in self.rules]
        placements = self.section_rule_placements.get(section_id, {})

        def key(r: Rule):
            p = placements.get(r.id)
            placed = p is not None and p.order is not None
            return (0, p.order, 0) if placed else (1, 0, -r.corroboration_count)

        rules.sort(key=key)
        return rules

    def rule_placements_for_section(self, section_id: str) -> list[RulePlacement]:
        return list(self.section_rule_placements.get(section_id, {}).values())

    def blocks_for_section(self, section_id: str) -> list[ContentBlock]:
        # Active blocks only; a block with no status (legacy) reads as active.
        # Normalisation happens on the returned view, never on the stored object
        # (a read must not write; the Neo4j adapter coalesces at query time).
        out: list[ContentBlock] = []
        for i in self.section_to_blocks.get(section_id, []):
            b = self.content_blocks.get(i)
            if b is None:
                continue
            if (b.status or BlockStatus.ACTIVE) is BlockStatus.ACTIVE:
                out.append(replace(b, status=BlockStatus.ACTIVE) if b.status is None else b)
        return out

    def get_content_block(self, block_id: str) -> ContentBlock | None:
        # Unlike blocks_for_section, no status filter: a review item can name a
        # block that has since been superseded, and the reviewer still needs the
        # text the verdict was about.
        return self.content_blocks.get(block_id)

    def section_for_block(self, block_id: str) -> Section | None:
        # section_to_blocks is the forward index; this walks it back. Linear over
        # sections, which is fine for a fake (the Neo4j adapter follows the edge).
        for section_id, block_ids in self.section_to_blocks.items():
            if block_id in block_ids:
                return self.sections.get(section_id)
        return None

    def sections_with_multiple_active_blocks(self, tenant_id: str) -> list[str]:
        # Custody invariant (Section 7.3): an authorial section holds exactly one
        # active block. Return the ids of this tenant's sections that hold more.
        violating: list[str] = []
        for section in self.sections.values():
            if section.tenant_id != tenant_id:
                continue
            if len(self.blocks_for_section(section.id)) > 1:
                violating.append(section.id)
        return violating

    def active_blocks_without_body(self, tenant_id: str) -> list[str]:
        # Empty text on a block that publishes; see the port for why these exist.
        return [b.id for b in self.content_blocks.values()
                if b.tenant_id == tenant_id
                and b.status is BlockStatus.ACTIVE
                and not b.body]

    def blocks_revised_for_rule(self, rule_id: str) -> list[ContentBlock]:
        txn_ids = {e.to_id for e in self.edges
                   if e.type is EdgeType.DERIVED_FROM and e.from_id == rule_id}
        if not txn_ids:
            return []
        block_ids = [e.from_id for e in self.edges
                     if e.type is EdgeType.DERIVED_FROM and e.to_id in txn_ids
                     and e.from_id in self.content_blocks]
        out: list[ContentBlock] = []
        for bid in dict.fromkeys(block_ids):
            b = self.content_blocks[bid]
            if (b.status or BlockStatus.ACTIVE) is BlockStatus.ACTIVE:
                out.append(b)
        return out

    def supersede_block(
        self, old_block_id: str, new_block: ContentBlock, section_id: str,
        transaction_id: str | None = None,
    ) -> None:
        # Upsert the new block as active and attach it to the section.
        new_block.status = BlockStatus.ACTIVE
        self.upsert_content_block(new_block)
        self.attach_block(new_block, section_id=section_id)
        # Mark the predecessor superseded; it stays attached for history.
        old = self.content_blocks.get(old_block_id)
        if old is not None:
            old.status = BlockStatus.SUPERSEDED
        # Lineage edges: SUPERSEDES between blocks, DERIVED_FROM to the txn.
        self.attach_edge(Edge(type=EdgeType.SUPERSEDES,
                              from_id=new_block.id, to_id=old_block_id))
        if transaction_id is not None:
            self.attach_edge(Edge(type=EdgeType.DERIVED_FROM,
                                  from_id=new_block.id, to_id=transaction_id))

    def upsert_content_block(self, block: ContentBlock) -> None:
        self.content_blocks[block.id] = block

    def upsert_artefact(self, artefact: Artefact, skill_id: str, path: str) -> None:
        self.artefacts[artefact.id] = artefact
        bucket = self.skill_to_artefacts.setdefault(skill_id, [])
        bucket[:] = [(aid, p) for (aid, p) in bucket if p != path]
        bucket.append((artefact.id, path))

    def artefacts_for_skill(self, skill_id: str) -> list[tuple[Artefact, str]]:
        return [(self.artefacts[aid], p) for (aid, p) in self.skill_to_artefacts.get(skill_id, [])]

    def upsert_publication(self, publication: Publication) -> None:
        self.publications[(publication.tenant_id, publication.source_ref)] = publication

    def get_publication(self, tenant_id: str, source_ref: str) -> Publication | None:
        return self.publications.get((tenant_id, source_ref))

    def publications_for_skill(self, tenant_id: str, skill_id: str) -> list[Publication]:
        return [p for (tid, _), p in self.publications.items()
                if tid == tenant_id and p.skill_id == skill_id]

    def delete_publications_for_skill(self, tenant_id: str, skill_id: str) -> None:
        self.publications = {
            key: p for key, p in self.publications.items()
            if not (key[0] == tenant_id and p.skill_id == skill_id)
        }

    def delete_publication(self, tenant_id: str, source_ref: str) -> None:
        self.publications.pop((tenant_id, source_ref), None)

    def append_skill_version(self, version: SkillVersion) -> None:
        self._skill_versions.append(version)

    def _versions_of(self, tenant_id: str, skill_id: str) -> list[SkillVersion]:
        rows = [v for v in self._skill_versions
                if v.tenant_id == tenant_id and v.skill_id == skill_id]
        rows.sort(key=lambda v: v.at, reverse=True)
        return rows

    def skill_versions(self, tenant_id: str, skill_id: str, limit: int = 50,
                       before: datetime | None = None) -> list[SkillVersion]:
        rows = self._versions_of(tenant_id, skill_id)
        if before is not None:
            rows = [v for v in rows if v.at < before]
        return rows[:limit]

    def get_skill_version(self, version_id: str) -> SkillVersion | None:
        return next((v for v in self._skill_versions if v.id == version_id), None)

    def recent_skill_changes(self, tenant_id: str, limit: int = 8) -> list[SkillChange]:
        rows = [v for v in self._skill_versions
                if v.tenant_id == tenant_id and v.skill_id in self.skills
                and self.skills[v.skill_id].tenant_id == tenant_id]
        rows.sort(key=lambda v: (v.at, v.id), reverse=True)
        return [SkillChange(id=v.id, skill_id=v.skill_id,
                            skill_name=self.skills[v.skill_id].name, at=v.at,
                            cause=v.cause, revision=v.revision,
                            actor_person_id=v.actor_person_id, detail=v.detail)
                for v in rows[:max(0, limit)]]

    def latest_skill_version(self, tenant_id: str, skill_id: str) -> SkillVersion | None:
        rows = self._versions_of(tenant_id, skill_id)
        return rows[0] if rows else None

    def trim_skill_versions(self, tenant_id: str, skill_id: str, keep: int) -> int:
        rows = self._versions_of(tenant_id, skill_id)
        if len(rows) <= keep:
            return 0
        oldest = rows[-1]
        doomed = {v.id for v in rows[keep:]} - {oldest.id}
        self._skill_versions = [v for v in self._skill_versions
                                if v.id not in doomed]
        return len(doomed)

    def record_usage_event(self, event: UsageEvent) -> None:
        self.usage[event.id] = event

    def usage_events(self, tenant_id: str, skill_id: str | None = None,
                     since: datetime | None = None) -> list[UsageEvent]:
        out = [e for e in self.usage.values()
               if e.tenant_id == tenant_id
               and (skill_id is None or e.skill_id == skill_id)
               and (since is None or e.timestamp >= since)]
        out.sort(key=lambda e: e.timestamp)
        return out

    def set_publish_block(self, tenant_id: str, reasons: list[str], source: str) -> None:
        self.publish_blocks[tenant_id] = PublishBlock(
            tenant_id=tenant_id, reasons=list(reasons), source=source,
            set_at=datetime.now(timezone.utc),
        )

    def clear_publish_block(self, tenant_id: str) -> None:
        self.publish_blocks.pop(tenant_id, None)

    def get_publish_block(self, tenant_id: str) -> PublishBlock | None:
        return self.publish_blocks.get(tenant_id)

    def observe_rule_in(self, rule_id: str, transaction_id: str, source_ref: str) -> None:
        with self._workflow_mutex:
            bucket = self.observations.setdefault(rule_id, set())
            new_source = source_ref not in {sr for (_t, sr) in bucket}
            bucket.add((transaction_id, source_ref))
            rule = self.rules.get(rule_id)
            if rule is not None and new_source:
                rule.corroboration_count += 1

    def examples_for_section(self, section_id: str) -> list[Example]:
        out = [e for e in self.examples.values() if e.parent_section_id == section_id]
        out.sort(key=lambda e: (e.order is None, e.order if e.order is not None else 0))
        return out

    def upsert_cross_skill_edge(
        self, edge_type: EdgeType, from_rule_id: str, to_rule_id: str,
        confidence: float, kind: str | None = None,
    ) -> None:
        for i, (et, frm, to, _conf, _k) in enumerate(self.cross_edges):
            if et is edge_type and frm == from_rule_id and to == to_rule_id:
                self.cross_edges[i] = (edge_type, from_rule_id, to_rule_id, confidence, kind)
                return
        self.cross_edges.append((edge_type, from_rule_id, to_rule_id, confidence, kind))

    def cross_skill_neighbours(self, rule_id: str) -> list[tuple[EdgeType, str, float]]:
        return [(et, to, conf) for (et, frm, to, conf, _k) in self.cross_edges if frm == rule_id]
