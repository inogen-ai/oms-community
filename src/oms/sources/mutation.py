"""Content generations and conditional writes inside the workflow transaction.

A tracked write fingerprints a skill immediately before its first write that
reaches it: the skill itself or, for a rule, block or example, every skill whose
content includes it. Only those skills are fingerprinted again, locked and
advanced, so an unrelated skill keeps its generation and its guards, and the
cost of a write follows what it touches rather than the size of the tenant.
"""
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass, replace
from enum import Enum
from functools import wraps
from hashlib import sha256
import json
from typing import TypeVar

from oms.domain.custody import effective_example_parent
from oms.domain.identity import SkillRef
from oms.domain.relationships import EDGE_LABELS
from oms.ports.mutation import MutationContext, MutationContextFactory, MutationRequest
from oms.sources.errors import SourceConflict, SourceForbidden
from oms.sources.models import Generations

T = TypeVar("T")


def _content(value):
    # Creation timestamps and embeddings are left out of the digest: neither
    # says anything about what the skill tells an agent, and both can differ
    # between two reads of the same content, which would make an unchanged
    # skill look changed and refuse every guarded write against it.
    if is_dataclass(value):
        return _content(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _content(item) for key, item in sorted(value.items())
                if key not in {"created_at", "embedding"}}
    if isinstance(value, (set, frozenset)):
        return sorted(_content(item) for item in value)
    if isinstance(value, (tuple, list)):
        return [_content(item) for item in value]
    return value


def _skill_content(graph, ref: SkillRef):
    """Everything an installed skill tells an agent, read in a storage-independent order.

    The record is what a content generation protects: rules wherever they are placed,
    sections with their placements, blocks and examples, provenance, tags and files.
    Each list is sorted by identifier so the digest does not depend on the order an
    adapter happens to return rows in.
    """
    skill = graph.get_skill(ref.skill_id, tenant_id=ref.tenant_id)
    if skill is None:
        return None
    tenant = ref.tenant_id
    rules = sorted(graph.rules_for_skill(ref.skill_id, tenant_id=tenant), key=lambda row: row.id)
    by_rule = {rule.id: rule for rule in rules}
    sections = []
    for section in sorted(graph.sections_for_skill(ref.skill_id, tenant_id=tenant), key=lambda row: row.id):
        section_rules = sorted(graph.rules_for_section(section.id, tenant_id=tenant), key=lambda row: row.id)
        by_rule.update((rule.id, rule) for rule in section_rules)
        sections.append({"section": section,
                         "rule_content": section_rules,
                         "rules": sorted(graph.rule_placements_for_section(section.id, tenant_id=tenant),
                                         key=lambda row: row.rule_id),
                         "blocks": sorted(graph.blocks_for_section(section.id, tenant_id=tenant),
                                          key=lambda row: row.id),
                         "examples": sorted(graph.examples_for_section(section.id, tenant_id=tenant),
                                            key=lambda row: row.id)})
    rules = [by_rule[key] for key in sorted(by_rule)]
    record = {"skill": skill, "rules": rules, "sections": sections,
              "provenance": {rule.id: sorted((transaction.id, transaction.tenant_id,
                    transaction.source_ref or "") for transaction in graph.lineage(rule.id)) for rule in rules},
              "rule_examples": {rule.id: sorted(graph.examples_for_rule(rule.id, tenant_id=tenant),
                                                key=lambda row: row.id)
                                for rule in rules},
              "examples": sorted(graph.examples_for_skill(ref.skill_id, tenant_id=tenant), key=lambda row: row.id),
              "tags": sorted(graph.tags_for_skill(ref.skill_id, tenant_id=tenant)),
              "tag_records": sorted(graph.tag_records_for_skill(ref.skill_id, tenant_id=tenant),
                                    key=lambda row: row.id),
              "files": sorted(graph.artefacts_for_skill(ref.skill_id, tenant_id=tenant),
                              key=lambda row: (row[1], row[0].id))}
    return record


def _owned_examples(record):
    examples = [*record["examples"], *(item for rows in record["rule_examples"].values() for item in rows)]
    for section in record["sections"]:
        examples.extend(section["examples"])
    return {example.id: example for example in examples}


def _owned_entities(record):
    entities = {("rule", rule.id) for rule in record["rules"]}
    for section in record["sections"]:
        entities.update(("block", block.id) for block in section["blocks"])
    entities.update(("example", identifier) for identifier in _owned_examples(record))
    return entities


def content_owners(graph, tenant_id: str, rule_ids: Iterable[str] = (),
                   block_ids: Iterable[str] = ()) -> dict[tuple[str, str], frozenset[str]]:
    """Every skill whose content includes each rule or block, by reverse lookup."""
    rules, blocks = sorted(set(rule_ids)), sorted(set(block_ids))
    # A rule or block can sit in several skills, and a write to it reaches
    # them all. Both stores answer this in one call; a graph without the
    # method (a test double, an older adapter) gets the slow but complete
    # walk over every skill of the tenant, so the answer is the same
    # whichever store is behind the graph.
    finder = getattr(graph, "content_owners", None)
    if finder is not None:
        return finder(rules, blocks, tenant_id=tenant_id)
    found = {("rule", rule): set() for rule in rules} | {("block", block): set() for block in blocks}
    for skill in graph.skills_for_tenant(tenant_id):
        for entity in _owned_entities(_skill_content(graph, SkillRef(tenant_id, skill.id))):
            if entity in found:
                found[entity].add(skill.id)
    return {entity: frozenset(owners) for entity, owners in found.items()}


def _fingerprint(graph, ref: SkillRef) -> tuple[str | None, frozenset[SkillRef]]:
    """One skill's content digest, and the other skills sharing any of its parts."""
    record = _skill_content(graph, ref)
    if record is None:
        return None, frozenset()
    entities = _owned_entities(record)
    found = content_owners(graph, ref.tenant_id, (key for kind, key in entities if kind == "rule"),
                           (key for kind, key in entities if kind == "block"))
    examples = _owned_examples(record)
    owners = {}
    for kind, key in entities:
        if kind == "example":
            label, parent = effective_example_parent(examples[key])
            skills = found.get(("rule", parent), frozenset()) if label == "Rule" else frozenset()
        else:
            skills = found.get((kind, key), frozenset())
        owners[kind, key] = sorted({SkillRef(ref.tenant_id, skill) for skill in skills} | {ref})
    record["ownership"] = [(entity, owners[entity]) for entity in sorted(entities)]
    record["source_ownership"] = [item.model_dump(mode="json")
                                  for item in source_repository(graph).ownership_for_skill(ref)]
    data = json.dumps(_content(record), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(data.encode()).hexdigest(), frozenset(owner for rows in owners.values() for owner in rows) - {ref}


def content_digest(graph, ref: SkillRef) -> str | None:
    return _fingerprint(graph, ref)[0]


def tenant_content(graph, tenant_id: str) -> dict[SkillRef, str]:
    return {ref: content_digest(graph, ref) for skill in graph.skills_for_tenant(tenant_id)
            if (ref := SkillRef(tenant_id, skill.id))}


def source_repository(graph):
    # Generations live in the source repository, and the tracker must read and
    # advance them through the same transaction as the writes it guards, so the
    # repository is derived from the graph handle rather than injected. The
    # imports are local so that this module, which the core repository loads
    # for every write, does not pull both adapter packages in at import time.
    from oms.sources.scoped import ScopedCollaborator
    if isinstance(graph, ScopedCollaborator):
        return ScopedCollaborator(source_repository(graph._target), graph._tenant_id)
    if hasattr(graph, "_driver"):
        from oms.adapters.neo4j.source_store import Neo4jSourceRepository
        return Neo4jSourceRepository(graph._driver)
    from oms.adapters.memory.source_store import InMemorySourceRepository
    return InMemorySourceRepository(graph)


_ACTIVE: ContextVar["ContentTracker | None"] = ContextVar("oms_content_tracker", default=None)


class ContentTracker:
    """The skills one transaction reaches, each fingerprinted before its first write."""

    def __init__(self, graph, tenant_id: str):
        self.graph, self.tenant_id = graph, tenant_id
        # A tracked write that runs inside another tracked write of the same
        # tenant reports every skill it reaches to the outer tracker as well,
        # so the outer transaction's baseline still predates the first write.
        # Another tenant's tracker is not a parent: its guards are not ours.
        parent = _ACTIVE.get()
        self.parent = parent if parent is not None and parent.tenant_id == tenant_id else None
        self.before: dict[SkillRef, str | None] = {}
        self.whole_tenant = False

    @contextmanager
    def active(self):
        token = _ACTIVE.set(self)
        try:
            yield RecordingGraph(self.graph, self)
        finally:
            _ACTIVE.reset(token)

    def skills(self, skill_ids: Iterable[str]) -> None:
        skill_ids = tuple(skill_ids)
        if self.parent is not None:
            self.parent.skills(skill_ids)
        # Only the first sighting is fingerprinted. The baseline has to be the
        # state immediately before the first write that reaches the skill; a
        # later write to the same skill must not replace it with a mid-transaction
        # digest, or the change would go unnoticed and the generation stand still.
        for skill_id in skill_ids:
            ref = SkillRef(self.tenant_id, skill_id)
            if ref not in self.before:
                self.before[ref] = content_digest(self.graph, ref)

    def rules(self, rule_ids: Iterable[str]) -> None:
        self.skills(sorted({skill for owners in content_owners(self.graph, self.tenant_id, rule_ids).values()
                            for skill in owners}))

    def blocks(self, block_ids: Iterable[str]) -> None:
        self.skills(sorted({skill for owners in content_owners(self.graph, self.tenant_id, (), block_ids).values()
                            for skill in owners}))

    def section(self, section_id: str) -> None:
        section = self.graph.get_section(section_id, tenant_id=self.tenant_id)
        if section is not None:
            self.skills((section.skill_id,))

    def example(self, example) -> None:
        if example is None or example.tenant_id != self.tenant_id:
            return
        label, parent = effective_example_parent(example)
        if label == "Rule":
            self.rules((parent,))
        elif label == "Section":
            self.section(parent)
        else:
            self.skills((parent,))

    def co_owners(self, skill_id: str) -> None:
        # Deleting a skill detaches content other skills may share, so they
        # change too and must be in the baseline before the delete runs.
        self.skills((skill_id,))
        _, others = _fingerprint(self.graph, SkillRef(self.tenant_id, skill_id))
        self.skills(sorted(other.skill_id for other in others))

    def everything(self) -> None:
        """Fall back to the whole tenant before a write this tracker cannot place.

        Slow, since every skill of the tenant is fingerprinted twice, but never
        wrong: a write nobody classified still advances every skill it changed.
        """
        if self.parent is not None:
            self.parent.everything()
        self.whole_tenant = True
        self.skills(skill.id for skill in self.graph.skills_for_tenant(self.tenant_id))

    def mark(self) -> dict[SkillRef, str | None]:
        refs = set(self.before)
        if self.whole_tenant:
            refs.update(SkillRef(self.tenant_id, skill.id) for skill in self.graph.skills_for_tenant(self.tenant_id))
        return {ref: content_digest(self.graph, ref) for ref in refs}

    def changed(self, since: dict[SkillRef, str | None] | None = None) -> tuple[SkillRef, ...]:
        baseline = {**self.before, **(since or {})}
        refs = set(baseline)
        if self.whole_tenant:
            refs.update(SkillRef(self.tenant_id, skill.id) for skill in self.graph.skills_for_tenant(self.tenant_id))
        return tuple(sorted(ref for ref in refs if content_digest(self.graph, ref) != baseline.get(ref)))

    def advance(self, allowed_skills: tuple[SkillRef, ...] | None = None) -> tuple[SkillRef, ...]:
        changed = self.changed()
        # The caller's guards cover only the skills it expected to reach (spec
        # §8.3). A change outside that set was made against a generation nobody
        # compared, so a concurrent writer could have altered the skill unseen;
        # the whole transaction is refused rather than committing over it.
        if allowed_skills is not None and not set(changed).issubset(allowed_skills):
            raise SourceConflict("content_changed", "affected skill set changed")
        # Generations are per skill and advance only where the digest moved, so
        # a write to one skill leaves every other skill's generation, and every
        # guard taken on it, intact. Locked first: the read of the old generation
        # and its replacement must not interleave with another writer's.
        if changed:
            sources = source_repository(self.graph)
            sources.lock_skills(changed)
            for ref in changed:
                old = sources.get_generations(ref)
                sources.advance_generations(old, Generations(skill=ref, content=old.content + 1,
                                                             binding=old.binding))
        return changed


def _in_tenant(tracker, tenant_id):
    return tenant_id is None or tenant_id == tracker.tenant_id


def _edge(tracker, edge, *, tenant_id=None, **_):
    if not _in_tenant(tracker, tenant_id):
        return
    # An edge carries identifiers, not kinds; the relationship table says what
    # each end can be, and every possibility is fingerprinted because a wrong
    # guess here would be a write the generations never saw.
    for side, identifier in ((0, edge.from_id), (1, edge.to_id)):
        labels = {pair[side] for pair in EDGE_LABELS.get(edge.type, ())}
        if "Skill" in labels and tracker.graph.get_skill(identifier, tenant_id=tracker.tenant_id) is not None:
            tracker.skills((identifier,))
        if "Rule" in labels:
            tracker.rules((identifier,))
        if "Section" in labels:
            tracker.section(identifier)
        if "ContentBlock" in labels:
            tracker.blocks((identifier,))
        if "Example" in labels:
            tracker.example(tracker.graph.get_example(identifier, tenant_id=tracker.tenant_id))


def _example(tracker, example, *, tenant_id=None, **_):
    if _in_tenant(tracker, tenant_id):
        tracker.example(tracker.graph.get_example(example.id, tenant_id=tracker.tenant_id))
        tracker.example(example)


def _tag(tracker, tag_id, name, **_):
    # Tag names are part of every carrying skill's content, so a rename changes
    # each of them; a tag written under its existing name changes nothing.
    previous = tracker.graph.tag_name(tag_id)
    if previous is not None and previous != name:
        tracker.skills(skill.id for skill in tracker.graph.skills_by_tags({previous}, tracker.tenant_id))


def _transaction(tracker, transaction, **_):
    # A transaction's tenant and source reference are recorded as provenance in
    # every skill holding a rule derived from it. Moving one would change an
    # unknown number of skills, so the whole tenant is the only safe baseline.
    previous = tracker.graph.get_transaction(transaction.id)
    if (previous is not None
        and (previous.tenant_id, previous.source_ref) != (transaction.tenant_id, transaction.source_ref)):
        tracker.everything()


def _rule_and_section(tracker, rule_id, section_id, *, tenant_id=None):
    if _in_tenant(tracker, tenant_id):
        tracker.rules((rule_id,))
        tracker.section(section_id)


def _block_and_section(tracker, block_id, section_id, *, tenant_id=None):
    if _in_tenant(tracker, tenant_id):
        tracker.blocks((block_id,))
        tracker.section(section_id)


def _owned_by(tracker, value, *, rules=(), skills=()):
    if getattr(value, "tenant_id", tracker.tenant_id) == tracker.tenant_id:
        tracker.rules(rules)
        tracker.skills(skills)


# Every graph-store method is one of three things to the tracker. `_REACHES`
# maps a content write to the skills it can change, derived from its own
# arguments before the write runs; `_UNTRACKED` names writes outside skill
# content; `_READS` names the methods that change nothing. A public callable in
# none of the three is a write somebody added without classifying it, and it
# falls back to fingerprinting the whole tenant: a slower transaction instead
# of a generation that silently fails to advance.
_REACHES: dict[str, Callable[..., None]] = {
    "upsert_rule": lambda t, rule, **_: _owned_by(t, rule, rules=(rule.id,)),
    "create_rule_if_absent": lambda t, rule, **_: _owned_by(t, rule, rules=(rule.id,)),
    "observe_rule_in": lambda t, rule_id, *args, **_: t.rules((rule_id,)),
    "upsert_transaction": _transaction,
    "upsert_skill": lambda t, skill, **_: _owned_by(t, skill, skills=(skill.id,)),
    "delete_skill": lambda t, skill_id, *, tenant_id=None, **_: _in_tenant(t, tenant_id) and t.co_owners(skill_id),
    "upsert_tag": _tag,
    "attach_edge": _edge,
    "detach_edge": _edge,
    "upsert_example": _example,
    "attach_example": _example,
    "remove_example": lambda t, example_id, *, tenant_id=None, **_: _in_tenant(t, tenant_id) and t.example(
        t.graph.get_example(example_id, tenant_id=t.tenant_id)),
    "upsert_section": lambda t, section, **_: section.tenant_id == t.tenant_id and (
        t.section(section.id) or t.skills((section.skill_id,))),
    "remove_section": lambda t, section_id, *, tenant_id=None, **_: _in_tenant(t, tenant_id) and t.section(section_id),
    "attach_rule": lambda t, rule, section_id, *args, tenant_id=None, **_: _rule_and_section(
        t, rule.id, section_id, tenant_id=tenant_id),
    "detach_rule": lambda t, rule_id, section_id, *, tenant_id=None, **_: _rule_and_section(
        t, rule_id, section_id, tenant_id=tenant_id),
    "inherit_placements": lambda t, retired_rule_id, successor, *, tenant_id=None, **_: _in_tenant(
        t, tenant_id) and t.rules((retired_rule_id, successor.id)),
    "attach_block": lambda t, block, section_id, *, tenant_id=None, **_: _block_and_section(
        t, block.id, section_id, tenant_id=tenant_id),
    "detach_block": lambda t, block_id, section_id, *, tenant_id=None, **_: _block_and_section(
        t, block_id, section_id, tenant_id=tenant_id),
    "supersede_block": lambda t, old_block_id, new_block, section_id, *args, tenant_id=None, **_: _block_and_section(
        t, old_block_id, section_id, tenant_id=tenant_id) or (_in_tenant(t, tenant_id) and t.blocks((new_block.id,))),
    "upsert_content_block": lambda t, block, **_: block.tenant_id == t.tenant_id and t.blocks((block.id,)),
    "upsert_artefact": lambda t, artefact, skill_id, path, *, tenant_id=None, **_: _in_tenant(
        t, tenant_id) and t.skills((skill_id,)),
    "remove_artefact": lambda t, skill_id, path, *, tenant_id=None, **_: _in_tenant(t, tenant_id)
    and t.skills((skill_id,)),
}

# Writes outside skill content: transaction workflow, review evidence, publication,
# history, usage and policy records. Any other public callable not named below
# as a read falls back to the whole tenant, so an unknown write is never missed.
_UNTRACKED = frozenset({
    "admit_scope_review", "admit_transaction", "append_skill_version", "claim_compile",
    "clear_publish_block", "clear_transaction_context", "delete_publication",
    "delete_publications_for_skill", "dismiss_all_failures", "dismiss_failure", "ensure_schema",
    "mark_compile_failed", "record_quarantine", "record_usage_event", "release_compile",
    "release_transaction", "set_compile_status", "set_constraint_status", "set_licence_hold",
    "set_publish_block", "trim_skill_versions", "upsert_constraint", "upsert_cross_skill_edge",
    "upsert_embedding", "upsert_learning", "upsert_publication",
})

_READS = frozenset({
    "active_blocks_without_body", "active_constraints", "all_constraints", "artefacts_for_skill",
    "blocks_for_section", "blocks_revised_for_rule", "content_owners", "cross_skill_neighbours",
    "examples_for_rule", "examples_for_section", "examples_for_skill", "failed_transactions",
    "find_duplicates", "find_related", "get_content_block", "get_example", "get_learning",
    "get_publication", "get_publish_block", "get_rule", "get_section", "get_skill", "get_skill_version",
    "get_transaction", "graph_neighbours", "graph_node", "latest_skill_version", "lineage",
    "pending_tenants", "pending_transactions", "publications_for_skill", "quarantined_payloads",
    "recent_skill_changes", "rule_context", "rule_origins", "rule_placements_for_section",
    "rules_by_tags", "rules_conflicting_with_constraints", "rules_derived_from", "rules_for_section",
    "rules_for_skill", "rules_for_tenant", "rules_page", "section_for_block", "sections_for_skill",
    "sections_with_multiple_active_blocks", "skill_versions", "skills_by_rule", "skills_by_tags",
    "skills_for_rule", "skills_for_tenant", "superseders_of", "tag_name", "tag_records_for_skill",
    "tags_for_skill", "text_search", "transaction_count_for_person",
    "transaction_counts_by_person_for_tenant", "transactions_by_ids", "transactions_for_person",
    "transactions_for_tenant", "usage_events", "workflow_state_for",
})

_OWN = frozenset({"_recording_graph", "_recording_tracker"})


class RecordingGraph:
    """The transaction's graph; each write first fingerprints the skills it reaches."""
    __slots__ = tuple(_OWN)

    def __init__(self, graph, tracker: ContentTracker):
        object.__setattr__(self, "_recording_graph", graph)
        object.__setattr__(self, "_recording_tracker", tracker)

    def __getattr__(self, name):
        if name in _OWN or name.startswith("__"):
            raise AttributeError(name)
        value = getattr(self._recording_graph, name)
        # Private names pass straight through, `_driver` among them:
        # `source_repository` reads it off this view to reach the same Neo4j
        # transaction, and an adapter's internals are not part of the port
        # this view classifies.
        if name.startswith("_") or not callable(value) or name in _READS or name in _UNTRACKED:
            return value
        reach, tracker = _REACHES.get(name), self._recording_tracker

        @wraps(value)
        def recorded(*args, **kwargs):
            if reach is None:
                tracker.everything()
            else:
                reach(tracker, *args, **kwargs)
            return value(*args, **kwargs)
        return recorded

    def __setattr__(self, name, value):
        if name in _OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self._recording_graph, name, value)

    def __delattr__(self, name):
        delattr(self._recording_graph, name)


def reach_skills(tenant_id: str, *, skill_ids: Iterable[str] = (), rule_ids: Iterable[str] = ()) -> None:
    """Declare skills a write is about to change outside the recorded graph methods."""
    tracker = _ACTIVE.get()
    if tracker is not None and tracker.tenant_id == tenant_id:
        tracker.rules(rule_ids)
        tracker.skills(skill_ids)


def watch_changes(graph, tenant_id: str) -> Callable[[], tuple[SkillRef, ...]]:
    """List the skills whose content changes from now on, from the active write's own snapshots."""
    # Inside a tracked write the tracker already holds the baseline for every
    # skill the write reaches, so the watch costs nothing extra. Outside one
    # (an importer run on its own) there is no such record, and the only
    # honest answer is to digest the whole tenant before and after.
    tracker = _ACTIVE.get()
    if tracker is not None and tracker.tenant_id == tenant_id:
        mark = tracker.mark()
        return lambda: tracker.changed(mark)
    before = tenant_content(graph, tenant_id)

    def changed():
        after = tenant_content(graph, tenant_id)
        return tuple(sorted(ref for ref in before.keys() | after.keys() if before.get(ref) != after.get(ref)))
    return changed


def validate_guards(graph, tenant_id: str, expected: tuple[Generations, ...]) -> None:
    if not expected:
        return
    refs = tuple(sorted(guard.skill for guard in expected))
    if any(ref.tenant_id != tenant_id for ref in refs):
        raise SourceForbidden("source_scope_denied", "mutation tenant mismatch")
    if len(refs) != len(set(refs)):
        raise ValueError("Mutation guards must be unique")
    # Locked before the comparison so that it still holds when the write
    # commits; compared without the lock, another transaction could advance a
    # generation between the check and the first write.
    sources = source_repository(graph)
    sources.lock_skills(refs)
    sources.compare_generations(expected)


def mutation_receipt(graph, tenant_id: str, result: T,
                     skills: Iterable[SkillRef]) -> tuple[T, tuple[Generations, ...], tuple[SkillRef, ...]]:
    """Committed generations of the guarded and changed skills, and which of them still exist."""
    sources = source_repository(graph)
    refs = tuple(sorted(set(skills)))
    return (result, tuple(sources.get_generations(ref) for ref in refs),
            tuple(ref for ref in refs if graph.get_skill(ref.skill_id, tenant_id=tenant_id) is not None))


def track_changes(graph, tenant_id: str, operation: Callable[[object], T], *,
                  allowed_skills: tuple[SkillRef, ...] | None = None) -> tuple[T, tuple[SkillRef, ...]]:
    """Run `operation` on a recording view of `graph`; advance each skill whose content changed."""
    tracker = ContentTracker(graph, tenant_id)
    with tracker.active() as recorded:
        result = operation(recorded)
    return result, tracker.advance(allowed_skills)


def conditional_change(graph, reviews, request: MutationRequest,
                       factory: MutationContextFactory,
                       operation: Callable[[MutationContext], T]) -> T:
    tracker = ContentTracker(graph, request.tenant_id)
    with tracker.active() as recorded:
        # The paid edition's factory wraps the graph in its own context. It may
        # hand back a different object, but it must be the same Neo4j
        # transaction: a second one would commit independently and the guards
        # compared here would say nothing about the writes made there.
        context = factory(recorded, reviews)
        if context.graph is not recorded:
            if not hasattr(graph, "_driver") or getattr(context.graph, "_driver", None) is not graph._driver:
                raise ValueError("Mutation factory must reuse the bound graph transaction")
            context = replace(context, graph=RecordingGraph(context.graph, tracker))
        from oms.sources.scoped import scoped_context
        scoped = scoped_context(context, recorded, reviews, request.tenant_id)
        context.admit()
        # Lock, then replay, then compare, in that order. The idempotency replay
        # comes before the generation comparison so a repeated request returns
        # the first result instead of a conflict about generations that its own
        # earlier commit advanced; a key reused for a different request or by
        # a different actor is refused, never answered with someone else's result.
        context.sources.lock_skills(request.affected_skills)
        previous = context.sources.get_operation(request.operation_id, tenant_id=request.tenant_id)
        if previous is not None:
            if previous.request_digest != request.request_digest or previous.context.actor_id != request.actor_id:
                raise SourceConflict("idempotency_key_reused")
            if previous.result.state in {"complete", "blocked", "failed", "awaiting_review"}:
                return previous.result
        context.sources.compare_generations(request.expected_generations)
        # Collaborators with state of their own, skill history among them, live
        # outside the graph transaction and do not roll back with it, so their
        # state is snapshotted here and put back by hand if the operation fails
        # after writing to them.
        snapshots = [(participant, participant.snapshot_state()) for participant in context.rollback_participants]
        try:
            result = operation(scoped)
            tracker.advance(request.affected_skills)
            return result
        except BaseException:
            for participant, snapshot in reversed(snapshots):
                participant.restore_state(snapshot)
            raise
