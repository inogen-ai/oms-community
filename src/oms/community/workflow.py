"""Manual learning: explicit human decisions, committed with lineage and history."""
from dataclasses import dataclass
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oms.community.matching import LocalMatcher, exact_text
from oms.community.placement import RuleInsertion, insertion_sections, insert_rule
from oms.domain.models import Edge, ReviewItem, Rule
from oms.domain.types import EdgeType, Plane, RuleStatus, SignalType, SkillVersionCause, Verdict
from oms.ports.workflow import DispositionResult, TERMINAL_STATES, effective_state
from oms.ports.custody import RetentionDisabled
from oms.publish.publisher import Publisher
from oms.publish.parts import document_revision
from oms.security.injection import DeterministicScreen
from oms.skills.history import SkillHistory
from oms.skills.service import SkillAdminService


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


class AmendmentPart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    anchor: str = Field(min_length=1, max_length=1000)
    # Clearing a reviewed rule retires it; its wording and history are kept.
    # Empty prose and metadata are still rejected by document validation.
    text: str = Field(max_length=1_000_000)
    expected_text: str = Field(max_length=1_000_000)


class Amendment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    skill_id: str = Field(min_length=1, max_length=1000)
    revision: str = Field(min_length=1, max_length=100)
    parts: list[AmendmentPart] = Field(min_length=1, max_length=100)
    affected_skill_ids: list[str] = Field(min_length=1, max_length=100)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["create", "reinforce", "amend", "reject", "release_safety"]
    body: str = Field(default="", max_length=10000)
    skill_ids: list[str] = Field(default_factory=list, max_length=100)
    rule_id: str | None = None
    confirm_reinforcement: bool = False
    expected_rule_body: str | None = Field(default=None, max_length=10000)
    amendment: Amendment | None = None
    placements: list[RuleInsertion] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def amendment_action(self):
        if self.action == "create" and (self.rule_id or self.confirm_reinforcement or self.expected_rule_body is not None):
            raise ValueError("creating a separate rule cannot also select or confirm an existing rule")
        if (self.action == "amend") != (self.amendment is not None):
            raise ValueError("an amendment is required only for the amend action")
        if self.amendment and (self.skill_ids != [self.amendment.skill_id]
                               or self.rule_id or self.confirm_reinforcement
                               or self.expected_rule_body is not None):
            raise ValueError("choose one amendment skill and its document parts")
        if self.placements and self.action != "create":
            raise ValueError("placement is only available when creating a rule")
        placement_skills = [placement.skill_id for placement in self.placements]
        if len(set(placement_skills)) != len(placement_skills) or not set(placement_skills) <= set(self.skill_ids):
            raise ValueError("choose at most one placement per selected skill")
        return self


@dataclass(frozen=True)
class DecisionResult:
    transaction_id: str
    state: str
    rule_id: str | None = None


class ManualDecisionError(ValueError):
    pass


class DecisionConflict(ManualDecisionError):
    pass


def _require_transaction(store, transaction_id, tenant_id):
    transaction = store.get_transaction(transaction_id)
    if transaction is None or transaction.tenant_id != tenant_id:
        raise ManualDecisionError("transaction not found in this workspace")
    if transaction.signal_type is SignalType.SKILL_IMPORT:
        raise ManualDecisionError("an import is provenance, not a manual correction")
    return transaction


def _enqueue(store, queue, transaction, payloads):
    state = effective_state(transaction, store)
    if state in TERMINAL_STATES:
        return DispositionResult(transaction.id, state)
    state = "held_safety" if transaction.held_reason else "awaiting_manual_review"
    item_id = f"manual-{transaction.id}"
    item = queue.get(item_id)
    if item is not None and item.resolved:
        raise DecisionConflict("resolved review item has an unfinished transaction")
    if item is None:
        context = payloads.get(transaction.id)
        text = context.contribution_text(transaction.signal_type) if context else transaction.summary or ""
        queue.enqueue(ReviewItem(
            id=item_id, kind="manual_correction", subject_id=transaction.id, other_id=None,
            verdict=Verdict.AMBIGUOUS, reason=transaction.held_reason or "human decision required",
            proposed_body=text, transaction_id=transaction.id, tenant_id=transaction.tenant_id))
    transaction.workflow_state = state
    store.upsert_transaction(transaction)
    return DispositionResult(transaction.id, state)


class ManualReviewDisposition:
    # States another edition's automation wrote and can no longer service.
    # A graph downgraded from the paid product carries `queued_automation`
    # rows, and `processing` rows whose worker no longer exists; with no
    # compiler here, both would otherwise stay invisible forever. The
    # reconcile sweep re-disposes them into the manual inbox.
    adoptable_states = frozenset({"queued_automation", "processing"})

    def __init__(self, repository, payloads):
        self.repository, self.payloads = repository, payloads

    def accept(self, transaction):
        return self.repository.atomic(transaction.id, transaction.tenant_id,
            lambda store, queue: _enqueue(store, queue,
                _require_transaction(store, transaction.id, transaction.tenant_id), self.payloads))


class ManualReviewService:
    def __init__(self, repository, payloads, retention=None, *, blob_store=None, history_factory=None,
                 publisher_factory=None):
        self.repository, self.payloads = repository, payloads
        self.retention = retention if retention is not None else RetentionDisabled()
        self.blob_store = blob_store
        self.history_factory = history_factory
        self.publisher_factory = publisher_factory

    def _publisher(self, store):
        return (self.publisher_factory(store) if self.publisher_factory else
                Publisher(store, blob_store=self.blob_store))

    def _history(self, store):
        return (self.history_factory(store) if self.history_factory else
                SkillHistory(store=store, publisher=self._publisher(store)))

    def _amend(self, store, txn, actor, amendment):
        skill_id, tenant_id = amendment.skill_id, txn.tenant_id
        service = SkillAdminService(store=store)
        service.skill(skill_id, tenant_id)
        parts, _ = self._publisher(store).outline_skill(skill_id, tenant_id)
        if document_revision(parts) != amendment.revision:
            raise DecisionConflict("This document has changed. Reload it before applying your correction.")
        editable = {part.anchor: part for part in parts
                    if part.editable and part.kind in ("rule", "overflow-rule", "prose")}
        anchors = [part.anchor for part in amendment.parts]
        if len(set(anchors)) != len(anchors) or not set(anchors) <= editable.keys():
            raise ManualDecisionError("Choose each editable rule or passage once.")
        # Reference-only rules do not contribute to the SKILL.md revision.
        # Check the selected text as well so those edits cannot overwrite a
        # newer reference version with the same main-document revision.
        if any(editable[part.anchor].edit_text != part.expected_text for part in amendment.parts):
            raise DecisionConflict("The selected guidance changed. Reload it before applying your correction.")
        removed_rules = {part.anchor for part in amendment.parts
                         if part.anchor.startswith("rule:") and not part.text.strip()}
        # The outline, reviewed text and active data-rule checks also apply to
        # removals. Only wording validation is skipped for those rules.
        problems = service.check_document(skill_id, tenant_id,
                                         [(part.anchor, part.text) for part in amendment.parts
                                          if part.anchor not in removed_rules])
        if problems:
            raise ManualDecisionError("; ".join(message for _, message in problems))
        affected = {skill_id}
        rule_ids = [anchor[5:] for anchor in anchors if anchor.startswith("rule:")]
        for rule_id in rule_ids:
            rule = store.get_rule(rule_id)
            if rule.status is not RuleStatus.ACTIVE or rule.plane is not Plane.DATA:
                raise ManualDecisionError("Only active guidance can be amended.")
            affected.update(skill.id for skill in store.skills_for_rule(rule_id)
                            if skill.tenant_id == tenant_id)
        if affected != set(amendment.affected_skill_ids):
            raise DecisionConflict("The affected skills changed. Reload and review all affected skills before applying.")
        for part in amendment.parts:
            if part.anchor.startswith("rule:") and len(part.text) > 10000:
                raise ManualDecisionError("A rule cannot exceed 10000 characters.")
            self._safe_wording(part.text, txn)
        # Capture the previous document too, even for guidance authored by an
        # older client which did not record a version. Both captures roll back
        # with the edit and inbox resolution if anything fails.
        for affected_id in sorted(affected):
            self._history(store).capture_required(affected_id, tenant_id,
                cause=SkillVersionCause.UNATTRIBUTED)
        for part in amendment.parts:
            if part.anchor.startswith("rule:"):
                rule_id = part.anchor[5:]
                if part.anchor in removed_rules:
                    rule = store.get_rule(rule_id)
                    rule.status = RuleStatus.RETIRED
                    store.upsert_rule(rule)
                else:
                    service.edit_rule(skill_id, rule_id, tenant_id, part.text, actor=actor)
                store.attach_edge(Edge(type=EdgeType.DERIVED_FROM, from_id=rule_id, to_id=txn.id))
            else:
                service.revise_block(skill_id, part.anchor[6:], tenant_id, part.text,
                                     actor=actor, transaction_id=txn.id)
        return (rule_ids[0] if len(rule_ids) == 1 else None), sorted(affected)

    @staticmethod
    def _safe_wording(body, txn):
        from oms.ingestion.sanitiser import RegexSanitiser
        from oms.ingestion.schema import ExecutionContext
        clean = RegexSanitiser().sanitise(ExecutionContext(
            user_input="", agent_raw_output="", user_correction=body)).context.user_correction
        if clean != body:
            raise ManualDecisionError("remove personal information before applying this wording")
        if DeterministicScreen().screen(body).score >= 0.5 and txn.workflow_safety_digest != _digest(body):
            raise ManualDecisionError("edited wording requires an explicit safety review")

    def inbox(self, tenant_id):
        store = self.repository.store
        skills = store.skills_for_tenant(tenant_id)
        rules = store.rules_for_tenant(tenant_id, limit=1_000_000)
        matcher = LocalMatcher(skills, rules)
        rows = []
        for item in self.repository.queue.pending(tenant_id):
            if item.kind != "manual_correction":
                continue
            txn = _require_transaction(store, item.subject_id, tenant_id)
            rows.append(self.describe(txn, skills=skills, rules=rules, body=item.proposed_body, matcher=matcher))
        return rows

    def describe(self, txn, *, skills=None, rules=None, body=None, matcher=None):
        """Project sanitised evidence for a manual decision in either edition."""
        store = self.repository.store
        if skills is None:
            skills = store.skills_for_tenant(txn.tenant_id)
        if rules is None:
            rules = store.rules_for_tenant(txn.tenant_id, limit=1_000_000)
        context = self.payloads.get(txn.id)
        body = body or (context.contribution_text(txn.signal_type) if context else txn.summary or "")
        matcher = matcher or LocalMatcher(skills, rules)
        exact_matches, similar_matches = matcher.rules_for(body)
        for match in similar_matches:
            match["skill_ids"] = sorted(skill.id for skill in store.skills_for_rule(match["id"])
                                        if skill.tenant_id == txn.tenant_id)
        return {"transaction_id": txn.id, "txn_id": txn.id, "text": body,
                "state": effective_state(txn, store), "skill_hint": txn.skill_hint,
                "posted_at": txn.timestamp.isoformat(), "repo": txn.repo,
                "source_agent_id": txn.source_agent_id, "source_runtime": txn.source_runtime.value,
                "context": {"user_input": context.user_input[:8000],
                            "agent_output": context.agent_raw_output[:8000]} if context else None,
                "context_truncated": bool(context and (len(context.user_input) > 8000
                                                       or len(context.agent_raw_output) > 8000)),
                "signal_type": txn.signal_type.value, "source_ref": txn.source_ref,
                "warning": txn.held_reason, "candidate_skill_ids": [s.id for s in skills
                    if txn.skill_hint and exact_text(txn.skill_hint) in (exact_text(s.id), exact_text(s.name))],
                "skill_suggestions": matcher.skills_for(body, txn.skill_hint),
                "exact_matches": exact_matches, "similar_matches": similar_matches}

    def decide(self, transaction_id, tenant_id, actor, decision: Decision):
        canonical = decision.model_dump()
        # Keep fingerprints of legacy exact-match decisions stable. New
        # confirmation fields participate only when actually supplied.
        if not canonical["confirm_reinforcement"]:
            del canonical["confirm_reinforcement"]
        if canonical["expected_rule_body"] is None:
            del canonical["expected_rule_body"]
        if canonical["amendment"] is None:
            del canonical["amendment"]
        else:
            canonical["amendment"]["parts"].sort(key=lambda part: part["anchor"])
            canonical["amendment"]["affected_skill_ids"] = sorted(set(canonical["amendment"]["affected_skill_ids"]))
        if not canonical["placements"]:
            del canonical["placements"]
        else:
            canonical["placements"].sort(key=lambda placement: placement["skill_id"])
        # What is applied is the sorted set of skills, so a replay of the same
        # decision with its skill list reordered is the same decision.
        canonical["skill_ids"] = sorted(set(canonical["skill_ids"]))
        fingerprint = _digest(json.dumps(canonical, sort_keys=True, separators=(",", ":")))

        def apply(store, queue):
            txn = _require_transaction(store, transaction_id, tenant_id)
            if effective_state(txn, store) == "processing":
                raise DecisionConflict("the correction is currently being processed")
            state = effective_state(txn, store)
            if state in TERMINAL_STATES:
                if txn.workflow_decision != fingerprint:
                    raise DecisionConflict("this correction already has a different decision")
                return DecisionResult(txn.id, state, txn.workflow_rule_id)
            _enqueue(store, queue, txn, self.payloads)
            item = queue.get(f"manual-{txn.id}")
            if decision.action == "release_safety":
                if not txn.held_reason:
                    return DecisionResult(txn.id, "awaiting_manual_review")
                txn.workflow_safety_digest = _digest((item.proposed_body or "").strip())
                txn.held_reason = None
                txn.workflow_state = "awaiting_manual_review"
                # Identity admission is deliberately untouched. The actor's
                # safety override is retained as a separate resolved item.
                override_id = f"manual-safety-{txn.id}"
                if queue.get(override_id) is None:
                    queue.enqueue(ReviewItem(id=override_id, kind="manual_safety_override",
                        subject_id=txn.id, other_id=None, verdict=Verdict.AMBIGUOUS,
                        reason=item.reason, tenant_id=tenant_id))
                    queue.resolve(override_id, "released", decided_by=actor)
                store.upsert_transaction(txn)
                legacy_hold = queue.get(f"review-held-{txn.id}")
                if legacy_hold is not None and not legacy_hold.resolved:
                    queue.resolve(legacy_hold.id, "released to manual review", decided_by=actor)
                return DecisionResult(txn.id, txn.workflow_state)
            if decision.action != "reject" and txn.held_reason:
                raise ManualDecisionError("review the safety hold before applying this correction")
            rule_id = None
            skill_ids = sorted(set(decision.skill_ids))
            if decision.action == "amend":
                rule_id, skill_ids = self._amend(store, txn, actor, decision.amendment)
            elif decision.action != "reject":
                body = decision.body.strip()
                if not body or not skill_ids:
                    raise ManualDecisionError("a nonempty rule and at least one selected skill are required")
                self._safe_wording(body, txn)
                for skill_id in skill_ids:
                    skill = store.get_skill(skill_id)
                    if skill is None or skill.tenant_id != tenant_id:
                        raise ManualDecisionError("selected skill not found in this workspace")
                if decision.action == "create":
                    for placement in decision.placements:
                        document, _ = self._publisher(store).outline_skill(placement.skill_id, tenant_id)
                        if document_revision(document) != placement.revision:
                            raise DecisionConflict("This document has changed. Reload it before placing the new rule.")
                        if placement.section_id not in {section["id"] for section in insertion_sections(store, placement.skill_id)}:
                            raise ManualDecisionError("Choose a rule section in the selected skill.")
                    rule_id = f"rule-{txn.id}"
                    if store.get_rule(rule_id) is not None:
                        raise DecisionConflict("the correction already has a rule")
                    rule = Rule(id=rule_id, body=body, tenant_id=tenant_id)
                else:
                    rule = store.get_rule(decision.rule_id or "")
                    if (rule is None or rule.tenant_id != tenant_id or rule.status is not RuleStatus.ACTIVE
                            or rule.plane is not Plane.DATA):
                        raise ManualDecisionError("reinforcement requires an active data rule in this workspace")
                    if decision.expected_rule_body is not None and decision.expected_rule_body != rule.body:
                        raise DecisionConflict("the selected rule changed; reload and compare it again")
                    is_exact = (exact_text(rule.body) == exact_text(body)
                                and exact_text(rule.body) == exact_text(item.proposed_body or ""))
                    if not is_exact and (not decision.confirm_reinforcement or decision.expected_rule_body is None):
                        raise ManualDecisionError("different wording requires explicit confirmation after comparing the existing rule")
                    rule_id = rule.id
                    rule.corroboration_count += 1
                store.upsert_rule(rule)
                store.attach_edge(Edge(type=EdgeType.DERIVED_FROM, from_id=rule_id, to_id=txn.id))
                for skill_id in skill_ids:
                    store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id=rule_id, to_id=skill_id))
                for placement in decision.placements:
                    insert_rule(store, rule, placement)
                skill_ids = sorted({s.id for s in store.skills_for_rule(rule_id)})
            if decision.action == "reject":
                store.mark_compile_failed(txn.id, "rejected by local operator", fault=False)
            txn.workflow_state = "rejected" if decision.action == "reject" else "applied"
            scope_item = queue.get(f"review-machine-{txn.id}")
            if scope_item is not None and not scope_item.resolved and (rule_id or decision.action == "amend"):
                txn.scope_reviewed = True
            txn.workflow_decision, txn.workflow_rule_id = fingerprint, rule_id
            store.upsert_transaction(txn)
            queue.resolve(item.id, decision.action, decided_by=actor)
            for legacy_id in (f"review-held-{txn.id}", f"review-unbound-{txn.id}", f"review-machine-{txn.id}"):
                legacy = queue.get(legacy_id)
                if legacy is not None and not legacy.resolved:
                    queue.resolve(legacy_id, f"manual {decision.action}", decided_by=actor)
            history = self._history(store)
            for skill_id in skill_ids if rule_id or decision.action == "amend" else []:
                history.capture_required(skill_id, tenant_id, cause=SkillVersionCause.RULE_EDIT,
                                         actor=actor, detail=(f"manual amend · correction {txn.id}" if decision.action == "amend"
                                                             else f"manual {decision.action}"))
            return DecisionResult(txn.id, txn.workflow_state, rule_id)

        result = self.repository.atomic(transaction_id, tenant_id, apply)
        if result.state in TERMINAL_STATES:
            # Deletion is idempotent and also retried after a committed
            # decision whose first response was interrupted.
            self.retention.forget(transaction_id, tenant_id)
        return result
