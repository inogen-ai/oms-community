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

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from oms.adapters.neo4j.review_queue import Neo4jReviewQueue
from oms.adapters.neo4j.store import Neo4jGraphStore
from oms.community.matching import exact_text
from oms.domain.copies import RuleSlot, split_rule_id
from oms.domain.identity import SkillRef
from oms.domain.models import ReviewItem, Skill, Transaction
from oms.domain.types import SignalType

if TYPE_CHECKING:
    from oms.ports.graph_store import GraphStore
    from oms.ports.source_store import SourceRepository

logger = logging.getLogger(__name__)

RULE_SPLIT_STAGES = ("rule_split_fenced", "rule_splitting", "rule_split_verified")
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
    warnings = (*(f"{skill} tracks this rule from a source but gets a copy"
                  for skill in sorted(claimed - {keeper})),
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
