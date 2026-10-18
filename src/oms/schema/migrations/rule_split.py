"""Schema 3: every rule shared between skills becomes one copy per skill.

Before schema 3 a rule could belong to several skills, or be placed in a
section of a skill it did not belong to. From schema 3 a rule belongs to one
skill and is placed only in that skill's sections (one rule, one skill), and
both stores refuse any write that would share a rule again.

The split is staged and resumable like `source_identity`: `rule_split_fenced`
(writers refuse), `rule_splitting` (one write transaction per shared rule),
`rule_split_verified`, then `ready` at schema 3. Each rule is split whole or
not at all, so a stop part way leaves nothing half split and the next start
continues. A copy carries its original's text, places, order and id prefix
(`oms.domain.copies`), so every published file stays byte for byte the same.

`plan_split` and `format_report` only read: `python -m oms.schema.split_rules
--report` shows what the split would do and writes nothing.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from oms.activity.models import AdminAction, AdminEvent
from oms.adapters.neo4j.activity import Neo4jAdminEventStore
from oms.adapters.neo4j.review_queue import Neo4jReviewQueue
from oms.adapters.neo4j.store import Neo4jGraphStore
from oms.community.matching import exact_text
from oms.domain.copies import CopyCause, RuleSlot, split_rule_id
from oms.domain.identity import SkillRef
from oms.domain.models import Edge, ReviewItem, Skill, Transaction
from oms.domain.types import EdgeType, SignalType, SkillVersionCause, Verdict
from oms.ports.graph_store import SectionMutabilityError
from oms.schema.metadata import CORE_VERSION, RULE_SPLIT_STAGES, SCHEMA_VERSION, SchemaCompatibilityError
from oms.skills.copies import link_copies

if TYPE_CHECKING:
    from neo4j import ManagedTransaction

    from oms.ports.graph_store import GraphStore
    from oms.ports.source_store import SourceRepository
    from oms.skills.history import SkillHistory

logger = logging.getLogger(__name__)

NO_LONGER_RAISED = "no_longer_raised"
NO_LONGER_RAISED_REASON = "no longer raised"
PENDING_CAPTURES = "rule_split_pending_captures"
SPLIT_COUNTS = "rule_split_counts"

_HOLDS = frozenset({"pending_rule", "injection", "control_plane"})
_RELATIONS = frozenset({"conflict", "supersession", "duplicate_low_confidence"})
_REASONS = {"skill_hint": "named by the first contribution", "import": "the skill whose file it came from",
            "source": "the skill whose source tracks it", "lowest_key": "first skill by id"}

# A rule with more than one skill: BELONGS_TO skills, plus the skills whose
# sections place it without a BELONGS_TO (spec 8.2).
_SHARED = (
    "MATCH (r:Rule) "
    "OPTIONAL MATCH (r)-[:BELONGS_TO]->(s:Skill) WHERE s.tenant_id = r.tenant_id "
    "WITH r, collect(DISTINCT s.id) AS owned "
    "OPTIONAL MATCH (p:Section)-[:CONTAINS_RULE]->(r) WHERE p.tenant_id = r.tenant_id "
    "WITH r, owned, owned + [x IN collect(DISTINCT p.skill_id) WHERE NOT x IN owned] AS skills "
    "WHERE size(skills) > 1 "
    "RETURN r.tenant_id AS tenant, r.id AS rule, r.status AS status, owned, skills ORDER BY tenant, rule")


@dataclass(frozen=True)
class CopyPlan:
    skill_id: str
    copy_id: str
    belongs: bool
    belongs_properties: dict[str, object]
    slots: tuple[RuleSlot, ...]


@dataclass(frozen=True)
class RulePlan:
    tenant_id: str
    rule_id: str
    status: str
    keeper: str
    keeper_reason: str
    copies: tuple[CopyPlan, ...]
    items_copied: tuple[str, ...]
    items_moved: tuple[str, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TenantPlan:
    tenant_id: str
    rules: tuple[RulePlan, ...]
    divergence_items: tuple[str, ...] = field(default_factory=tuple)

    @property
    def skills_affected(self) -> tuple[str, ...]:
        return tuple(sorted({skill for rule in self.rules
                             for skill in (rule.keeper, *(c.skill_id for c in rule.copies))}))

    @property
    def copies(self) -> int:
        return sum(len(rule.copies) for rule in self.rules)

    @property
    def held_items(self) -> int:
        return len({item for rule in self.rules for item in (*rule.items_copied, *rule.items_moved)})

    def counts(self) -> dict[str, int]:
        return {"shared_rules": len(self.rules), "skills_affected": len(self.skills_affected),
                "copies": self.copies, "held_items": self.held_items,
                "divergence_items": len(self.divergence_items)}


class _ReadOnlyDriver:
    """Hands out read sessions only, so a report cannot write even by mistake."""
    def __init__(self, driver) -> None:
        self._driver = driver

    def session(self, **kwargs):
        kwargs["default_access_mode"] = "READ"
        return self._driver.session(**kwargs)


def choose_keeper(skills: Sequence[Skill], *, hint: str | None, import_source_ref: str | None,
                  source_claimed: frozenset[str]) -> tuple[str, str]:
    """(skill id, reason): the skill that keeps a shared rule's id (spec 8.3, plan D2)."""
    ordered = sorted(skills, key=lambda skill: SkillRef(skill.tenant_id, skill.id).storage_key)
    if hint:
        wanted = exact_text(hint)
        for skill in ordered:
            if wanted in (exact_text(skill.id), exact_text(skill.name)):
                return skill.id, "skill_hint"
    if import_source_ref:
        for skill in ordered:
            if skill.import_source_ref == import_source_ref:
                return skill.id, "import"
    claimed = [skill for skill in ordered if skill.id in source_claimed]
    if len(claimed) == 1:
        return claimed[0].id, "source"
    return ordered[0].id, "lowest_key"


def _earliest(lineage: Sequence[Transaction], tenant: str) -> Transaction | None:
    """The rule's first contribution: the earliest of its DERIVED_FROM
    transactions in its own tenant (`lineage` reads oldest first). Two with
    one timestamp are told apart by id, so a report and the split it
    describes always choose the same keeper."""
    own = [txn for txn in lineage if txn.tenant_id == tenant]
    if not own:
        return None
    return min((txn for txn in own if txn.timestamp == own[0].timestamp), key=lambda txn: txn.id)


def _names(item: ReviewItem, rule_id: str) -> bool:
    return rule_id in (item.subject_id, item.other_id, item.rule_id) or item.other_id == f"rule:{rule_id}"


def _polarity_transaction(item: ReviewItem, rule_id: str) -> str | None:
    prefix = f"polarity-{rule_id}-"
    return item.id[len(prefix):] if item.id.startswith(prefix) else None


def _treatment(item: ReviewItem, *, rule_id: str, keeper: str, copy_skills: frozenset[str],
               skills_of: Callable[[str], frozenset[str]], is_constraint: Callable[[str], bool],
               section_skill: Callable[[str], str | None], block_skill: Callable[[str], str | None],
               import_skill: Callable[[str], str | None]) -> dict[str, str] | None:
    """`item_treatment`, or None when no row of the plan's table covers the item."""
    kind = item.kind
    if kind in ("cross_skill_divergence", "skill_assignment"):
        return {}
    if item.subject_id == rule_id and (kind in _HOLDS or (
            kind == "conflict" and item.other_id is not None and is_constraint(item.other_id))):
        return {skill: "copy" for skill in copy_skills}
    if kind == "withdrawn_repeat" and item.subject_id == rule_id:
        successor = skills_of(item.other_id) if item.other_id else frozenset()
        return {skill: ("copy" if skill in successor else "pending") for skill in copy_skills}
    home: str | None = None
    if kind in _RELATIONS and rule_id in (item.subject_id, item.other_id):
        other = item.other_id if item.subject_id == rule_id else item.subject_id
        theirs = skills_of(other) if other else frozenset()
        if keeper not in theirs:
            home = next((skill for skill in sorted(copy_skills) if skill in theirs), None)
    elif kind == "block_conflict" and item.subject_id == rule_id and item.other_id:
        home = block_skill(item.other_id)
    elif kind == "block_revision" and item.rule_id == rule_id and item.other_id:
        home = section_skill(item.other_id)
    elif kind == "proposed_edit" and item.other_id == f"rule:{rule_id}":
        home = item.subject_id
    elif kind in ("removal", "polarity_conflict") and item.subject_id == rule_id:
        transaction = item.transaction_id or _polarity_transaction(item, rule_id)
        home = import_skill(transaction) if transaction else None
    else:
        return None
    return {home: "move"} if home in copy_skills else {}


def item_treatment(item: ReviewItem, *, rule_id: str, keeper: str, copy_skills: frozenset[str],
                   skills_of: Callable[[str], frozenset[str]], is_constraint: Callable[[str], bool],
                   section_skill: Callable[[str], str | None], block_skill: Callable[[str], str | None],
                   import_skill: Callable[[str], str | None]) -> dict[str, str]:
    """What happens to one open item naming `rule_id`, per copy skill (spec 8.5,
    plan "Item treatment"): "copy" (the copy gets its own item), "pending" (the
    copy gets a pending_rule item) or "move" (the item moves to that skill's
    copy). An empty answer means the item stays as it is."""
    treatment = _treatment(item, rule_id=rule_id, keeper=keeper, copy_skills=copy_skills,
                           skills_of=skills_of, is_constraint=is_constraint, section_skill=section_skill,
                           block_skill=block_skill, import_skill=import_skill)
    return treatment if treatment is not None else {}


class _Resolvers:
    """The reads `item_treatment` needs, over one store and tenant."""
    def __init__(self, store: GraphStore, tenant_id: str) -> None:
        self._store, self._tenant = store, tenant_id
        self._constraints: frozenset[str] | None = None
        self._imports: dict[str, str] | None = None

    def skills_of(self, rule_id: str) -> frozenset[str]:
        return self._store.content_owners([rule_id], [], tenant_id=self._tenant).get(("rule", rule_id), frozenset())

    def is_constraint(self, node_id: str) -> bool:
        if self._constraints is None:
            self._constraints = frozenset(c.id for c in self._store.all_constraints(self._tenant))
        return node_id in self._constraints

    def section_skill(self, section_id: str) -> str | None:
        section = self._store.get_section(section_id, tenant_id=self._tenant)
        return section.skill_id if section is not None else None

    def block_skill(self, block_id: str) -> str | None:
        section = self._store.section_for_block(block_id, tenant_id=self._tenant)
        return section.skill_id if section is not None else None

    def import_skill(self, transaction_id: str) -> str | None:
        if self._imports is None:
            # First skill by storage key, so two skills naming one file always resolve alike.
            self._imports = {}
            for skill in sorted(self._store.skills_for_tenant(self._tenant),
                                key=lambda skill: SkillRef(skill.tenant_id, skill.id).storage_key):
                if skill.import_source_ref:
                    self._imports.setdefault(skill.import_source_ref, skill.id)
        txn = self._store.get_transaction(transaction_id)
        if txn is None or txn.tenant_id != self._tenant or not txn.source_ref:
            return None
        return self._imports.get(txn.source_ref)

    def callables(self) -> dict[str, Callable]:
        return {"skills_of": self.skills_of, "is_constraint": self.is_constraint,
                "section_skill": self.section_skill, "block_skill": self.block_skill,
                "import_skill": self.import_skill}


def _membership(store: Neo4jGraphStore, tenant: str, rule_id: str, skill_id: str) -> dict[str, object]:
    with store._driver.session() as session:
        row = session.run("MATCH (:Rule {id:$rule, tenant_id:$tenant})-[b:BELONGS_TO]->(:Skill {id:$skill, tenant_id:$tenant}) "
                          "RETURN properties(b) AS props", rule=rule_id, skill=skill_id, tenant=tenant).single()
    return dict(row["props"]) if row is not None else {}


def _slots(store: Neo4jGraphStore, tenant: str, rule_id: str, skill_id: str) -> tuple[RuleSlot, ...]:
    with store._driver.session() as session:
        rows = list(session.run(
            "MATCH (s:Section {skill_id:$skill, tenant_id:$tenant})-[c:CONTAINS_RULE]->(:Rule {id:$rule, tenant_id:$tenant}) "
            "RETURN s.id AS section, c.order AS ord, c.group AS grp ORDER BY section",
            rule=rule_id, skill=skill_id, tenant=tenant))
    return tuple(RuleSlot(row["section"], row["ord"], row["grp"]) for row in rows)


def _placements_without_skill(store: Neo4jGraphStore, tenant: str, rule_id: str) -> tuple[tuple[str, str], ...]:
    """(section, skill) for every section placing the rule whose skill has no
    Skill node. Only a hand-edited graph holds one, and no copy can go there,
    so the split refuses the rule until the placement or the skill is fixed."""
    with store._driver.session() as session:
        rows = list(session.run(
            "MATCH (p:Section {tenant_id:$tenant})-[:CONTAINS_RULE]->(:Rule {id:$rule, tenant_id:$tenant}) "
            "WHERE p.skill_id IS NOT NULL AND NOT EXISTS { MATCH (:Skill {id:p.skill_id, tenant_id:$tenant}) } "
            "RETURN p.id AS section, p.skill_id AS skill ORDER BY section", rule=rule_id, tenant=tenant))
    return tuple((row["section"], row["skill"]) for row in rows)


def _prefix_siblings(store: Neo4jGraphStore, tenant: str, rule_id: str, copy: CopyPlan) -> tuple[str, ...]:
    """The rules of the copy's skill whose ids sort between the rule's and the
    copy's. A copy sorts where its original did only when there are none
    (`oms.domain.copies`): where one of them ties with the copy, on count or
    on placement order, the two change places in the skill's file."""
    with store._driver.session() as session:
        rows = list(session.run(
            "MATCH (y:Rule {tenant_id:$tenant}) WHERE y.id STARTS WITH $rule AND y.id <> $rule "
            "AND (EXISTS { MATCH (y)-[:BELONGS_TO]->(:Skill {id:$skill, tenant_id:$tenant}) } "
            "OR EXISTS { MATCH (:Section {skill_id:$skill, tenant_id:$tenant})-[:CONTAINS_RULE]->(y) }) "
            "RETURN y.id AS id", rule=rule_id, skill=copy.skill_id, tenant=tenant))
    # Compared here, as the renderer's own sort compares them.
    return tuple(sorted(row["id"] for row in rows if rule_id < row["id"] < copy.copy_id))


def plan_rule(store: Neo4jGraphStore, sources: SourceRepository, pending: Sequence[ReviewItem],
              row: dict) -> RulePlan:
    tenant, rule_id = row["tenant"], row["rule"]
    skills = [skill for skill in (store.get_skill(skill_id, tenant_id=tenant) for skill_id in row["skills"])
              if skill is not None]
    first = _earliest(store.lineage(rule_id), tenant)
    claimed = frozenset(skill.id for skill in skills if any(
        rule_id in claim.entity_ids for claim in sources.ownership_for_skill(SkillRef(tenant, skill.id))))
    keeper, reason = choose_keeper(
        skills, hint=first.skill_hint if first is not None else None,
        import_source_ref=(first.source_ref if first is not None
                           and first.signal_type is SignalType.SKILL_IMPORT else None),
        source_claimed=claimed)
    owned = set(row["owned"])
    copies = tuple(
        CopyPlan(skill_id=skill.id, copy_id=split_rule_id(rule_id, SkillRef(tenant, skill.id)),
                 belongs=skill.id in owned,
                 belongs_properties=_membership(store, tenant, rule_id, skill.id) if skill.id in owned else {},
                 slots=_slots(store, tenant, rule_id, skill.id))
        for skill in sorted(skills, key=lambda s: SkillRef(tenant, s.id).storage_key) if skill.id != keeper)
    resolvers = _Resolvers(store, tenant)
    copied, moved, kept = [], [], []
    for item in pending:
        if not _names(item, rule_id):
            continue
        treatment = _treatment(item, rule_id=rule_id, keeper=keeper,
                               copy_skills=frozenset(c.skill_id for c in copies), **resolvers.callables())
        if treatment is None:
            # The plan's last row: no treatment covers it, so it stays and the report lists it.
            kept.append(item)
            continue
        if any(action in ("copy", "pending") for action in treatment.values()):
            copied.append(item.id)
        if "move" in treatment.values():
            moved.append(item.id)
    warnings = (*(f"placed in section {section}, whose skill {skill} does not exist: the split refuses "
                  "this rule until that placement is deleted or the skill is restored"
                  for section, skill in _placements_without_skill(store, tenant, rule_id)),
                *(f"{skill} tracks this rule from a source but gets a copy"
                  for skill in sorted(claimed - {keeper})),
                *(f"in {copy.skill_id}, {sibling} sorts between this rule and its copy, so where the two tie "
                  "they change places in the published file"
                  for copy in copies for sibling in _prefix_siblings(store, tenant, rule_id, copy)),
                *(f"open item {item.id} ({item.kind}) stays with the original in {keeper}"
                  for item in sorted(kept, key=lambda item: item.id)))
    return RulePlan(tenant_id=tenant, rule_id=rule_id, status=row["status"], keeper=keeper,
                    keeper_reason=reason, copies=copies, items_copied=tuple(sorted(copied)),
                    items_moved=tuple(sorted(moved)), warnings=warnings)


def plan_split(driver) -> tuple[TenantPlan, ...]:
    """What the split would do, per tenant. Reads only."""
    from oms.sources.mutation import source_repository
    reader = _ReadOnlyDriver(driver)
    store, queue = Neo4jGraphStore(reader), Neo4jReviewQueue(reader)
    sources = source_repository(store)
    with reader.session() as session:
        shared = [dict(row) for row in session.run(_SHARED)]
        tenants = sorted({row["tenant"] for row in session.run(
            "MATCH (n) WHERE (n:Rule OR n:ReviewItem) AND n.tenant_id IS NOT NULL "
            "RETURN DISTINCT n.tenant_id AS tenant")})
    plans = []
    for tenant in tenants:
        pending = queue.pending(tenant)
        divergence = tuple(sorted(item.id for item in pending if item.kind == "cross_skill_divergence"))
        rules = tuple(plan_rule(store, sources, pending, row) for row in shared if row["tenant"] == tenant)
        if rules or divergence:
            plans.append(TenantPlan(tenant_id=tenant, rules=rules, divergence_items=divergence))
    return tuple(plans)


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def format_report(plans: Sequence[TenantPlan]) -> str:
    lines = ["Rule split report: nothing has been changed."]
    if not plans:
        lines.append("No rule is shared between skills, and no divergence item is open.")
    for plan in plans:
        lines.append(f"Tenant {plan.tenant_id}: {_count(len(plan.rules), 'shared rule')}, "
                     f"{_count(len(plan.skills_affected), 'skill')} affected, "
                     f"{_count(plan.copies, 'copy', 'copies')} to make, "
                     f"{_count(plan.held_items, 'held item')} affected, "
                     f"{_count(len(plan.divergence_items), 'divergence item')} to close")
        for rule in plan.rules:
            lines.append(f"  {rule.rule_id}: kept by {rule.keeper} ({_REASONS[rule.keeper_reason]}); "
                         f"copies for {', '.join(copy.skill_id for copy in rule.copies)}")
            lines.extend(f"    warning: {warning}" for warning in rule.warnings)
    return "\n".join(lines) + "\n"


def _bound(tx: ManagedTransaction) -> tuple[Neo4jGraphStore, Neo4jReviewQueue]:
    from oms.community.repository import _TransactionDriver
    driver = _TransactionDriver(tx)
    return Neo4jGraphStore(driver), Neo4jReviewQueue(driver)


def _skills_of(tx: ManagedTransaction, tenant: str, rule_id: str) -> tuple[list[str], frozenset[str]]:
    """The rule's skills (its BELONGS_TO skills, then the skills whose
    sections place it), and its BELONGS_TO skills alone."""
    row = tx.run(
        "MATCH (r:Rule {id:$rule, tenant_id:$tenant}) "
        "OPTIONAL MATCH (r)-[:BELONGS_TO]->(s:Skill {tenant_id:$tenant}) "
        "WITH r, collect(DISTINCT s.id) AS owned "
        "OPTIONAL MATCH (p:Section {tenant_id:$tenant})-[:CONTAINS_RULE]->(r) "
        # Neo4j 5 refuses an aggregate beside a variable it does not group on,
        # so `owned` is carried as a grouping key first.
        "WITH owned, collect(DISTINCT p.skill_id) AS placed "
        "RETURN owned, owned + [x IN placed WHERE NOT x IN owned] AS skills",
        rule=rule_id, tenant=tenant).single()
    return (list(row["skills"]), frozenset(row["owned"])) if row is not None else ([], frozenset())


def _as_planned(store: Neo4jGraphStore, plan: RulePlan, skills: Sequence[str], owned: frozenset[str]) -> bool:
    """Whether the graph still holds what the plan read: the same skills and,
    in each copy's skill, the same membership and the same places. A plan
    that missed a membership or a place would leave the original in that
    skill beside its copy, still shared, and every later split would refuse
    it; one that missed a change would give the copy what the original no
    longer had."""
    if set(skills) != {plan.keeper, *(copy.skill_id for copy in plan.copies)}:
        return False
    for copy in plan.copies:
        if (copy.skill_id in owned) is not copy.belongs:
            return False
        if copy.belongs and _membership(store, plan.tenant_id, plan.rule_id, copy.skill_id) != copy.belongs_properties:
            return False
        if _slots(store, plan.tenant_id, plan.rule_id, copy.skill_id) != copy.slots:
            return False
    return True


# Rule-to-rule edges that follow the skill their other end is in (plan "Edge
# treatment"). An incoming SUPERSEDES is the exception: it is copied to every
# copy, so no member of a superseded rule comes back into force (D3).
_FOLLOW = ("CONFLICTS_WITH", "RELATED_TO", "DUPLICATE_OF", "SUPERSEDES")


def _move_relations(tx: ManagedTransaction, tenant: str, rule_id: str, keeper: str, copies: dict[str, str]) -> None:
    members = [rule_id, *copies.values()]
    for kind in _FOLLOW:
        for outgoing in (True, False):
            pattern = (f"(o:Rule {{id:$rule, tenant_id:$tenant}})-[e:{kind}]->(x:Rule {{tenant_id:$tenant}})"
                       if outgoing else
                       f"(x:Rule {{tenant_id:$tenant}})-[e:{kind}]->(o:Rule {{id:$rule, tenant_id:$tenant}})")
            rows = list(tx.run(
                f"MATCH {pattern} WHERE NOT x.id IN $members "
                "OPTIONAL MATCH (x)-[:BELONGS_TO]->(xs:Skill {tenant_id:$tenant}) "
                "WITH e, x, collect(DISTINCT xs.id) AS owned "
                "OPTIONAL MATCH (p:Section {tenant_id:$tenant})-[:CONTAINS_RULE]->(x) "
                "WITH e, x, owned, collect(DISTINCT p.skill_id) AS placed "
                "RETURN elementId(e) AS edge, x.id AS other, properties(e) AS props, "
                "owned + placed AS skills",
                rule=rule_id, tenant=tenant, members=members))
            for row in rows:
                theirs = set(row["skills"])
                if kind == "SUPERSEDES" and not outgoing:
                    targets, remove = list(copies.values()), False
                else:
                    targets = [copy_id for skill, copy_id in copies.items() if skill in theirs]
                    remove = bool(targets) and keeper not in theirs
                for copy_id in targets:
                    merge = f"MERGE (c)-[n:{kind}]->(x) " if outgoing else f"MERGE (x)-[n:{kind}]->(c) "
                    tx.run("MATCH (c:Rule {id:$copy, tenant_id:$tenant}), (x:Rule {id:$other, tenant_id:$tenant}) "
                           + merge + "SET n += $props",
                           copy=copy_id, other=row["other"], tenant=tenant, props=dict(row["props"])).consume()
                if remove:
                    tx.run("MATCH ()-[e]->() WHERE elementId(e) = $edge DELETE e", edge=row["edge"]).consume()


def _item_copy_id(item_id: str, rule_id: str, copy_id: str) -> str:
    if rule_id in item_id:
        return item_id.replace(rule_id, copy_id, 1)
    digest = hashlib.sha256("\x1f".join((item_id, copy_id)).encode("utf-8")).hexdigest()[:12]
    return f"{item_id}-split-{digest}"


def _repoint(item: ReviewItem, rule_id: str, copy_id: str) -> ReviewItem:
    other = item.other_id
    if other == rule_id:
        other = copy_id
    elif other == f"rule:{rule_id}":
        other = f"rule:{copy_id}"
    return replace(item, subject_id=copy_id if item.subject_id == rule_id else item.subject_id,
                   other_id=other, rule_id=copy_id if item.rule_id == rule_id else item.rule_id)


def _treat_items(store: Neo4jGraphStore, queue: Neo4jReviewQueue, tenant: str, rule_id: str, keeper: str,
                 copies: dict[str, str]) -> int:
    resolvers = _Resolvers(store, tenant)
    touched = 0
    # In id order: where two items would give a copy one new id, the same one
    # is written on every run.
    for item in sorted(queue.pending(tenant), key=lambda item: item.id):
        if not _names(item, rule_id):
            continue
        treatment = item_treatment(item, rule_id=rule_id, keeper=keeper, copy_skills=frozenset(copies),
                                   **resolvers.callables())
        for skill, action in sorted(treatment.items()):
            copy_id = copies[skill]
            if action == "copy":
                queue.enqueue_once(replace(_repoint(item, rule_id, copy_id),
                                           id=_item_copy_id(item.id, rule_id, copy_id)))
            elif action == "pending":
                queue.enqueue_once(ReviewItem(
                    id=f"review-pending-{copy_id}", kind="pending_rule", subject_id=copy_id, other_id=None,
                    verdict=Verdict.AMBIGUOUS,
                    reason=f"Split from {rule_id}: this skill's copy waits for its own decision.",
                    tenant_id=tenant, priority=item.priority, group_id=item.group_id,
                    transaction_id=item.transaction_id, created_at=item.created_at))
            elif action == "move":
                queue.enqueue(_repoint(item, rule_id, copy_id))
        touched += bool(treatment)
    return touched


def _lock(tx: ManagedTransaction) -> None:
    """Take the metadata node's write lock before reading what will be written back."""
    tx.run("MERGE (m:OMSMetadata {id:'core'}) SET m._migration_lock=true REMOVE m._migration_lock").consume()


def _record(tx: ManagedTransaction, tenant: str, *, affected: Sequence[str], captures: Sequence[str], copies: int,
            items: int, divergence: int = 0, rules: int = 1) -> None:
    # Locked first: two splitters running at once must not lose each other's counts.
    _lock(tx)
    row = tx.run(f"MATCH (m:OMSMetadata {{id:'core'}}) RETURN m.{SPLIT_COUNTS} AS counts, "
                 f"m.{PENDING_CAPTURES} AS pending").single()
    counts = json.loads(row["counts"]) if row is not None and row["counts"] else {}
    mine = counts.setdefault(tenant, {"shared_rules": 0, "copies": 0, "held_items": 0,
                                      "divergence_closed": 0, "skills": []})
    mine["shared_rules"] += rules
    mine["copies"] += copies
    mine["held_items"] += items
    mine["divergence_closed"] += divergence
    mine["skills"] = sorted(set(mine["skills"]) | set(affected))
    # Sorted, so the list does not depend on the order rules were split in.
    known = set(row["pending"] or []) if row is not None else set()
    pending = sorted(known | {SkillRef(tenant, skill).storage_key for skill in captures})
    tx.run(f"MATCH (m:OMSMetadata {{id:'core'}}) SET m.{SPLIT_COUNTS} = $counts, m.{PENDING_CAPTURES} = $pending",
           counts=json.dumps(counts, sort_keys=True), pending=pending).consume()


def split_rule(tx: ManagedTransaction, plan: RulePlan, *, now: datetime) -> bool:
    """Split one shared rule in the caller's write transaction (spec 8.4, 8.5).

    False when it is no longer shared (split by an earlier or a concurrent
    run), so a resumed or repeated run changes nothing. A rule that is still
    shared while one of its copies exists, that a section of a missing skill
    places, or whose skills, memberships or places differ from the plan, is
    refused before anything is written. One the store could not take out of
    a copy's skill is refused after, and the transaction rolls it all back."""
    tenant, rule_id = plan.tenant_id, plan.rule_id
    if tx.run("MATCH (r:Rule {id:$id, tenant_id:$tenant}) SET r._split_lock = true REMOVE r._split_lock "
              "RETURN r.id AS id", id=rule_id, tenant=tenant).single() is None:
        return False
    skills, owned = _skills_of(tx, tenant, rule_id)
    if len(skills) <= 1:
        return False
    store, queue = _bound(tx)
    orphans = _placements_without_skill(store, tenant, rule_id)
    if orphans:
        places = ", ".join(f"section {section} of skill {skill}" for section, skill in orphans)
        raise SchemaCompatibilityError(f"rule {rule_id} in tenant {tenant} is placed in {places}, and no such "
                                       "skill exists, so no copy can go there: delete that placement or "
                                       "restore the skill")
    written = [copy.copy_id for copy in plan.copies if store.get_rule(copy.copy_id) is not None]
    if written:
        raise SchemaCompatibilityError(f"rule {rule_id} is still shared but copies of it exist: {', '.join(written)}")
    if not _as_planned(store, plan, skills, owned):
        raise SchemaCompatibilityError(f"rule {rule_id} changed after the split was planned; run the split again")
    copies: dict[str, str] = {}
    for copy in plan.copies:
        made = store.copy_rule(rule_id, copy.copy_id, tenant_id=tenant,
                               skill_id=copy.skill_id if copy.belongs else None, slots=copy.slots,
                               belongs_properties=copy.belongs_properties)
        tx.run("MATCH (c:Rule {id:$id, tenant_id:$tenant}) SET c.split_from = $original, c.split_at = $now",
               id=made.id, tenant=tenant, original=rule_id, now=now).consume()
        if copy.belongs:
            store.detach_edge(Edge(EdgeType.BELONGS_TO, rule_id, copy.skill_id), tenant_id=tenant)
        for slot in copy.slots:
            store.detach_rule(rule_id, slot.section_id, tenant_id=tenant)
        copies[copy.skill_id] = made.id
    # The store skips a detach whose ends lack the `Entity` label. Committing
    # then would leave a copy beside an original still in its skill, refused
    # on every later run, so the whole rule rolls back instead (spec 10).
    remaining, _ = _skills_of(tx, tenant, rule_id)
    if set(remaining) != {plan.keeper}:
        raise SchemaCompatibilityError(
            f"rule {rule_id} in tenant {tenant} is still in {', '.join(sorted(set(remaining) - {plan.keeper}))} "
            "after its copies were made, so it was not split: its BELONGS_TO edge could not be removed "
            "(the rule or that skill has no Entity label); add the label")
    _move_relations(tx, tenant, rule_id, plan.keeper, copies)
    items = _treat_items(store, queue, tenant, rule_id, plan.keeper, copies)
    link_copies(store, [rule_id, *copies.values()], cause=CopyCause.SPLIT, tenant_id=tenant)
    _record(tx, tenant, affected=[plan.keeper, *copies], captures=list(copies), copies=len(copies), items=items)
    logger.info("rule split: %s kept by %s; copies %s", rule_id, plan.keeper, ", ".join(copies.values()))
    return True


def close_divergence(tx: ManagedTransaction, tenant_id: str) -> int:
    """Close the tenant's open divergence items (spec 2, decision 6): OMS no
    longer raises them. The edges stay. One statement per item, which locks
    the item first and changes it only while it is open, so two splitters
    running at once close each item once and count it once."""
    closed = 0
    ids = [row["id"] for row in tx.run(
        "MATCH (n:ReviewItem {tenant_id:$tenant, kind:'cross_skill_divergence', resolved:false}) "
        "RETURN n.id AS id ORDER BY id", tenant=tenant_id)]
    for item_id in ids:
        row = tx.run("MATCH (n:ReviewItem {id:$id, tenant_id:$tenant}) "
                     "SET n._claim = true REMOVE n._claim WITH n WHERE n.resolved = false "
                     "SET n.reason=$reason, n.resolved=true, n.resolution=$resolution, "
                     "n.decided_by=null, n.decided_at=$now RETURN n.id AS id",
                     id=item_id, tenant=tenant_id, reason=NO_LONGER_RAISED_REASON,
                     resolution=NO_LONGER_RAISED, now=datetime.now(timezone.utc)).single()
        closed += row is not None
    if closed:
        _record(tx, tenant_id, affected=(), captures=(), copies=0, items=0, divergence=closed, rules=0)
    return closed


# -- The stages (spec 8.1): fenced, splitting, verified, then ready -----------

# What the split refuses a rule for: data it cannot split as it stands. Such
# a rule is skipped and named; anything else (a stop, a lost database) stops
# the run at once.
_REFUSALS = (SchemaCompatibilityError, SectionMutabilityError)


def _version(metadata: dict) -> int:
    return metadata.get("schema_version", metadata.get("core_version", 0))


def _metadata(runner) -> dict | None:
    row = runner.run("MATCH (m:OMSMetadata {id:'core'}) RETURN properties(m) AS metadata").single()
    return dict(row["metadata"]) if row else None


def _advance(driver, expected: str, stage: str) -> None:
    with driver.session() as session:
        session.run("MATCH (m:OMSMetadata {id:'core'}) WHERE m.migration_state=$expected "
                    "SET m.migration_state=$stage", expected=expected, stage=stage).consume()


def _stage(driver) -> str | None:
    with driver.session() as session:
        return (_metadata(session) or {}).get("migration_state")


def split_everything(driver, *, now: datetime | None = None) -> None:
    """Close every tenant's divergence items and split every shared rule, one
    write transaction each, then prove nothing is shared.

    A rule the split refuses (a hand-edited graph) does not stop the others.
    Every refusal is named in one error at the end, and the graph stays at
    `rule_splitting`, so the next start continues once the data is repaired."""
    now = now or datetime.now(timezone.utc)
    refused: list[str] = []
    for plan in plan_split(driver):
        with driver.session() as session:
            session.execute_write(lambda tx, tenant=plan.tenant_id: close_divergence(tx, tenant))
        for rule in plan.rules:
            try:
                with driver.session() as session:
                    session.execute_write(lambda tx, rule=rule: split_rule(tx, rule, now=now))
            except _REFUSALS as exc:
                reason = str(exc)
                refused.append(reason if rule.rule_id in reason else f"rule {rule.rule_id}: {reason}")
    if refused:
        raise SchemaCompatibilityError(
            f"the rule split cannot finish until {'this is' if len(refused) == 1 else 'these are'} repaired: "
            f"{'; '.join(refused)}. Every other shared rule is split and the graph stays at stage "
            "rule_splitting, so the next start continues the split")
    with driver.session() as session:
        left = [row["rule"] for row in session.run(_SHARED)]
    if left:
        raise SchemaCompatibilityError(f"rules are still shared after the split: {', '.join(left[:10])}; "
                                       "the next start splits them")


def _record_events(tx: ManagedTransaction) -> None:
    """One `rules_split` event per tenant the split changed (spec 8.6), written
    in the transaction that marks the graph ready, so exactly once."""
    from oms.community.repository import _TransactionDriver
    row = tx.run(f"MATCH (m:OMSMetadata {{id:'core'}}) RETURN m.{SPLIT_COUNTS} AS counts").single()
    counts = json.loads(row["counts"]) if row is not None and row["counts"] else {}
    events = Neo4jAdminEventStore(_TransactionDriver(tx))
    at = datetime.now(timezone.utc)
    for tenant, mine in sorted(counts.items()):
        events.record_admin_event(AdminEvent(
            id=f"rules-split-{tenant}", tenant_id=tenant, at=at, action=AdminAction.RULES_SPLIT,
            subject_id=tenant, subject_kind="tenant",
            after=json.dumps({"shared_rules": mine["shared_rules"], "skills_affected": len(mine["skills"]),
                              "copies": mine["copies"], "held_items": mine["held_items"],
                              "divergence_closed": mine["divergence_closed"]}, sort_keys=True)))


def migrate(manager, *, old_writers_stopped: bool, edition: str, private_version: int | None) -> dict | None:
    """Take a schema 2 graph to schema 3, resuming from any stage a stop left.

    One read on a graph already at schema 3, on an empty graph (`initialise`
    creates schema 3 there) and on a graph the source identity step has not
    finished (it comes first, and start-up refuses such a graph as before)."""
    if old_writers_stopped is not True:
        raise SchemaCompatibilityError("incompatible writers must be stopped before maintenance")
    if edition not in ("community", "enterprise"):
        raise ValueError("unknown database edition")
    if private_version is not None and (type(private_version) is not int or private_version < 1):
        raise ValueError("private schema version must be a positive integer")
    driver = manager.driver
    with driver.session() as session:
        metadata = _metadata(session)
    if metadata is None:
        return None
    version, stage = _version(metadata), metadata.get("migration_state")
    if version >= SCHEMA_VERSION and stage == "ready":
        return metadata
    if version < 2 or stage not in ("ready", *RULE_SPLIT_STAGES):
        return metadata
    manager._check(metadata, edition, SCHEMA_VERSION, private_version, maintenance=True)

    def fence(tx: ManagedTransaction) -> dict:
        _lock(tx)
        current = _metadata(tx)
        manager._check(current, edition, SCHEMA_VERSION, private_version, maintenance=True)
        if _version(current) == 2 and current.get("migration_state") == "ready":
            # Writers refuse from here on; a release older than this one also
            # refuses the newer minimum.
            tx.run("MATCH (m:OMSMetadata {id:'core'}) SET m.migration_state='rule_split_fenced', "
                   "m.minimum_core_version=$minimum", minimum=CORE_VERSION).consume()
        return _metadata(tx)

    with driver.session() as session:
        stage = session.execute_write(fence)["migration_state"]
    if stage == "rule_split_fenced":
        _advance(driver, "rule_split_fenced", "rule_splitting")
        stage = _stage(driver)
    if stage == "rule_splitting":
        split_everything(driver)
        _advance(driver, "rule_splitting", "rule_split_verified")
        stage = _stage(driver)
    if stage == "rule_split_verified":
        shared_again: list[str] = []

        def finish(tx: ManagedTransaction) -> dict:
            _lock(tx)
            current = _metadata(tx)
            if current.get("migration_state") != "rule_split_verified":
                return current
            shared_again[:] = [row["rule"] for row in tx.run(_SHARED)]
            if shared_again:
                # Shared again since the check (a hand edit while stopped):
                # back to splitting, where the next start splits it.
                tx.run("MATCH (m:OMSMetadata {id:'core'}) SET m.migration_state='rule_splitting'").consume()
                return _metadata(tx)
            _record_events(tx)
            tx.run(f"MATCH (m:OMSMetadata {{id:'core'}}) SET m.core_version=$version, m.schema_version=$version, "
                   f"m.migration_state='ready', m.last_successful_migration=$last REMOVE m.{SPLIT_COUNTS}",
                   version=SCHEMA_VERSION, last=f"core-{SCHEMA_VERSION}").consume()
            return _metadata(tx)

        with driver.session() as session:
            metadata = session.execute_write(finish)
        if shared_again:
            raise SchemaCompatibilityError(
                f"rules were shared again before the split finished: {', '.join(shared_again[:10])}; the graph "
                "is back at stage rule_splitting, and the next start splits them")
        return metadata
    with driver.session() as session:
        return _metadata(session)


def pending_split_captures(driver) -> tuple[SkillRef, ...]:
    """The skills that got a copy and still wait for their RULE_SPLIT version."""
    with driver.session() as session:
        row = session.run(f"MATCH (m:OMSMetadata {{id:'core'}}) RETURN m.{PENDING_CAPTURES} AS keys").single()
    return tuple(SkillRef.from_storage_key(key) for key in (row["keys"] or [])) if row else ()


def capture_pending_split_versions(driver, history: SkillHistory, *, tenant_id: str | None = None) -> int:
    """Record a RULE_SPLIT version for each skill that got a copy, with the
    edition's own publisher (plan D7). `SkillHistory` keeps it only when the
    snapshot changed. A skill whose capture fails stays pending for the next
    start, and the others go on; one that is done leaves the list."""
    done = 0
    for ref in pending_split_captures(driver):
        if tenant_id is not None and ref.tenant_id != tenant_id:
            continue
        try:
            history.capture_required(ref.skill_id, ref.tenant_id, cause=SkillVersionCause.RULE_SPLIT,
                                     detail="rules shared with other skills became one copy per skill")
            with driver.session() as session:
                session.run(f"MATCH (m:OMSMetadata {{id:'core'}}) SET m.{PENDING_CAPTURES} = "
                            f"[key IN coalesce(m.{PENDING_CAPTURES}, []) WHERE key <> $key]",
                            key=ref.storage_key).consume()
        except Exception:  # noqa: BLE001 - recorded later; never stops a start
            logger.exception("rule split: could not record the version of %s in %s; the next start tries again",
                             ref.skill_id, ref.tenant_id)
            continue
        done += 1
    return done
