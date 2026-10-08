import math
from dataclasses import dataclass, replace
from copy import deepcopy
from oms.domain.identity import SkillRef
from oms.domain.custody import effective_example_parent
from oms.domain.relationships import EDGE_LABELS
from datetime import datetime, timezone

from oms.domain.models import (
    Rule, Skill, Transaction, Edge, Learning, Constraint, Example, Tag,
    Section, ContentBlock, Artefact, Publication, PublishBlock, QuarantinedPayload,
    RulePage, RulePlacement, UsageEvent, SkillVersion, SkillChange,
)
from oms.domain.types import (CompileStatus, ConstraintSource, ConstraintStatus, EdgeType,
                              BlockStatus, Mutability, SignalType, RuleStatus)
from oms.ports.graph_store import TRANSACTION_CONTEXT_FIELDS, RuleContext, SectionMutabilityError


@dataclass(kw_only=True)
class _ScopedEdge(Edge):
    tenant_id: str
    source_kind: str
    target_kind: str


class InMemoryGraphStore:
    _GRAPH_RECORDS = {"Skill": "skills", "Rule": "rules", "Transaction": "transactions",
                      "Section": "sections", "ContentBlock": "content_blocks", "Example": "examples",
                      "Constraint": "constraints", "Artefact": "artefacts"}
    _GRAPH_SCOPED = frozenset({"Skill", "Section", "ContentBlock", "Example"})

    def _graph_root(self, label, node_id, tenant_id):
        return (label, SkillRef(tenant_id, node_id) if label in self._GRAPH_SCOPED else node_id)

    def _graph_visible(self, root, tenant_id):
        label, key = root
        if label == "Tag":
            return key in self.tags and any(e.to_id == key and e.tenant_id == tenant_id
                                            and e.type is EdgeType.TAGGED_WITH for e in self.edges)
        value = getattr(self, self._GRAPH_RECORDS[label]).get(key)
        return value is not None and value.tenant_id == tenant_id

    def _graph_roots(self, node_id, tenant_id):
        """Every visible node an explorer key names; more than one is ambiguous."""
        from oms.adapters.graph_view import parse_graph_key
        roots = [self._graph_root(label, node_id, tenant_id) for label in self._GRAPH_RECORDS if label != "Artefact"]
        qualified = parse_graph_key(node_id)
        if qualified is not None:
            label, parts = qualified
            roots.append((label, parts[0]) if label == "Tag" else (label, (tenant_id, *parts)))
        roots = [root for root in roots if self._graph_visible(root, tenant_id)]
        if roots:
            return roots
        # Keys from before tags and file occurrences were qualified.
        legacy = [("Artefact", key) for key, value in self.artefacts.items()
                  if value.id == node_id and value.tenant_id == tenant_id] + [("Tag", node_id)]
        return [root for root in legacy if self._graph_visible(root, tenant_id)]

    def _graph_record(self, root):
        from dataclasses import asdict
        from oms.adapters.graph_view import graph_record
        label, key = root
        if label == "Tag":
            return graph_record({"id": key, "name": self.tags[key]}, label)
        properties = asdict(getattr(self, self._GRAPH_RECORDS[label])[key])
        if label == "Artefact":
            properties.update(owner_skill_id=key[1], occurrence_path=key[2])
        return graph_record(properties, label)

    def graph_node(self, node_id, tenant_id):
        roots = self._graph_roots(node_id, tenant_id)
        return self._graph_record(roots[0]) if len(roots) == 1 else None

    def _graph_edges(self, tenant_id):
        """Every relationship in the workspace, between node identities rather than ids."""
        labels = {name: label for label, name in self._GRAPH_RECORDS.items()} | {"tags": "Tag"}
        def ends(kind, node_id, owner=None):
            if labels[kind] != "Artefact":
                return [self._graph_root(labels[kind], node_id, tenant_id)]
            matches = [key for key, value in self.artefacts.items()
                       if value.id == node_id and value.tenant_id == tenant_id]
            owned = [key for key in matches if owner is not None and owner[0] == "Skill" and key[1] == owner[1].skill_id]
            return [("Artefact", key) for key in owned or matches]
        edges = {(source, target, e.type.value) for e in self.edges if e.tenant_id == tenant_id
                 for source in ends(e.source_kind, e.from_id) for target in ends(e.target_kind, e.to_id, source)}
        edges.update((self._graph_root("Skill", s.skill_id, tenant_id), ("Section", key), "HAS_SECTION")
                     for key, s in self.sections.items() if s.tenant_id == tenant_id)
        edges.update((("Section", key), ("Rule", rid), "CONTAINS_RULE") for key, ids in self.section_to_rules.items()
                     if key.tenant_id == tenant_id for rid in ids)
        edges.update((("Section", key), self._graph_root("ContentBlock", bid, tenant_id), "CONTAINS_BLOCK")
                     for key, ids in self.section_to_blocks.items() if key.tenant_id == tenant_id for bid in ids)
        edges.update((("Skill", key), ("Artefact", occurrence), "HAS_ARTEFACT")
                     for key, files in self.skill_to_artefacts.items() if key.tenant_id == tenant_id for occurrence in files)
        edges.update((("Rule", frm), ("Rule", to), kind.value) for kind, frm, to, *_ in self.cross_edges
                     if frm in self.rules and self.rules[frm].tenant_id == tenant_id)
        for key, example in self.examples.items():
            if example.tenant_id != tenant_id:
                continue
            label, parent = effective_example_parent(example)
            edges.add((self._graph_root(label, parent, tenant_id), ("Example", key), "HAS_EXAMPLE"))
        return edges

    def graph_neighbours(self, node_id, tenant_id, *, offset=0, limit=100):
        roots = self._graph_roots(node_id, tenant_id)
        if len(roots) != 1:
            return {"nodes": [], "edges": [], "total": 0, "next_offset": None}
        (anchor,) = roots
        records = {anchor: self._graph_record(anchor)}
        neighbours = {}
        for source, target, kind in self._graph_edges(tenant_id):
            if anchor not in (source, target):
                continue
            other = target if source == anchor else source
            if other not in records:
                if not self._graph_visible(other, tenant_id):
                    continue
                records[other] = self._graph_record(other)
            neighbours.setdefault(other, []).append(
                {"source": records[source]["id"], "target": records[target]["id"], "type": kind})
        chosen = sorted(neighbours, key=lambda root: (records[root]["id"], root[0]))[offset:offset + limit]
        return {"nodes": [records[root] for root in chosen],
                "edges": [edge for root in chosen for edge in sorted(
                    neighbours[root], key=lambda edge: (edge["source"], edge["target"], edge["type"]))],
                "total": len(neighbours),
                "next_offset": offset + limit if offset + limit < len(neighbours) else None}

    def __init__(self) -> None:
        import threading
        self._compile_mutex = threading.Lock()
        self._workflow_mutex = threading.RLock()
        self._compile_claims: dict[str, str] = {}
        self.rules: dict[str, Rule] = {}
        self.transactions: dict[str, Transaction] = {}
        self.skills: dict[SkillRef, Skill] = {}
        self.tags: dict[str, str] = {}
        self.edges: list[_ScopedEdge] = []
        self.learnings: dict[str, Learning] = {}
        self.constraints: dict[str, Constraint] = {}
        self.examples: dict[SkillRef, Example] = {}
        # Skill-package custody storage.
        self.sections: dict[SkillRef, Section] = {}
        self.content_blocks: dict[SkillRef, ContentBlock] = {}
        self.artefacts: dict[tuple[str, str, str], Artefact] = {}
        self.publications: dict[tuple[str, str], Publication] = {}
        self.publish_blocks: dict[str, PublishBlock] = {}          # tenant_id -> block flag
        self.observations: dict[str, set[tuple[str, str]]] = {}   # rule_id -> {(txn_id, source_ref)}
        self.compile_failures: dict[str, str] = {}                 # txn_id -> reason
        self.non_fault_failures: dict[str, bool] = {}              # txn_id -> settled, not a defect
        self.dismissed_failures: dict[str, str | None] = {}        # txn_id -> who acknowledged it
        self.compile_status: dict[str, CompileStatus] = {}         # txn_id -> out of pending, why
        self.cross_edges: list[tuple[EdgeType, str, str, float, str | None]] = []
        self.section_to_rules: dict[SkillRef, list[str]] = {}
        self.section_rule_placements: dict[SkillRef, dict[str, RulePlacement]] = {}   # section_id -> rule_id -> placement
        self.section_to_blocks: dict[SkillRef, list[str]] = {}
        self.skill_to_artefacts: dict[SkillRef, list[tuple[str, str, str]]] = {}    # skill -> occurrence keys
        self.usage: dict[str, UsageEvent] = {}                     # skill-fetch stream (spec §7.5.1)
        # Contributions the sanitiser refused, keyed by transaction id: the
        # third way work leaves the pipeline, beside failures and holds.
        self.quarantined: dict[str, QuarantinedPayload] = {}
        self._skill_versions: list[SkillVersion] = []

    def upsert_rule(self, rule: Rule) -> None:
        existing = self.rules.get(rule.id)
        if existing is not None and existing.tenant_id != rule.tenant_id:
            raise ValueError("Rule ID belongs to another tenant")
        self.rules[rule.id] = deepcopy(rule)

    def create_rule_if_absent(self, rule: Rule) -> None:
        with self._workflow_mutex:
            existing = self.rules.get(rule.id)
            if existing is not None and existing.tenant_id != rule.tenant_id:
                raise ValueError("Rule ID belongs to another tenant")
            self.rules.setdefault(rule.id, deepcopy(rule))

    def get_rule(self, rule_id: str) -> Rule | None:
        return deepcopy(self.rules.get(rule_id))

    def upsert_transaction(self, transaction: Transaction) -> None:
        existing = self.transactions.get(transaction.id)
        if existing is not None and existing.tenant_id != transaction.tenant_id:
            raise ValueError("Transaction ID belongs to another tenant")
        self.transactions[transaction.id] = deepcopy(transaction)

    def workflow_state_for(self, transaction_id: str) -> str:
        txn = self.transactions.get(transaction_id)
        if txn and txn.workflow_state:
            return txn.workflow_state
        if self.compile_status.get(transaction_id) is CompileStatus.FAILED:
            return "failed"
        return "received"

    def get_transaction(self, transaction_id: str) -> Transaction | None:
        return deepcopy(self.transactions.get(transaction_id))

    def upsert_skill(self, skill: Skill) -> None:
        self.skills[SkillRef(skill.tenant_id, skill.id)] = deepcopy(skill)

    def get_skill(self, skill_id: str, *, tenant_id: str) -> Skill | None:
        return deepcopy(self.skills.get(SkillRef(tenant_id, skill_id)))

    def delete_skill(self, skill_id: str, *, tenant_id: str) -> None:
        key = SkillRef(tenant_id, skill_id)
        self.skills.pop(key, None)
        sections = {k for k, sec in self.sections.items()
                    if sec.tenant_id == tenant_id and sec.skill_id == skill_id}
        blocks = set()
        for section in sections:
            blocks.update(self.section_to_blocks.pop(section, []))
            self.section_to_rules.pop(section, None)
            self.section_rule_placements.pop(section, None)
            self.sections.pop(section, None)
        # A block still attached to another owner survives the deletion.
        retained = {bid for sec, ids in self.section_to_blocks.items()
                    if sec.tenant_id == tenant_id for bid in ids}
        for bid in blocks - retained:
            self.content_blocks.pop(SkillRef(tenant_id, bid), None)
        section_ids = {k.skill_id for k in sections}
        owned_parents = {("Skill", skill_id), *(("Section", sid) for sid in section_ids)}
        example_ids = {ex.id for ex in self.examples.values() if ex.tenant_id == tenant_id
                       and effective_example_parent(ex) in owned_parents}
        self.examples = {key: ex for key, ex in self.examples.items()
                         if not (ex.tenant_id == tenant_id and ex.id in example_ids)}
        removed = {("skills", skill_id), *(("sections", sid) for sid in section_ids),
                   *(("content_blocks", bid) for bid in blocks - retained),
                   *(("examples", eid) for eid in example_ids)}
        self.edges = [e for e in self.edges if not (e.tenant_id == tenant_id and
                      ((e.source_kind, e.from_id) in removed or (e.target_kind, e.to_id) in removed))]
        self.skill_to_artefacts.pop(key, None)
        self.artefacts = {k: a for k, a in self.artefacts.items() if k[:2] != (tenant_id, skill_id)}
        self._skill_versions = [v for v in self._skill_versions
                                if (v.tenant_id, v.skill_id) != (tenant_id, skill_id)]

    def upsert_tag(self, tag_id: str, name: str) -> None:
        self.tags[tag_id] = name

    def tag_name(self, tag_id: str) -> str | None:
        return self.tags.get(tag_id)

    def attach_edge(self, edge: Edge, *, tenant_id: str) -> None:
        pair = self._edge_pair(edge, tenant_id)
        if pair is None:
            raise KeyError("Relationship endpoints are missing or outside the tenant")
        source, target = pair
        self._attach_typed_edge(edge, tenant_id, source[0], target[0])

    def _attach_typed_edge(self, edge: Edge, tenant_id: str, source_kind: str, target_kind: str) -> None:
        scoped = _ScopedEdge(type=edge.type, from_id=edge.from_id, to_id=edge.to_id,
                             properties=deepcopy(edge.properties), tenant_id=tenant_id,
                             source_kind=source_kind, target_kind=target_kind)
        if not any(e.type is edge.type and e.from_id == edge.from_id and e.to_id == edge.to_id
                   and e.tenant_id == tenant_id and e.source_kind == source_kind
                   and e.target_kind == target_kind for e in self.edges):
            self.edges.append(scoped)

    def detach_edge(self, edge: Edge, *, tenant_id: str) -> None:
        pair = self._edge_pair(edge, tenant_id)
        if pair is None:
            return
        source, target = pair
        self.edges = [e for e in self.edges if not (e.type is edge.type and
                      e.from_id == edge.from_id and e.to_id == edge.to_id and e.tenant_id == tenant_id
                      and e.source_kind == source[0] and e.target_kind == target[0])]

    def _edge_pair(self, edge: Edge, tenant_id: str):
        names = {"Skill": "skills", "Rule": "rules", "Tag": "tags", "Transaction": "transactions",
                 "Section": "sections", "ContentBlock": "content_blocks", "Example": "examples",
                 "Artefact": "artefacts", "Constraint": "constraints"}
        pairs = [(source, target) for left, right in EDGE_LABELS[edge.type]
                 for source in self._endpoint_matches(edge.from_id, (names[left],), tenant_id)
                 for target in self._endpoint_matches(edge.to_id, (names[right],), tenant_id)]
        if len(pairs) > 1:
            raise ValueError("Relationship endpoints are ambiguous")
        return pairs[0] if pairs else None

    def superseders_of(self, rule_id: str) -> list[str]:
        seen: list[str] = []
        for e in self.edges:
            if e.type is EdgeType.SUPERSEDES and e.target_kind == "rules" and e.to_id == rule_id and e.from_id not in seen:
                seen.append(e.from_id)
        return seen

    def skills_for_rule(self, rule_id: str, *, tenant_id: str) -> list[Skill]:
        rule = self.rules.get(rule_id)
        if rule is None or rule.tenant_id != tenant_id:
            return []
        ids = {e.to_id for e in self.edges if e.type is EdgeType.BELONGS_TO
               and e.from_id == rule_id and e.tenant_id == tenant_id}
        return [deepcopy(self.skills[SkillRef(tenant_id, sid)]) for sid in sorted(ids)
                if SkillRef(tenant_id, sid) in self.skills]

    def skills_by_rule(self, rule_ids: list[str], *, tenant_id: str) -> dict[str, list[Skill]]:
        return {rid: skills for rid in sorted(set(rule_ids))
                if (skills := self.skills_for_rule(rid, tenant_id=tenant_id))}

    def rules_by_tags(self, tags: set[str], tenant_id: str, limit: int = 50) -> list[Rule]:
        out: list[Rule] = []
        for e in self.edges:
            if e.type is EdgeType.TAGGED_WITH and e.source_kind == "rules" and self.tags.get(e.to_id) in tags:
                rule = self.rules.get(e.from_id)
                if rule and rule.tenant_id == tenant_id and rule not in out:
                    out.append(deepcopy(rule))
        return out[:limit]

    def skills_by_tags(self, tags: set[str], tenant_id: str) -> list[Skill]:
        ids = {e.from_id for e in self.edges if e.tenant_id == tenant_id
               and e.type is EdgeType.TAGGED_WITH and e.source_kind == "skills" and self.tags.get(e.to_id) in tags}
        return [deepcopy(self.skills[SkillRef(tenant_id, sid)]) for sid in sorted(ids)
                if SkillRef(tenant_id, sid) in self.skills]

    def tag_records_for_skill(self, skill_id: str, *, tenant_id: str) -> list[Tag]:
        identifiers = {edge.to_id for edge in self.edges if edge.tenant_id == tenant_id
                       and edge.type is EdgeType.TAGGED_WITH and edge.source_kind == "skills"
                       and edge.from_id == skill_id and edge.to_id in self.tags}
        return [Tag(id=identifier, name=self.tags[identifier]) for identifier in sorted(identifiers)]

    def tags_for_skill(self, skill_id: str, *, tenant_id: str) -> list[str]:
        return sorted({self.tags[e.to_id] for e in self.edges if e.tenant_id == tenant_id
                       and e.type is EdgeType.TAGGED_WITH and e.source_kind == "skills" and e.from_id == skill_id
                       and e.to_id in self.tags})

    def rules_for_skill(self, skill_id: str, *, tenant_id: str) -> list[Rule]:
        ids = {e.from_id for e in self.edges if e.tenant_id == tenant_id
               and e.type is EdgeType.BELONGS_TO and e.to_id == skill_id}
        return [deepcopy(self.rules[rid]) for rid in sorted(ids)
                if rid in self.rules and self.rules[rid].tenant_id == tenant_id]

    def lineage(self, rule_id: str) -> list[Transaction]:
        rows = [
            self.transactions[e.to_id]
            for e in self.edges
            if e.type is EdgeType.DERIVED_FROM and e.source_kind == "rules" and e.from_id == rule_id and e.to_id in self.transactions
        ]
        # Oldest first, promised by the port: the rule story reads this as a
        # timeline and edge-insertion order is not chronological order.
        return deepcopy(sorted(rows, key=lambda t: t.timestamp))

    def rule_context(self, rule_ids: list[str], *, tenant_id: str) -> dict[str, RuleContext]:
        out: dict[str, RuleContext] = {}
        for rule_id in rule_ids:
            if rule_id not in self.rules or self.rules[rule_id].tenant_id != tenant_id:
                continue   # absent, not empty: the caller can tell them apart
            skills = self.skills_for_rule(rule_id, tenant_id=tenant_id)
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
            if edge.source_kind == "rules" and edge.from_id in self.rules:
                out.setdefault(edge.to_id, edge.from_id)
        return out

    def transactions_for_tenant(self, tenant_id: str,
                                since: datetime | None = None,
                                limit: int = 200) -> list[Transaction]:
        rows = [t for t in self.transactions.values()
                if t.tenant_id == tenant_id
                and (since is None or t.timestamp >= since)]
        return deepcopy(sorted(rows, key=lambda t: t.timestamp, reverse=True)[:limit])

    def clear_transaction_context(self, transaction_ids: list[str], tenant_id: str) -> int:
        cleared = 0
        for transaction_id in set(transaction_ids):
            txn = self.transactions.get(transaction_id)
            if txn is None or txn.tenant_id != tenant_id:
                continue
            if any(getattr(txn, name) is not None for name in TRANSACTION_CONTEXT_FIELDS):
                cleared += 1
            self.transactions[transaction_id] = replace(
                txn, **{name: None for name in TRANSACTION_CONTEXT_FIELDS})
        return cleared

    def rules_for_tenant(self, tenant_id: str, limit: int = 500) -> list[Rule]:
        rows = [deepcopy(r) for r in self.rules.values() if r.tenant_id == tenant_id]
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
        return RulePage(items=deepcopy(matched[offset:offset + limit]), total=len(matched),
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
        return deepcopy(rows[:limit])

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
        self.constraints[constraint.id] = deepcopy(constraint)

    def active_constraints(self, tenant_id: str) -> list[Constraint]:
        return [deepcopy(c) for c in self.constraints.values()
                if c.tenant_id == tenant_id
                and c.status is ConstraintStatus.ACTIVE]

    def all_constraints(self, tenant_id: str) -> list[Constraint]:
        return [deepcopy(c) for c in self.constraints.values() if c.tenant_id == tenant_id]

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
        return [deepcopy(s) for _, s in sorted(self.skills.items()) if s.tenant_id == tenant_id]

    def transactions_for_person(self, person_id: str, tenant_id: str) -> list[Transaction]:
        # Attribution predicate in `_attributed_to`, shared with the count.
        rows = [t for t in self.transactions.values()
                if self._attributed_to(t, person_id, tenant_id)]
        rows.sort(key=lambda t: t.timestamp)
        return deepcopy(rows)

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
        return deepcopy(pending[:limit])

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
            if (e.type is EdgeType.CONFLICTS_WITH and e.target_kind == "constraints"
                    and e.tenant_id == tenant_id and e.to_id in self.constraints):
                rule = self.rules.get(e.from_id)
                if rule and rule.tenant_id == tenant_id and rule not in out:
                    out.append(deepcopy(rule))
        return out

    def upsert_example(self, example: Example, *, tenant_id: str) -> None:
        if example.tenant_id != tenant_id:
            raise ValueError("Example does not belong to this tenant")
        label, parent = effective_example_parent(example)
        records = {"Skill": "skills", "Section": "sections", "Rule": "rules"}
        self._endpoint(parent, (records[label],), tenant_id)
        self.edges = [edge for edge in self.edges if not (edge.tenant_id == tenant_id
            and edge.type in (EdgeType.HAS_EXAMPLE, EdgeType.ILLUSTRATES)
            and ((edge.source_kind == "examples" and edge.from_id == example.id)
                 or (edge.target_kind == "examples" and edge.to_id == example.id)))]
        self.examples[SkillRef(tenant_id, example.id)] = deepcopy(example)

    def get_example(self, example_id: str, *, tenant_id: str) -> Example | None:
        return deepcopy(self.examples.get(SkillRef(tenant_id, example_id)))

    def examples_for_rule(self, rule_id: str, *, tenant_id: str) -> list[Example]:
        return sorted((deepcopy(e) for e in self.examples.values() if e.tenant_id == tenant_id
                       and effective_example_parent(e) == ("Rule", rule_id)), key=lambda e: (e.order is None, e.order or 0, e.id))

    def examples_for_skill(self, skill_id: str, *, tenant_id: str) -> list[Example]:
        sections = {s.id for s in self.sections_for_skill(skill_id, tenant_id=tenant_id)}
        parents = {("Skill", skill_id), *(("Section", sid) for sid in sections)}
        return sorted((deepcopy(e) for e in self.examples.values() if e.tenant_id == tenant_id
                       and effective_example_parent(e) in parents),
                      key=lambda e: (e.order is None, e.order or 0, e.id))

    # -- Skill-package custody operations ----------------------------------

    def upsert_section(self, section: Section) -> None:
        if SkillRef(section.tenant_id, section.skill_id) not in self.skills:
            raise KeyError(section.skill_id)
        key = SkillRef(section.tenant_id, section.id)
        existing = self.sections.get(key)
        if existing is not None and existing.skill_id != section.skill_id:
            raise ValueError("Section identity belongs to another skill")
        self.sections[key] = deepcopy(section)

    def get_section(self, section_id: str, *, tenant_id: str) -> Section | None:
        return deepcopy(self.sections.get(SkillRef(tenant_id, section_id)))

    def sections_for_skill(self, skill_id: str, *, tenant_id: str) -> list[Section]:
        return sorted((deepcopy(s) for s in self.sections.values()
                       if s.tenant_id == tenant_id and s.skill_id == skill_id), key=lambda s: (s.order, s.id))

    def attach_rule(self, rule: Rule, section_id: str,
                    order: int | None = None, group: str | None = None, *, tenant_id: str) -> None:
        key = SkillRef(tenant_id, section_id)
        sec = self.sections.get(key)
        if sec is None:
            raise KeyError(section_id)
        if rule.tenant_id != tenant_id or rule.id not in self.rules or self.rules[rule.id].tenant_id != tenant_id:
            raise ValueError("Rule does not belong to this tenant")
        if sec.mutability is not Mutability.SYSTEM_AGGREGATED:
            raise SectionMutabilityError(f"section {section_id} is {sec.mutability.value}; cannot attach rule")
        bucket = self.section_to_rules.setdefault(key, [])
        if rule.id not in bucket:
            bucket.append(rule.id)
        self.section_rule_placements.setdefault(key, {})[rule.id] = RulePlacement(rule_id=rule.id, order=order, group=group)

    def detach_rule(self, rule_id: str, section_id: str, *, tenant_id: str) -> None:
        key = SkillRef(tenant_id, section_id)
        self.section_to_rules[key] = [identifier for identifier in self.section_to_rules.get(key, ()) if identifier != rule_id]
        self.section_rule_placements.get(key, {}).pop(rule_id, None)
        self.edges = [edge for edge in self.edges if not (edge.tenant_id == tenant_id and edge.type is EdgeType.CONTAINS_RULE and edge.from_id == section_id and edge.to_id == rule_id)]

    def detach_block(self, block_id: str, section_id: str, *, tenant_id: str) -> None:
        key = SkillRef(tenant_id, section_id)
        self.section_to_blocks[key] = [identifier for identifier in self.section_to_blocks.get(key, ()) if identifier != block_id]
        self.edges = [edge for edge in self.edges if not (edge.tenant_id == tenant_id and edge.type is EdgeType.CONTAINS_BLOCK and edge.from_id == section_id and edge.to_id == block_id)]

    def remove_example(self, example_id: str, *, tenant_id: str) -> None:
        example = self.examples.get(SkillRef(tenant_id, example_id))
        parents = {effective_example_parent(example)} if example else set()
        parents.update(({"skills": "Skill", "rules": "Rule", "sections": "Section"}[edge.source_kind], edge.from_id) for edge in self.edges
                       if edge.tenant_id == tenant_id and edge.type is EdgeType.HAS_EXAMPLE
                       and edge.target_kind == "examples" and edge.to_id == example_id)
        if len(parents) > 1:
            raise ValueError("Example has ambiguous owners")
        self.examples.pop(SkillRef(tenant_id, example_id), None)
        self.edges = [edge for edge in self.edges if not (edge.tenant_id == tenant_id and ((edge.source_kind == "examples" and edge.from_id == example_id) or (edge.target_kind == "examples" and edge.to_id == example_id)))]

    def remove_section(self, section_id: str, *, tenant_id: str) -> None:
        key = SkillRef(tenant_id, section_id)
        if (self.section_to_rules.get(key) or self.section_to_blocks.get(key)
                or self.examples_for_section(section_id, tenant_id=tenant_id)
                or any(edge.tenant_id == tenant_id and edge.source_kind == "sections" and edge.from_id == section_id for edge in self.edges)):
            raise ValueError("Section still owns content")
        self.sections.pop(key, None)
        self.section_to_rules.pop(key, None)
        self.section_to_blocks.pop(key, None)
        self.section_rule_placements.pop(key, None)
        self.edges = [edge for edge in self.edges if not (edge.tenant_id == tenant_id and edge.target_kind == "sections" and edge.to_id == section_id)]

    def inherit_placements(self, retired_rule_id: str, successor: Rule, *, tenant_id: str) -> None:
        old = self.rules.get(retired_rule_id)
        if old is None or old.tenant_id != tenant_id or successor.tenant_id != tenant_id:
            raise ValueError("Placement inheritance crosses tenant ownership")
        for section, placements in list(self.section_rule_placements.items()):
            placement = placements.get(retired_rule_id)
            if section.tenant_id == tenant_id and placement is not None:
                self.attach_rule(successor, section.skill_id, order=placement.order, group=placement.group, tenant_id=tenant_id)

    def attach_block(self, block: ContentBlock, section_id: str, *, tenant_id: str) -> None:
        key = SkillRef(tenant_id, section_id)
        sec = self.sections.get(key)
        if sec is None:
            raise KeyError(section_id)
        if block.tenant_id != tenant_id or SkillRef(tenant_id, block.id) not in self.content_blocks:
            raise ValueError("Block does not belong to this tenant")
        if sec.mutability is not Mutability.AUTHORIAL_PASSTHROUGH:
            raise SectionMutabilityError(f"section {section_id} is {sec.mutability.value}; cannot attach block")
        bucket = self.section_to_blocks.setdefault(key, [])
        if block.id not in bucket:
            bucket.append(block.id)

    def attach_example(self, example: Example, *, tenant_id: str) -> None:
        self.upsert_example(example, tenant_id=tenant_id)

    def rules_for_section(self, section_id: str, *, tenant_id: str) -> list[Rule]:
        key = SkillRef(tenant_id, section_id)
        rules = [deepcopy(self.rules[rid]) for rid in self.section_to_rules.get(key, [])
                 if rid in self.rules and self.rules[rid].tenant_id == tenant_id]
        placements = self.section_rule_placements.get(key, {})
        def order(rule):
            p = placements.get(rule.id)
            return (0, p.order, rule.id) if p and p.order is not None else (1, -rule.corroboration_count, rule.id)
        return sorted(rules, key=order)

    def rule_placements_for_section(self, section_id: str, *, tenant_id: str) -> list[RulePlacement]:
        return sorted((deepcopy(p) for p in self.section_rule_placements.get(SkillRef(tenant_id, section_id), {}).values()),
                      key=lambda p: (p.order is None, p.order or 0, p.rule_id))

    def blocks_for_section(self, section_id: str, *, tenant_id: str, include_inactive: bool = False) -> list[ContentBlock]:
        blocks = [self.content_blocks[SkillRef(tenant_id, bid)]
                  for bid in self.section_to_blocks.get(SkillRef(tenant_id, section_id), [])
                  if SkillRef(tenant_id, bid) in self.content_blocks]
        return sorted((replace(deepcopy(b), status=b.status or BlockStatus.ACTIVE) for b in blocks
                       if include_inactive or (b.status or BlockStatus.ACTIVE) is BlockStatus.ACTIVE), key=lambda b: b.id)

    def get_content_block(self, block_id: str, *, tenant_id: str) -> ContentBlock | None:
        return deepcopy(self.content_blocks.get(SkillRef(tenant_id, block_id)))

    def content_owners(self, rule_ids: list[str], block_ids: list[str], *,
                       tenant_id: str) -> dict[tuple[str, str], frozenset[str]]:
        rules = {rid for rid in rule_ids if rid in self.rules and self.rules[rid].tenant_id == tenant_id}
        blocks = {bid for bid in block_ids if SkillRef(tenant_id, bid) in self.content_blocks}
        owners = {("rule", rid): set() for rid in rule_ids} | {("block", bid): set() for bid in block_ids}
        for edge in self.edges:
            if (edge.type is EdgeType.BELONGS_TO and edge.tenant_id == tenant_id and edge.from_id in rules
                    and SkillRef(tenant_id, edge.to_id) in self.skills):
                owners["rule", edge.from_id].add(edge.to_id)
        for key, section in self.sections.items():
            if key.tenant_id == tenant_id:
                placed = (("rule", rid) for rid in self.section_to_rules.get(key, ()) if rid in rules)
                held = (("block", bid) for bid in self.section_to_blocks.get(key, ()) if bid in blocks)
                for entity in (*placed, *held):
                    owners[entity].add(section.skill_id)
        return {entity: frozenset(skills) for entity, skills in owners.items()}

    def section_for_block(self, block_id: str, *, tenant_id: str) -> Section | None:
        for key, ids in sorted(self.section_to_blocks.items()):
            if key.tenant_id == tenant_id and block_id in ids:
                return deepcopy(self.sections.get(key))
        return None

    def sections_with_multiple_active_blocks(self, tenant_id: str) -> list[str]:
        # Custody invariant (Section 7.3): an authorial section holds exactly one
        # active block. Return the ids of this tenant's sections that hold more.
        violating: list[str] = []
        for section in self.sections.values():
            if section.tenant_id != tenant_id:
                continue
            if len(self.blocks_for_section(section.id, tenant_id=tenant_id)) > 1:
                violating.append(section.id)
        return violating

    def active_blocks_without_body(self, tenant_id: str) -> list[str]:
        # Empty text on a block that publishes; see the port for why these exist.
        return [b.id for b in self.content_blocks.values()
                if b.tenant_id == tenant_id
                and b.status is BlockStatus.ACTIVE
                and not b.body]

    def blocks_revised_for_rule(self, rule_id: str, *, tenant_id: str) -> list[ContentBlock]:
        rule = self.rules.get(rule_id)
        if rule is None or rule.tenant_id != tenant_id:
            return []
        transactions = {e.to_id for e in self.edges if e.tenant_id == tenant_id
                        and e.type is EdgeType.DERIVED_FROM and e.source_kind == "rules" and e.from_id == rule_id}
        ids = {e.from_id for e in self.edges if e.tenant_id == tenant_id
               and e.type is EdgeType.DERIVED_FROM and e.source_kind == "content_blocks" and e.to_id in transactions}
        blocks = [self.content_blocks[SkillRef(tenant_id, bid)] for bid in sorted(ids)
                  if SkillRef(tenant_id, bid) in self.content_blocks]
        return [deepcopy(b) for b in blocks if (b.status or BlockStatus.ACTIVE) is BlockStatus.ACTIVE]

    def supersede_block(self, old_block_id: str, new_block: ContentBlock, section_id: str,
                        transaction_id: str | None = None, *, tenant_id: str) -> None:
        key = SkillRef(tenant_id, section_id)
        sec = self.sections.get(key)
        old = self.content_blocks.get(SkillRef(tenant_id, old_block_id))
        if sec is None or old is None or old_block_id not in self.section_to_blocks.get(key, []):
            raise KeyError(old_block_id)
        if new_block.tenant_id != tenant_id:
            raise ValueError("Block does not belong to this tenant")
        if sec.mutability is not Mutability.AUTHORIAL_PASSTHROUGH:
            raise SectionMutabilityError("Cannot supersede a block in an aggregated section")
        if transaction_id is not None:
            self._endpoint(transaction_id, ("transactions",), tenant_id)
        self.upsert_content_block(replace(new_block, status=BlockStatus.ACTIVE))
        self.attach_block(new_block, section_id, tenant_id=tenant_id)
        old.status = BlockStatus.SUPERSEDED
        self._attach_typed_edge(Edge(type=EdgeType.SUPERSEDES, from_id=new_block.id, to_id=old_block_id),
                                tenant_id, "content_blocks", "content_blocks")
        if transaction_id is not None:
            self._attach_typed_edge(Edge(type=EdgeType.DERIVED_FROM, from_id=new_block.id, to_id=transaction_id),
                                    tenant_id, "content_blocks", "transactions")

    def upsert_content_block(self, block: ContentBlock) -> None:
        self.content_blocks[SkillRef(block.tenant_id, block.id)] = deepcopy(block)

    def upsert_artefact(self, artefact: Artefact, skill_id: str, path: str, *, tenant_id: str) -> None:
        if artefact.tenant_id != tenant_id:
            raise ValueError("Artefact does not belong to this tenant")
        skill = SkillRef(tenant_id, skill_id)
        if skill not in self.skills:
            raise KeyError(skill_id)
        occurrence = (tenant_id, skill_id, path)
        self.artefacts[occurrence] = deepcopy(artefact)
        bucket = self.skill_to_artefacts.setdefault(skill, [])
        if occurrence not in bucket:
            bucket.append(occurrence)

    def artefacts_for_skill(self, skill_id: str, *, tenant_id: str) -> list[tuple[Artefact, str]]:
        return [(deepcopy(self.artefacts[key]), key[2]) for key in sorted(
            self.skill_to_artefacts.get(SkillRef(tenant_id, skill_id), []))]

    def remove_artefact(self, skill_id: str, path: str, *, tenant_id: str) -> None:
        occurrence = (tenant_id, skill_id, path)
        previous = self.artefacts.get(occurrence)
        skill = SkillRef(tenant_id, skill_id)
        self.skill_to_artefacts[skill] = [key for key in self.skill_to_artefacts.get(skill, ()) if key != occurrence]
        self.artefacts.pop(occurrence, None)
        if previous is not None and not any(key[:2] == (tenant_id, skill_id) and value.id == previous.id
                                            for key, value in self.artefacts.items()):
            self.edges = [edge for edge in self.edges if not (edge.tenant_id == tenant_id
                and edge.source_kind == "skills" and edge.from_id == skill_id
                and edge.target_kind == "artefacts" and edge.to_id == previous.id)]

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
        rows.sort(key=lambda v: (v.at, v.id), reverse=True)
        return rows

    def skill_versions(self, tenant_id: str, skill_id: str, limit: int = 50,
                       before: datetime | None = None, *, before_id: str | None = None) -> list[SkillVersion]:
        rows = self._versions_of(tenant_id, skill_id)
        if before is not None:
            rows = [v for v in rows if v.at < before or (v.at == before and before_id is not None and v.id < before_id)]
        return rows[:limit]

    def get_skill_version(self, version_id: str, *, tenant_id: str) -> SkillVersion | None:
        return deepcopy(next((v for v in self._skill_versions if v.id == version_id and v.tenant_id == tenant_id), None))

    def recent_skill_changes(self, tenant_id: str, limit: int = 8) -> list[SkillChange]:
        rows = [v for v in self._skill_versions if v.tenant_id == tenant_id
                and SkillRef(tenant_id, v.skill_id) in self.skills]
        rows.sort(key=lambda v: (v.at, v.id), reverse=True)
        return [SkillChange(id=v.id, skill_id=v.skill_id, skill_name=self.skills[SkillRef(tenant_id, v.skill_id)].name,
                            at=v.at, cause=v.cause, revision=v.revision,
                            actor_person_id=v.actor_person_id, detail=v.detail) for v in rows[:max(0, limit)]]

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
                                if not (v.tenant_id == tenant_id and v.skill_id == skill_id and v.id in doomed)]
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
            rule = self.rules.get(rule_id)
            transaction = self.transactions.get(transaction_id)
            if rule is None or transaction is None:
                raise KeyError(rule_id if rule is None else transaction_id)
            if rule.tenant_id != transaction.tenant_id:
                raise ValueError("Rule observation crosses tenant ownership")
            bucket = self.observations.setdefault(rule_id, set())
            new_source = source_ref not in {sr for (_t, sr) in bucket}
            bucket.add((transaction_id, source_ref))
            rule = self.rules.get(rule_id)
            if rule is not None and new_source:
                rule.corroboration_count += 1

    def examples_for_section(self, section_id: str, *, tenant_id: str) -> list[Example]:
        return sorted((deepcopy(e) for e in self.examples.values() if e.tenant_id == tenant_id
                       and effective_example_parent(e) == ("Section", section_id)), key=lambda e: (e.order is None, e.order or 0, e.id))

    def upsert_cross_skill_edge(
        self, edge_type: EdgeType, from_rule_id: str, to_rule_id: str,
        confidence: float, kind: str | None = None,
    ) -> None:
        source = self.rules.get(from_rule_id)
        target = self.rules.get(to_rule_id)
        if source is None or target is None:
            raise KeyError(from_rule_id if source is None else to_rule_id)
        if source.tenant_id != target.tenant_id:
            raise ValueError("Rule relationship crosses tenant ownership")
        for i, (et, frm, to, _conf, _k) in enumerate(self.cross_edges):
            if et is edge_type and frm == from_rule_id and to == to_rule_id:
                self.cross_edges[i] = (edge_type, from_rule_id, to_rule_id, confidence, kind)
                return
        self.cross_edges.append((edge_type, from_rule_id, to_rule_id, confidence, kind))

    def cross_skill_neighbours(self, rule_id: str) -> list[tuple[EdgeType, str, float]]:
        return [(et, to, conf) for (et, frm, to, conf, _k) in self.cross_edges if frm == rule_id]

    def _endpoint_matches(self, node_id: str, labels: tuple[str, ...], tenant_id: str):
        matches = []
        for label in labels:
            records = getattr(self, label)
            if label == "tags":
                if node_id in records:
                    matches.append((label, node_id))
            elif label == "artefacts":
                matches.extend((label, key) for key, value in records.items()
                               if value.id == node_id and value.tenant_id == tenant_id)
            else:
                key = SkillRef(tenant_id, node_id) if label in {
                    "skills", "sections", "content_blocks", "examples"} else node_id
                value = records.get(key)
                if value is not None and value.tenant_id == tenant_id:
                    matches.append((label, key))
        return matches

    def _endpoint(self, node_id: str, labels: tuple[str, ...], tenant_id: str):
        matches = self._endpoint_matches(node_id, labels, tenant_id)
        if not matches:
            if any(value.id == node_id and value.tenant_id != tenant_id
                   for label in labels if label != "tags" for value in getattr(self, label).values()):
                raise ValueError("Relationship endpoint belongs to another tenant")
            raise KeyError(node_id)
        if len(matches) != 1:
            raise ValueError("Ambiguous relationship endpoint")
        return matches[0]
