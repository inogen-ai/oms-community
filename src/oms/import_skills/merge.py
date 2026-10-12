from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from oms.settings.thresholds import tenant_threshold
from oms.domain.ids import example_id as _example_id
from oms.domain.ids import normalise as _normalise
from oms.domain.models import Edge, Example, ReviewItem, Rule
from oms.domain.types import EdgeType, Polarity, RuleStatus, SignalType, Verdict
from oms.import_skills.parser import ParsedSkill
from oms.import_skills.identity import canonical_source
from oms.ports.import_extension import ImportExtension
from oms.ports.graph_store import GraphStore
from oms.ports.injection_screen import CLEAN, InjectionScreen
from oms.ports.review_queue import ReviewQueue
from oms.ports.settings_store import SettingsStore
from oms.security.injection import DeterministicScreen

logger = logging.getLogger(__name__)

_IMPORT_PREFIX = "rule-import-"


@dataclass
class MergeReport:
    created: int = 0
    corroborated: int = 0
    flagged_removed: int = 0
    flagged_injection: int = 0

    def add(self, other: "MergeReport") -> None:
        self.created += other.created
        self.corroborated += other.corroborated
        self.flagged_removed += other.flagged_removed
        self.flagged_injection += other.flagged_injection


class MergeStep:
    def __init__(self, store: GraphStore, queue: ReviewQueue,
                 extension: ImportExtension | None = None,
                 injection_screen: InjectionScreen | None = None,
                 injection_threshold: float = 0.5,
                 settings_store: SettingsStore | None = None) -> None:
        self._store = store
        self._queue = queue
        self._extension = extension
        # Imported rule bodies bypass contribution ingestion, so screen them
        # at this write boundary before they can enter published guidance.
        self._screen = injection_screen or DeterministicScreen()
        # A SEPARATE key from the ingest boundary's, deliberately: import
        # screens a whole package at once and a security tenant's corpus
        # quotes attack strings by nature, so an administrator recovering
        # from a flooded queue must be able to raise this boundary without
        # blinding the one that watches live contributions (design §5.1).
        self._injection_threshold = injection_threshold
        self._settings_store = settings_store

    def merge_skill(self, skill: ParsedSkill, transaction_id: str, tenant_id: str,
                    *, observe_sources: bool = False) -> MergeReport:
        report = MergeReport()
        existing = self._store.rules_for_skill(skill.id, tenant_id=tenant_id)          # snapshot before inserts
        by_key = {(_normalise(r.body), r.polarity): r for r in existing}
        by_norm = {_normalise(r.body): r for r in existing}       # for cross-polarity detection
        by_id = {r.id: r for r in existing}                       # vector-match candidates
        seen: set[str] = set()
        incoming_norms: set[str] = set()                          # to suppress redundant removal flags

        for prule in skill.rules:
            if not prule.body.strip():
                continue    # never store an empty rule (its '' embedding poisons dedup)
            norm = _normalise(prule.body)
            incoming_norms.add(norm)
            key = (norm, prule.polarity)
            rule_id = _IMPORT_PREFIX + hashlib.sha1(
                f"{tenant_id}|{skill.id}|{norm}|{prule.polarity.value}".encode("utf-8")
            ).hexdigest()[:16]
            rule = Rule(id=rule_id, body=prule.body, tenant_id=tenant_id,
                        corroboration_count=0 if observe_sources else 1,
                        reference_only=prule.reference_only, polarity=prule.polarity)
            match = by_key.get(key)
            if match is None and self._extension is not None:
                match = self._extension.match_rule(rule, by_id)

            # Cross-polarity collision: same words, opposite intent.
            opposite = by_norm.get(norm)
            if (opposite is not None and opposite.polarity is not prule.polarity
                    and (match is None or match.id != opposite.id)):
                self._flag_polarity_conflict(opposite, prule, transaction_id, tenant_id)

            if match is not None:
                if not observe_sources:
                    match.corroboration_count += 1
                    self._store.upsert_rule(match)
                self._store.attach_edge(Edge(type=EdgeType.DERIVED_FROM,
                                             from_id=match.id, to_id=transaction_id), tenant_id=tenant_id)
                if observe_sources:
                    self._store.observe_rule_in(match.id, transaction_id, skill.source_ref)
                for ex in prule.examples:
                    self._store.upsert_example(Example(
                        id=_example_id(match.id, ex.body),
                        body=ex.body, kind=ex.kind, tenant_id=tenant_id,
                        parent_rule_id=match.id,
                    ), tenant_id=tenant_id)
                seen.add(match.id)
                report.corroborated += 1
                continue

            if self._screen_body(rule, transaction_id, tenant_id):
                report.flagged_injection += 1
            if observe_sources:
                # Another import may have created and observed this exact
                # rule after our initial snapshot. Preserve its count and any
                # intervening review instead of replacing it with count zero.
                self._store.create_rule_if_absent(rule)
            else:
                self._store.upsert_rule(rule)
            if self._extension is not None:
                self._extension.rule_stored(rule)
            self._store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id=rule.id, to_id=skill.id), tenant_id=tenant_id)
            # The rule's OWN tags only. The skill's go on the skill, because a
            # tag copied down here would make `rules_by_tags` answer with every
            # rule in the skill rather than the ones about the tag.
            for tag in dict.fromkeys(prule.tags):
                self._store.upsert_tag(tag, tag)
                self._store.attach_edge(Edge(type=EdgeType.TAGGED_WITH, from_id=rule.id, to_id=tag), tenant_id=tenant_id)
            self._store.attach_edge(Edge(type=EdgeType.DERIVED_FROM, from_id=rule.id, to_id=transaction_id), tenant_id=tenant_id)
            if observe_sources:
                self._store.observe_rule_in(rule.id, transaction_id, skill.source_ref)
            for ex in prule.examples:
                self._store.upsert_example(Example(
                    id=_example_id(rule.id, ex.body),
                    body=ex.body, kind=ex.kind, tenant_id=tenant_id,
                    parent_rule_id=rule.id,
                ), tenant_id=tenant_id)
            by_key[key] = rule
            by_norm[norm] = rule
            by_id[rule.id] = rule
            seen.add(rule.id)
            report.created += 1

        # Bullets dropped from the source on re-import: route to review, never delete (spec 4.4).
        # Suppress removal when the same normalised body appears with opposite polarity in
        # this import: that case is already covered by a polarity_conflict signal.
        for r in existing:
            if (r.id.startswith(_IMPORT_PREFIX) and r.id not in seen
                    and r.status is RuleStatus.ACTIVE
                    and _normalise(r.body) not in incoming_norms
                    and any(t.tenant_id == tenant_id and t.signal_type is SignalType.SKILL_IMPORT
                            and t.source_ref and canonical_source(t.source_ref, skill.import_name or skill.id) == skill.source_ref
                            for t in self._store.lineage(r.id))):
                self._queue.enqueue_once(ReviewItem(
                    id=f"removal-{r.id}-{transaction_id}", kind="removal", subject_id=r.id,
                    other_id=None, verdict=Verdict.AMBIGUOUS,
                    reason=f"rule absent from re-imported {skill.source_ref}", tenant_id=tenant_id,
                    transaction_id=transaction_id))
                report.flagged_removed += 1
        return report


    def _screen_body(self, rule: Rule, transaction_id: str, tenant_id: str) -> bool:
        """Screen an imported rule body before it is stored. True when flagged.

        Mutates `rule.status` rather than refusing the import: the same
        flag-never-reject policy the ingest screen follows, and for the same
        reason - a security tenant's genuine rules quote attack strings
        verbatim, so a rejecting screen would silently drop the corpus such a
        tenant most needs recorded. FLAGGED keeps the rule out of publish until
        a human has looked at it.
        """
        # Same belt-and-braces as the ingest boundary: the never-raise promise
        # lives in the screen, and the price of a broken one here would be a
        # failed import rather than a missed detection.
        try:
            result = self._screen.screen(rule.body)
        except Exception:
            logger.exception("injection screen failed; treating rule %s as clean",
                             rule.id)
            result = CLEAN
        threshold = tenant_threshold(self._settings_store, tenant_id,
                                     "injection_threshold_import",
                                     self._injection_threshold)
        if result.score < threshold:
            return False
        rule.status = RuleStatus.FLAGGED
        self._queue.enqueue_once(ReviewItem(
            id=f"injection-{rule.id}-{transaction_id}",
            kind="injection", subject_id=rule.id, other_id=None,
            verdict=Verdict.AMBIGUOUS,
            reason=f"imported rule body: {result.reason()}",
            tenant_id=tenant_id, priority="high"))
        return True

    def _flag_polarity_conflict(self, existing: Rule, incoming, transaction_id: str, tenant_id: str) -> None:
        self._queue.enqueue_once(ReviewItem(
            id=f"polarity-{existing.id}-{transaction_id}",
            kind="polarity_conflict", subject_id=existing.id, other_id=None,
            verdict=Verdict.CONFLICTS_WITH,
            reason=(f"normalised body matches an existing rule with opposite polarity "
                    f"({existing.polarity.value} vs {incoming.polarity.value})"),
            tenant_id=tenant_id,
        ))
